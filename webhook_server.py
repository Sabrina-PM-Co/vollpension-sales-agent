#!/usr/bin/env python3
"""
FastAPI Webhook-Server – v2.0
==============================
Empfängt alle Events und koordiniert Angebots-, Rechnungs- und Mahnungsworkflows.

Endpoints:
  POST /webhook/pipedrive          – Pipedrive: Deal added → Angebotsworkflow
  POST /webhook/deal-won           – Pipedrive: Deal gewonnen → Rechnungsworkflow
  POST /webhook/invoice-final      – Pipedrive: Event Datum +1 → Schlussrechnung
  POST /webhook/slack/interactive  – Slack: Button-Klicks (Freigabe/Ablehnung)
  POST /webhook/slack/events       – Slack: Text-Nachrichten (Freigabe per Text)
  GET  /cron/dunning-check         – Täglicher Cron-Job: Mahnwesen
  GET  /health                     – Health Check

Pipedrive Automations einrichten:
  1. Trigger "Deal Status → Won"  → POST /webhook/deal-won
  2. Trigger "Event Datum" +1 Tag → POST /webhook/invoice-final

Slack App einrichten:
  - Interactivity URL: https://DEINE-DOMAIN.com/webhook/slack/interactive
  - Events URL:        https://DEINE-DOMAIN.com/webhook/slack/events
  - Subscribed events: message.channels, app_mention
"""

import os
import json
import hashlib
import hmac
import asyncio
import logging
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse

from config          import (
    WEBHOOK_SECRET, SLACK_SIGNING_SECRET, CRON_SECRET, ADMIN_SECRET,
    PIPEDRIVE_STAGE_ANFRAGEN, PIPEDRIVE_STAGE_ANGEBOT_GELEGT,
)
from pipedrive_tools import pipedrive_update_deal
from agent           import process_new_deal
from invoice_workflow import process_deal_won, process_final_invoice, execute_after_approval
from offer_workflow   import execute_after_change_request
from dunning_workflow import process_dunning_check
from audit_logger    import cleanup_old_logs, get_cost_report
from slack_approval  import handle_interactive_action, handle_slack_message
from state_manager   import get_approval_request, get_invoice_state
import sqlite3
from pathlib import Path

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("webhook")

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Pipedrive → Sevdesk Agent v2",
    description="Angebots-, Rechnungs- und Mahnungsworkflow mit Slack-Freigabe.",
    version="2.0.0",
)


# ─── Sicherheit ───────────────────────────────────────────────────────────────

def verify_pipedrive_secret(request: Request) -> bool:
    if not WEBHOOK_SECRET:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            _, pwd = decoded.split(":", 1)
            return hmac.compare_digest(pwd, WEBHOOK_SECRET)
        except Exception:
            return False
    return True


def verify_slack_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    """Verifiziert Slack-Request-Signatur via HMAC-SHA256."""
    secret = SLACK_SIGNING_SECRET.strip()
    if not secret:
        return True  # Nur in Entwicklung – in Produktion immer setzen!
    sig_basestring = f"v0:{timestamp}:{request_body.decode()}"
    expected = "v0=" + hmac.new(
        secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_cron_secret(request: Request) -> bool:
    """Schützt den Cron-Endpoint vor unautorisierten Aufrufen."""
    if not CRON_SECRET:
        return True
    secret = request.headers.get("X-Cron-Secret", "")
    return hmac.compare_digest(secret, CRON_SECRET)


# ─── Background Task Helper ───────────────────────────────────────────────────

async def _bg(fn, *args):
    """Führt eine synchrone Funktion im ThreadPool aus."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: fn(*args))
        logger.info(f"✅ Background Task abgeschlossen: {fn.__name__} → {result}")
    except Exception as exc:
        logger.exception(f"❌ Background Task Fehler ({fn.__name__}): {exc}")


# ─── ANGEBOTS-WORKFLOW ────────────────────────────────────────────────────────

@app.post("/webhook/pipedrive")
async def pipedrive_new_deal(request: Request, background_tasks: BackgroundTasks):
    """
    Pipedrive: Deal angelegt ODER in Stage "Anfragen" verschoben → Angebotsworkflow.

    Triggert bei:
      - action=added  (neuer Deal direkt in "Anfragen")
      - action=updated + stage_id wechselt zu PIPEDRIVE_STAGE_ANFRAGEN
        (Deal manuell in "Anfragen" verschoben)
    """
    body = await request.body()

    if not verify_pipedrive_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload: dict = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültiger JSON-Payload")

    meta    = payload.get("meta", {})
    action  = meta.get("action", "")
    obj     = meta.get("object", "")
    current = payload.get("current", {})
    previous = payload.get("previous", {})

    if obj != "deal":
        return JSONResponse({"status": "ignored", "reason": "Kein Deal-Event"})

    deal_id = current.get("id")
    if not deal_id:
        return JSONResponse({"status": "ignored", "reason": "Keine Deal-ID"})

    # Trigger 1: Neuer Deal angelegt
    if action == "added":
        logger.info(f"📥 Neuer Deal angelegt: ID={deal_id}")
        background_tasks.add_task(_bg, process_new_deal, deal_id, current)
        return JSONResponse({"status": "accepted", "deal_id": deal_id, "trigger": "added"})

    # Trigger 2: Deal in Stage "Anfragen" verschoben (manuell oder via Automation)
    if action == "updated" and PIPEDRIVE_STAGE_ANFRAGEN:
        current_stage  = current.get("stage_id")
        previous_stage = previous.get("stage_id")
        if (current_stage == PIPEDRIVE_STAGE_ANFRAGEN
                and previous_stage != PIPEDRIVE_STAGE_ANFRAGEN):
            logger.info(f"📥 Deal {deal_id} in Stage 'Anfragen' verschoben (Stage {previous_stage} → {current_stage})")
            background_tasks.add_task(_bg, process_new_deal, deal_id, current)
            return JSONResponse({"status": "accepted", "deal_id": deal_id, "trigger": "stage_change"})

    return JSONResponse({"status": "ignored"})


# ─── RECHNUNGS-WORKFLOW: Trigger 1 ────────────────────────────────────────────

@app.post("/webhook/deal-won")
async def deal_won_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Pipedrive Automation: Deal-Status wechselt zu 'Won'.
    → Prüft Deal-Betrag → ggf. Anzahlungsrechnung erstellen + Slack-Freigabe.

    Pipedrive sendet: {"deal_id": 123, "deal_value": 2400.0}
    """
    body = await request.body()

    if not verify_pipedrive_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload: dict = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültiger JSON-Payload")

    # Pipedrive Automation kann verschiedene Payload-Strukturen senden
    deal_id    = payload.get("deal_id") or payload.get("current", {}).get("id")
    deal_value = float(
        payload.get("deal_value")
        or payload.get("current", {}).get("value", 0)
        or 0
    )

    if not deal_id:
        raise HTTPException(status_code=400, detail="Keine Deal-ID im Payload")

    logger.info(f"🏆 Deal gewonnen: ID={deal_id}, Betrag=€{deal_value}")
    background_tasks.add_task(_bg, process_deal_won, deal_id, deal_value)

    return JSONResponse({
        "status":  "accepted",
        "deal_id": deal_id,
        "message": f"Rechnungsworkflow gestartet (Betrag: €{deal_value})",
    })


# ─── RECHNUNGS-WORKFLOW: Trigger 2 ────────────────────────────────────────────

@app.post("/webhook/invoice-final")
async def invoice_final_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Pipedrive Automation: 'Event Datum' Feld + 1 Tag.
    → Schlussrechnung erstellen (50% oder 100%, mit/ohne Storno) + Slack-Freigabe.

    Pipedrive sendet: {"deal_id": 123}
    """
    body = await request.body()

    if not verify_pipedrive_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload: dict = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültiger JSON-Payload")

    deal_id = payload.get("deal_id") or payload.get("current", {}).get("id")
    if not deal_id:
        raise HTTPException(status_code=400, detail="Keine Deal-ID im Payload")

    logger.info(f"📅 Event vorbei → Schlussrechnung für Deal {deal_id}")
    background_tasks.add_task(_bg, process_final_invoice, deal_id)

    return JSONResponse({
        "status":  "accepted",
        "deal_id": deal_id,
        "message": "Schlussrechnungsworkflow gestartet",
    })


# ─── SLACK: Button-Klicks ─────────────────────────────────────────────────────

@app.post("/webhook/slack/interactive")
async def slack_interactive(request: Request, background_tasks: BackgroundTasks):
    """
    Slack Interactive Components: Button-Klicks (Freigabe / Ablehnung).

    Slack sendet payload als URL-encoded Form-Daten (nicht JSON!).
    Signaturverifikation via SLACK_SIGNING_SECRET.
    """
    body      = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Ungültige Slack-Signatur")

    # Slack schickt payload als URL-encoded: payload=<URL-encoded JSON>
    body_str = body.decode("utf-8")
    parsed   = urllib.parse.parse_qs(body_str)
    raw_payload = parsed.get("payload", [""])[0]

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültiger Slack-Payload")

    # Sofort 200 zurück an Slack (Slack erwartet <3 Sek.)
    # Dann im Hintergrund verarbeiten
    background_tasks.add_task(_handle_slack_action, payload)

    return JSONResponse({})  # Slack erwartet leere 200-Antwort


async def _handle_slack_action(payload: dict):
    """Verarbeitet Slack Button-Klick im Hintergrund."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: handle_interactive_action(payload)
        )

        action = result.get("action")
        logger.info(f"Slack-Aktion: {action}")

        if action == "approved":
            # Freigabe → Versand ausführen
            approval_req = result.get("request")
            if approval_req:
                await loop.run_in_executor(
                    None, lambda: execute_after_approval(approval_req)
                )
                # Angebot freigegeben + versendet → Deal in Stage "Angebot gelegt" schieben
                if (approval_req.get("request_type") == "offer"
                        and PIPEDRIVE_STAGE_ANGEBOT_GELEGT):
                    deal_id = approval_req.get("deal_id")
                    if deal_id:
                        logger.info(f"📊 Deal {deal_id} → Stage 'Angebot gelegt' ({PIPEDRIVE_STAGE_ANGEBOT_GELEGT})")
                        await loop.run_in_executor(
                            None,
                            lambda: pipedrive_update_deal(
                                deal_id, {"stage_id": PIPEDRIVE_STAGE_ANGEBOT_GELEGT}
                            ),
                        )

        elif action == "awaiting_changes":
            # Ändern-Button gedrückt → Nur State-Update + Thread-Prompt
            # (bereits in handle_interactive_action erledigt, kein weiterer Schritt nötig)
            logger.info(f"Änderungswunsch für Request {result.get('request_id')} – warte auf Feedback im Thread")

    except Exception as exc:
        logger.exception(f"Fehler beim Verarbeiten der Slack-Aktion: {exc}")


# ─── SLACK: Text-Nachrichten ──────────────────────────────────────────────────

@app.post("/webhook/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """
    Slack Event API: Text-Nachrichten im Freigabe-Channel.
    Erkennt Freigabe-Keywords ('freigabe', 'ok', 'ja', ...) in Thread-Antworten.

    Slack schickt beim ersten Einrichten eine URL-Verification Challenge.
    """
    body      = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültiger JSON-Payload")

    # Slack URL-Verification (einmalig beim Einrichten) – vor Signaturprüfung!
    if data.get("type") == "url_verification":
        return JSONResponse({"challenge": data.get("challenge")})

    if not verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Ungültige Slack-Signatur")

    event = data.get("event", {})

    # Nur relevante Nachrichten (kein Bot, muss Thread sein)
    if event.get("type") == "message" and not event.get("bot_id"):
        background_tasks.add_task(_handle_slack_message, event)

    return JSONResponse({"status": "ok"})


async def _handle_slack_message(event: dict):
    """Verarbeitet Text-Nachricht im Hintergrund."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: handle_slack_message(event)
        )

        if not result:
            return

        action = result.get("action")

        if action == "approved":
            approval_req = get_approval_request(result["request_id"])
            if approval_req:
                await loop.run_in_executor(
                    None, lambda: execute_after_approval(approval_req)
                )
                # Angebot freigegeben + versendet → Deal in Stage "Angebot gelegt" schieben
                if (approval_req.get("request_type") == "offer"
                        and PIPEDRIVE_STAGE_ANGEBOT_GELEGT):
                    deal_id = approval_req.get("deal_id")
                    if deal_id:
                        logger.info(f"📊 Deal {deal_id} → Stage 'Angebot gelegt' ({PIPEDRIVE_STAGE_ANGEBOT_GELEGT})")
                        await loop.run_in_executor(
                            None,
                            lambda: pipedrive_update_deal(
                                deal_id, {"stage_id": PIPEDRIVE_STAGE_ANGEBOT_GELEGT}
                            ),
                        )

        elif action == "change_requested":
            # Feedback-Text aus Slack → Agent überarbeitet Angebot
            approval_req = result.get("request")
            feedback     = result.get("feedback", "")
            if approval_req and feedback:
                logger.info(f"Starte Angebotsüberarbeitung für Request {result['request_id']}: {feedback[:80]}")
                await loop.run_in_executor(
                    None, lambda: execute_after_change_request(approval_req, feedback)
                )

        elif action == "feedback":
            logger.info(f"Slack Freitext-Feedback: {result.get('feedback', '')[:100]}")

    except Exception as exc:
        logger.exception(f"Fehler beim Verarbeiten der Slack-Nachricht: {exc}")


# ─── MAHNWESEN: Cron-Job ──────────────────────────────────────────────────────

@app.get("/cron/dunning-check")
async def dunning_cron(request: Request, background_tasks: BackgroundTasks):
    """
    Täglicher Cron-Job: Prüft offene Rechnungen auf Fälligkeit.
    Gesichert via X-Cron-Secret Header.

    Railway Cron einrichten:
      - Command: curl -H "X-Cron-Secret: $CRON_SECRET" https://DEINE-DOMAIN.com/cron/dunning-check
      - Schedule: 0 8 * * * (täglich 8:00 Uhr)
    """
    if not verify_cron_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("🔔 Mahnwesen-Cron gestartet")
    background_tasks.add_task(_bg, process_dunning_check)
    background_tasks.add_task(_bg, cleanup_old_logs)   # Logs > 90 Tage aufräumen

    return JSONResponse({"status": "started", "message": "Mahnwesen-Check + Log-Cleanup läuft im Hintergrund"})


# ─── HEALTH & INFO ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "pipedrive-sevdesk-agent", "version": "2.0.0"}


@app.get("/")
async def root():
    return {
        "service": "Pipedrive → Sevdesk Agentic Workflow v2",
        "version": "2.0.0",
        "endpoints": {
            "POST /webhook/pipedrive":         "Neuer Deal → Angebotsworkflow",
            "POST /webhook/deal-won":           "Deal gewonnen → Anzahlungsrechnung",
            "POST /webhook/invoice-final":      "Event vorbei → Schlussrechnung",
            "POST /webhook/slack/interactive":  "Slack Button-Klicks (Freigabe)",
            "POST /webhook/slack/events":       "Slack Text-Nachrichten (Freigabe per Text)",
            "GET  /cron/dunning-check":         "Mahnwesen-Check (täglich)",
            "GET  /health":                     "Health Check",
        },
    }


# ─── ADMIN: Feedback History & Deal-Übersicht ────────────────────────────────

def _verify_admin(request: Request) -> bool:
    """Schützt Admin-Endpoints via X-Admin-Secret Header."""
    if not ADMIN_SECRET:
        return True  # Nur für lokale Entwicklung – in Produktion immer setzen!
    return hmac.compare_digest(
        request.headers.get("X-Admin-Secret", ""),
        ADMIN_SECRET,
    )


@app.get("/admin/deals/{deal_id}/feedback")
async def get_deal_feedback(deal_id: int, request: Request):
    """
    Gibt die vollständige Feedback-History für einen Deal zurück.

    Header: X-Admin-Secret: <ADMIN_SECRET aus .env>

    Response:
      {
        "deal_id": 42,
        "deal_title": "...",
        "approval_requests": [
          {
            "id": 1,
            "request_type": "ar_invoice",
            "invoice_number": "AN-2025-001",
            "status": "superseded",
            "revision_count": 2,
            "feedback_history": [
              { "round": 1, "user_id": "U123ABC", "feedback": "Preis erhöhen", "timestamp": "..." },
              ...
            ],
            "created_at": "...",
            "approved_at": "..."
          }, ...
        ]
      }
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    db_path = Path(os.environ.get("DB_PATH", "") or (Path(__file__).parent / "workflow_state.db"))
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row

        state_row = con.execute(
            "SELECT deal_title FROM invoice_state WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        deal_title = state_row["deal_title"] if state_row else f"Deal #{deal_id}"

        rows = con.execute(
            """SELECT id, request_type, invoice_number, invoice_id, status,
                      revision_count, feedback_history,
                      approved_by, approved_at,
                      changes_requested_by, changes_requested_at,
                      created_at, updated_at
               FROM approval_requests
               WHERE deal_id = ?
               ORDER BY created_at ASC""",
            (deal_id,)
        ).fetchall()
        con.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB-Fehler: {e}")

    requests_out = []
    for r in rows:
        try:
            feedback = json.loads(r["feedback_history"] or "[]")
        except Exception:
            feedback = []
        requests_out.append({
            "id":                    r["id"],
            "request_type":          r["request_type"],
            "invoice_number":        r["invoice_number"],
            "invoice_id":            r["invoice_id"],
            "status":                r["status"],
            "revision_count":        r["revision_count"] or 0,
            "feedback_history":      feedback,
            "approved_by":           r["approved_by"],
            "approved_at":           r["approved_at"],
            "changes_requested_by":  r["changes_requested_by"],
            "changes_requested_at":  r["changes_requested_at"],
            "created_at":            r["created_at"],
            "updated_at":            r["updated_at"],
        })

    return JSONResponse({
        "deal_id":           deal_id,
        "deal_title":        deal_title,
        "total_revisions":   sum(r["revision_count"] or 0 for r in rows),
        "approval_requests": requests_out,
    })


@app.get("/admin/deals/{deal_id}/state")
async def get_deal_state(deal_id: int, request: Request):
    """
    Gibt den vollständigen Workflow-State eines Deals zurück inkl. Agent-Notizen.

    Header: X-Admin-Secret: <ADMIN_SECRET aus .env>
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    state = get_invoice_state(deal_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Kein State für Deal {deal_id}")

    try:
        state["agent_notes"] = json.loads(state.get("agent_notes") or "[]")
    except Exception:
        pass

    return JSONResponse(state)


@app.get("/admin/pending-approvals")
async def get_pending_approvals(request: Request):
    """
    Listet alle offenen Freigabeanfragen (pending + awaiting_changes).

    Header: X-Admin-Secret: <ADMIN_SECRET aus .env>
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    db_path = Path(os.environ.get("DB_PATH", "") or (Path(__file__).parent / "workflow_state.db"))
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT ar.*, ist.deal_title
               FROM approval_requests ar
               LEFT JOIN invoice_state ist ON ar.deal_id = ist.deal_id
               WHERE ar.status IN ('pending', 'awaiting_changes')
               ORDER BY ar.created_at DESC""",
        ).fetchall()
        con.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB-Fehler: {e}")

    out = []
    for r in rows:
        try:
            feedback = json.loads(r["feedback_history"] or "[]")
        except Exception:
            feedback = []
        out.append({
            "id":             r["id"],
            "deal_id":        r["deal_id"],
            "deal_title":     r["deal_title"] or f"Deal #{r['deal_id']}",
            "request_type":   r["request_type"],
            "invoice_number": r["invoice_number"],
            "status":         r["status"],
            "revision_count": r["revision_count"] or 0,
            "feedback_rounds": len(feedback),
            "last_feedback":  feedback[-1]["feedback"][:100] if feedback else None,
            "created_at":     r["created_at"],
        })

    return JSONResponse({"count": len(out), "pending": out})


# ─── ADMIN: 3 Workflow-History-Dokumente ──────────────────────────────────────

_HISTORY_CSS = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 1200px; margin: 0 auto; padding: 24px; background: #f8fafc; color: #1e293b; }
  h1   { font-size: 1.5rem; margin-bottom: 4px; }
  .sub { color: #64748b; font-size: .875rem; margin-bottom: 24px; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
  th   { background: #1e293b; color: #fff; text-align: left; padding: 10px 14px; font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; }
  td   { padding: 10px 14px; border-bottom: 1px solid #e2e8f0; font-size: .875rem; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #f1f5f9; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: .75rem; font-weight: 600; }
  .pending         { background: #fef3c7; color: #92400e; }
  .approved        { background: #d1fae5; color: #065f46; }
  .awaiting_changes{ background: #dbeafe; color: #1e40af; }
  .superseded      { background: #f1f5f9; color: #475569; }
  .expired         { background: #fee2e2; color: #991b1b; }
  details summary  { cursor: pointer; color: #3b82f6; font-size: .8rem; }
  pre  { margin: 4px 0 0; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px; padding: 8px; font-size: .75rem; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }
  .nav { display: flex; gap: 12px; margin-bottom: 20px; }
  .nav a { padding: 6px 14px; background: #e2e8f0; border-radius: 6px; text-decoration: none; color: #334155; font-size: .875rem; }
  .nav a.active { background: #1e293b; color: #fff; }
  .empty { text-align: center; padding: 40px; color: #94a3b8; }
</style>
"""

def _history_nav(active: str) -> str:
    links = [
        ("offers",   "📋 Angebote",   "/admin/history/offers"),
        ("invoices", "🧾 Rechnungen", "/admin/history/invoices"),
        ("dunning",  "🔔 Mahnungen",  "/admin/history/dunning"),
    ]
    nav = '<div class="nav">'
    for key, label, href in links:
        css = ' class="active"' if key == active else ""
        nav += f'<a href="{href}"{css}>{label}</a>'
    return nav + "</div>"


def _render_history_html(
    title: str,
    nav_active: str,
    rows: list,
    subtitle: str = "",
) -> str:
    nav   = _history_nav(nav_active)
    table = ""

    if not rows:
        table = '<p class="empty">Keine Einträge vorhanden.</p>'
    else:
        table += """<table>
<thead><tr>
  <th>#</th><th>Deal</th><th>Typ</th><th>Nummer</th>
  <th>Status</th><th>Revisionen</th><th>Erstellt</th><th>Feedback-History</th>
</tr></thead><tbody>"""
        for r in rows:
            history_json = json.dumps(r["feedback_history"], indent=2, ensure_ascii=False)
            rounds       = len(r["feedback_history"])
            status_css   = r["status"].replace(" ", "_")
            table += f"""<tr>
  <td>{r['id']}</td>
  <td><strong>{r['deal_title']}</strong><br><small>Deal #{r['deal_id']}</small></td>
  <td><code>{r['request_type']}</code></td>
  <td>{r['invoice_number'] or '–'}</td>
  <td><span class="badge {status_css}">{r['status']}</span></td>
  <td style="text-align:center">{r['revision_count'] or 0}</td>
  <td>{(r['created_at'] or '')[:16]}</td>
  <td>
    <details><summary>{rounds} Eintrag/Einträge</summary>
      <pre>{history_json}</pre>
    </details>
  </td>
</tr>"""
        table += "</tbody></table>"

    return f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><title>{title}</title>{_HISTORY_CSS}</head>
<body>
  <h1>{title}</h1>
  <p class="sub">{subtitle} · <a href="/docs">API Docs</a></p>
  {nav}
  {table}
</body>
</html>"""


def _fetch_history(request_types: list[str]) -> list[dict]:
    """Liest approval_requests + deal_title für gegebene request_types."""
    db_path = Path(os.environ.get("DB_PATH", "") or (Path(__file__).parent / "workflow_state.db"))
    placeholders = ",".join("?" for _ in request_types)
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""SELECT ar.id, ar.deal_id, ar.request_type, ar.invoice_number,
                       ar.status, ar.revision_count, ar.feedback_history,
                       ar.approved_by, ar.approved_at, ar.created_at,
                       ist.deal_title
                FROM approval_requests ar
                LEFT JOIN invoice_state ist ON ar.deal_id = ist.deal_id
                WHERE ar.request_type IN ({placeholders})
                ORDER BY ar.created_at DESC""",
            request_types,
        ).fetchall()
        con.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB-Fehler: {e}")

    out = []
    for r in rows:
        try:
            history = json.loads(r["feedback_history"] or "[]")
        except Exception:
            history = []
        out.append({
            "id":             r["id"],
            "deal_id":        r["deal_id"],
            "deal_title":     r["deal_title"] or f"Deal #{r['deal_id']}",
            "request_type":   r["request_type"],
            "invoice_number": r["invoice_number"],
            "status":         r["status"],
            "revision_count": r["revision_count"] or 0,
            "feedback_history": history,
            "approved_by":    r["approved_by"],
            "approved_at":    r["approved_at"],
            "created_at":     r["created_at"],
        })
    return out


@app.get("/admin/history/offers", response_class=HTMLResponse)
async def history_offers(request: Request):
    """
    📋 Angebots-History: alle approval_requests vom Typ 'offer'.
    Header: X-Admin-Secret
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    rows = _fetch_history(["offer"])
    return _render_history_html(
        title="📋 Angebots-History",
        nav_active="offers",
        rows=rows,
        subtitle=f"{len(rows)} Einträge",
    )


@app.get("/admin/history/invoices", response_class=HTMLResponse)
async def history_invoices(request: Request):
    """
    🧾 Rechnungs-History: alle approval_requests vom Typ ar_invoice / sr_invoice / sr_storno.
    Header: X-Admin-Secret
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    rows = _fetch_history(["ar_invoice", "sr_invoice", "sr_storno"])
    return _render_history_html(
        title="🧾 Rechnungs-History",
        nav_active="invoices",
        rows=rows,
        subtitle=f"{len(rows)} Einträge",
    )


@app.get("/admin/history/dunning", response_class=HTMLResponse)
async def history_dunning(request: Request):
    """
    🔔 Mahnungs-History: alle approval_requests vom Typ 'dunning'.
    Header: X-Admin-Secret
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    rows = _fetch_history(["dunning"])
    return _render_history_html(
        title="🔔 Mahnungs-History",
        nav_active="dunning",
        rows=rows,
        subtitle=f"{len(rows)} Einträge",
    )


# ─── Lokaler Start ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8000, reload=False)
