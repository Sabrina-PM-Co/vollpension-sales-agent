#!/usr/bin/env python3
"""
Rechnungs-Workflow (AR + SR)
=============================
Verwaltet Anzahlungsrechnungen (AR) und Schlussrechnungen (SR).

Einstiegspunkte:
  1. process_deal_won(deal_id, deal_value)
     → Trigger: Deal-Status wechselt zu 'Won' in Pipedrive
     → Bei Betrag > €1.000: AR über 50% erstellen + Slack-Freigabe
     → Bei Betrag ≤ €1.000: Nur State merken, keine AR

  2. process_final_invoice(deal_id)
     → Trigger: 1 Tag nach 'Event Datum' Feld in Pipedrive (via Pipedrive Automation)
     → Prüft AR-Status in Sevdesk
     → Erstellt SR (50% oder 100%, mit/ohne Storno)
     → Slack-Freigabe durch 1 von 2 Personen (OR-Logik)

  3. execute_after_approval(approval_request)
     → Nach Freigabe in Slack: Rechnung/Mahnung tatsächlich versenden

Designprinzip: Agent bereitet vor. Mensch entscheidet via Slack. Erst nach Freigabe sendet Agent.

History-Typ in approval_requests: request_type IN ('ar_invoice', 'sr_invoice', 'sr_storno')
Sichtbar unter: GET /admin/history/invoices

Andere Workflows:
  - Angebote:  offer_workflow.py
  - Mahnungen: dunning_workflow.py
  - Shared:    agent_runner.py
"""

import json
import re

from pipedrive_tools       import PIPEDRIVE_TOOL_DEFINITIONS, PIPEDRIVE_TOOL_MAP
from sevdesk_tools         import SEVDESK_TOOL_DEFINITIONS,   SEVDESK_TOOL_MAP
from sevdesk_invoice_tools import (
    SEVDESK_INVOICE_TOOL_DEFINITIONS,
    SEVDESK_INVOICE_TOOL_MAP,
    sevdesk_send_invoice,
    sevdesk_cancel_invoice,
    sevdesk_send_dunning,
)
from agent_runner import run_agent
from state_manager import (
    upsert_invoice_state,
    get_invoice_state,
    add_agent_note,
    create_approval_request,
)
from slack_approval import (
    send_approval_request,
    post_status_update,
)
from audit_logger import log_approval_event

INVOICE_TOOLS = (
    PIPEDRIVE_TOOL_DEFINITIONS
    + SEVDESK_TOOL_DEFINITIONS
    + SEVDESK_INVOICE_TOOL_DEFINITIONS
)
INVOICE_TOOL_MAP = {
    **PIPEDRIVE_TOOL_MAP,
    **SEVDESK_TOOL_MAP,
    **SEVDESK_INVOICE_TOOL_MAP,
}

INVOICE_SYSTEM_PROMPT = """Du bist ein präziser Rechnungsassistent. Deine Aufgabe ist es,
Rechnungsdaten zu analysieren, Sevdesk-Rechnungen zu erstellen (als Entwurf),
und den Sachverhalt für die menschliche Freigabe aufzubereiten.

WICHTIGE REGELN:
1. Erstelle Rechnungen NUR als Entwurf (status 100 in Sevdesk).
2. Sende NIEMALS eine Rechnung ohne vorherige menschliche Freigabe.
3. Prüfe IMMER zuerst ob eine Anzahlungsrechnung existiert und ob sie bezahlt wurde.
4. Bei Storno: Erkläre klar warum storniert wird und was die Konsequenzen sind.
5. Gib am Ende eine strukturierte Zusammenfassung zurück (JSON-kompatibel).

Rechnungslogik:
- Deal-Betrag > €1.000: AR über 50%, später SR über restliche 50%
- Deal-Betrag ≤ €1.000: Keine AR, SR über 100%
- AR vorhanden + bezahlt → SR über restliche 50%
- AR vorhanden + NICHT bezahlt + versendet → Storno AR + SR über 100% (nur nach Freigabe!)
- AR vorhanden + Entwurf (nicht versendet) → AR löschen/ignorieren, SR über 100%

Sprache: Deutsch. Ton: professionell, präzise."""


# ─── Trigger 1: Deal gewonnen ─────────────────────────────────────────────────

def process_deal_won(deal_id: int, deal_value: float) -> dict:
    """
    Wird aufgerufen wenn ein Pipedrive-Deal auf 'gewonnen' gesetzt wird.

    Bei Betrag > €1.000: Anzahlungsrechnung über 50% erstellen.
    Bei Betrag ≤ €1.000: Nur State speichern, keine AR.
    """
    print(f"\n{'='*60}")
    print(f"💰 TRIGGER 1: Deal gewonnen – ID {deal_id}, Betrag €{deal_value:,.2f}")
    print(f"{'='*60}")

    upsert_invoice_state(deal_id, deal_value=deal_value, phase="deal_won")

    if deal_value <= 1000.0:
        upsert_invoice_state(deal_id, ar_status="none", phase="awaiting_event")
        add_agent_note(
            deal_id,
            f"Betrag €{deal_value} ≤ €1.000 → keine AR, warte auf Event Datum"
        )
        print(f"   ℹ️  Betrag ≤ €1.000 – keine Anzahlungsrechnung.")
        return {"success": True, "action": "no_ar_needed", "deal_id": deal_id}

    ar_amount = round(deal_value * 0.5, 2)
    add_agent_note(deal_id, f"Betrag €{deal_value} > €1.000 → AR über €{ar_amount}")

    result = run_agent(
        system_prompt=INVOICE_SYSTEM_PROMPT,
        initial_message=f"""Deal ID {deal_id} wurde gewonnen. Deal-Betrag: €{deal_value:.2f}.

Aufgabe:
1. Lade Deal-Details aus Pipedrive (deal_id: {deal_id})
2. Lade Kontaktperson und Firma
3. Suche oder lege Kontakt in Sevdesk an
4. Erstelle eine Anzahlungsrechnung (invoice_type='AR') über €{ar_amount:.2f}
   (= 50% von €{deal_value:.2f}) als Entwurf in Sevdesk
5. Gib am Ende eine Zusammenfassung zurück mit: contact_id_sdsk, ar_invoice_id,
   ar_invoice_number, ar_amount, sevdesk_link, contact_email, contact_name, deal_title
""",
        tools=INVOICE_TOOLS,
        tool_map=INVOICE_TOOL_MAP,
        workflow_type="ar_invoice",
        deal_id=deal_id,
    )

    if result["error"]:
        return {"success": False, "deal_id": deal_id, "error": result["error"]}

    c = result["tool_results"]

    upsert_invoice_state(
        deal_id,
        contact_id_sdsk=c.get("contact_id_sdsk", ""),
        contact_email=c.get("contact_email", ""),
        deal_title=c.get("deal_title", ""),
        ar_sevdesk_id=c.get("ar_invoice_id", ""),
        ar_invoice_number=c.get("ar_invoice_number", ""),
        ar_amount=ar_amount,
        ar_status="draft",
        phase="ar_pending_approval",
    )

    req = create_approval_request(
        request_type="ar_invoice",
        deal_id=deal_id,
        invoice_id=c.get("ar_invoice_id", ""),
        invoice_number=c.get("ar_invoice_number", ""),
        notify_person1=True,
        notify_person2=True,
    )

    send_approval_request(
        request_id=req["id"],
        request_type="ar_invoice",
        deal_title=c.get("deal_title", f"Deal #{deal_id}"),
        invoice_number=c.get("ar_invoice_number", "–"),
        invoice_amount=ar_amount,
        sevdesk_link=c.get("ar_sevdesk_link", ""),
        contact_name=c.get("contact_name", ""),
        contact_email=c.get("contact_email", ""),
    )

    log_approval_event("requested", req["id"], deal_id, "ar_invoice")
    print(f"   ✅ AR-Entwurf erstellt, Slack-Freigabe angefordert.")
    return {
        "success":               True,
        "action":                "ar_draft_created",
        "deal_id":               deal_id,
        "ar_invoice_id":         c.get("ar_invoice_id"),
        "ar_invoice_number":     c.get("ar_invoice_number"),
        "approval_request_id":   req["id"],
    }


# ─── Trigger 2: Event Datum + 1 Tag ───────────────────────────────────────────

def process_final_invoice(deal_id: int) -> dict:
    """
    Wird 1 Tag nach dem 'Event Datum' Pipedrive-Feld aufgerufen.

    Logik:
    - Keine AR → SR über 100%
    - AR vorhanden + bezahlt → SR über 50% mit Referenz auf AR
    - AR vorhanden + offen + versendet → SONDERFALL: Storno-Empfehlung
    - AR im Entwurf (nie gesendet) → ignorieren, SR über 100%
    """
    print(f"\n{'='*60}")
    print(f"📅 TRIGGER 2: Event vorbei – Schlussrechnung für Deal {deal_id}")
    print(f"{'='*60}")

    state = get_invoice_state(deal_id)

    result = run_agent(
        system_prompt=INVOICE_SYSTEM_PROMPT,
        initial_message=f"""Deal ID {deal_id}: Das Event hat stattgefunden (gestern).
Erstelle jetzt die Schlussrechnung.

Bekannter State:
{json.dumps(state or {}, indent=2, ensure_ascii=False)}

Aufgabe:
1. Lade Deal-Details aus Pipedrive (deal_id: {deal_id})
2. Lade alle Rechnungen des Kontakts aus Sevdesk: sevdesk_get_invoices_for_contact
3. Prüfe ob eine Anzahlungsrechnung (AR) existiert:
   a. Keine AR → Schlussrechnung über 100% des Deal-Betrags erstellen
   b. AR vorhanden → Prüfe Zahlungsstatus via sevdesk_check_payment_status:
      - Bezahlt (status 2000) → SR über 50% erstellen, Referenz auf AR
      - Entwurf (status 100, nie versendet) → SR über 100% erstellen
      - Versendet (status 200) aber NICHT bezahlt → SONDERFALL:
        Erstelle SR-Entwurf über 100% OHNE Storno. Kennzeichne als sonderfall=true.

4. Erstelle die Schlussrechnung als Entwurf (invoice_type='RE')
5. Gib strukturierte Zusammenfassung zurück:
{{
  "sonderfall": bool,
  "ar_invoice_id": str_oder_null,
  "ar_paid": bool,
  "sr_invoice_id": str,
  "sr_invoice_number": str,
  "sr_amount": float,
  "sr_sevdesk_link": str,
  "contact_id_sdsk": str,
  "contact_email": str,
  "contact_name": str,
  "deal_title": str,
  "deal_value": float,
  "warning_text": str
}}
""",
        tools=INVOICE_TOOLS,
        tool_map=INVOICE_TOOL_MAP,
        workflow_type="sr_invoice",
        deal_id=deal_id,
    )

    if result["error"]:
        return {"success": False, "deal_id": deal_id, "error": result["error"]}

    c = result["tool_results"]

    summary_data: dict = {}
    try:
        json_match = re.search(r'\{[\s\S]*\}', result["summary"])
        if json_match:
            summary_data = json.loads(json_match.group())
    except Exception:
        pass

    is_sonderfall = summary_data.get("sonderfall", False)
    ar_invoice_id = summary_data.get("ar_invoice_id") or c.get("ar_invoice_id")
    sr_invoice_id = summary_data.get("sr_invoice_id") or c.get("sr_invoice_id", "")
    sr_number     = summary_data.get("sr_invoice_number") or c.get("sr_invoice_number", "")
    sr_amount     = summary_data.get("sr_amount", 0)
    sr_link       = summary_data.get("sr_sevdesk_link") or c.get("sr_sevdesk_link", "")
    contact_email = summary_data.get("contact_email") or c.get("contact_email", "")
    contact_name  = summary_data.get("contact_name") or c.get("contact_name", "")
    deal_title    = summary_data.get("deal_title") or c.get("deal_title", f"Deal #{deal_id}")
    warning_text  = summary_data.get("warning_text", "")

    upsert_invoice_state(
        deal_id,
        contact_email=contact_email,
        sr_sevdesk_id=sr_invoice_id,
        sr_invoice_number=sr_number,
        sr_amount=sr_amount,
        sr_status="draft",
        phase="sr_pending_approval",
    )

    req_type  = "sr_storno" if is_sonderfall else "sr_invoice"
    notify_p1 = not is_sonderfall
    notify_p2 = True

    if is_sonderfall and not warning_text:
        ar_num = state.get("ar_invoice_number", ar_invoice_id) if state else ar_invoice_id
        warning_text = (
            f"Anzahlungsrechnung {ar_num} wurde versendet aber nicht bezahlt. "
            f"Agent-Empfehlung: AR stornieren + SR über 100% versenden."
        )

    req = create_approval_request(
        request_type=req_type,
        deal_id=deal_id,
        invoice_id=sr_invoice_id,
        invoice_number=sr_number,
        notify_person1=notify_p1,
        notify_person2=notify_p2,
    )

    send_approval_request(
        request_id=req["id"],
        request_type=req_type,
        deal_title=deal_title,
        invoice_number=sr_number,
        invoice_amount=sr_amount,
        sevdesk_link=sr_link,
        contact_name=contact_name,
        contact_email=contact_email,
        notify_person1=notify_p1,
        notify_person2=notify_p2,
        warning_text=warning_text,
    )

    add_agent_note(
        deal_id,
        f"SR-Entwurf erstellt ({sr_number}), Freigabe angefordert. Sonderfall: {is_sonderfall}"
    )
    log_approval_event("requested", req["id"], deal_id, req_type)

    print(f"   ✅ SR-Entwurf erstellt, Slack-Freigabe angefordert (Sonderfall: {is_sonderfall})")
    return {
        "success":             True,
        "action":              "sr_draft_created",
        "deal_id":             deal_id,
        "sonderfall":          is_sonderfall,
        "sr_invoice_id":       sr_invoice_id,
        "sr_invoice_number":   sr_number,
        "approval_request_id": req["id"],
    }


# ─── Post-Freigabe: Versand ausführen ─────────────────────────────────────────

def execute_after_approval(approval_request: dict) -> dict:
    """
    Wird aufgerufen nachdem eine Freigabe über Slack erteilt wurde.
    Führt den tatsächlichen Versand aus (Rechnung oder Mahnung).

    Args:
        approval_request: Datensatz aus approval_requests-Tabelle

    Returns:
        {"success": bool, "action": str, "message": str}
    """
    req_type   = approval_request["request_type"]
    invoice_id = approval_request["invoice_id"]
    deal_id    = approval_request.get("deal_id", 0)
    state      = get_invoice_state(deal_id) if deal_id else {}
    contact_email = state.get("contact_email", "") if state else ""

    print(f"\n🚀 Post-Freigabe: {req_type} | Invoice: {invoice_id}")

    try:
        approved_by = approval_request.get("approved_by", "")

        if req_type == "ar_invoice":
            sevdesk_send_invoice(invoice_id, contact_email)
            upsert_invoice_state(deal_id, ar_status="sent")
            add_agent_note(deal_id, "AR versendet nach Slack-Freigabe")
            log_approval_event("sent", approval_request["id"], deal_id, req_type, approved_by)
            post_status_update(
                f"✅ Anzahlungsrechnung {approval_request['invoice_number']} "
                f"versendet an {contact_email}"
            )
            return {"success": True, "action": "ar_sent"}

        elif req_type == "sr_invoice":
            sevdesk_send_invoice(invoice_id, contact_email)
            upsert_invoice_state(deal_id, sr_status="sent", phase="completed")
            add_agent_note(deal_id, "SR versendet nach Slack-Freigabe")
            log_approval_event("sent", approval_request["id"], deal_id, req_type, approved_by)
            post_status_update(
                f"✅ Schlussrechnung {approval_request['invoice_number']} "
                f"versendet an {contact_email}"
            )
            return {"success": True, "action": "sr_sent"}

        elif req_type == "sr_storno":
            ar_id = state.get("ar_sevdesk_id") if state else ""
            if ar_id:
                sevdesk_cancel_invoice(ar_id)
                upsert_invoice_state(deal_id, ar_status="cancelled")
                add_agent_note(
                    deal_id,
                    f"AR {state.get('ar_invoice_number')} storniert nach Freigabe"
                )
                log_approval_event(
                    "sent", approval_request["id"], deal_id, "storno",
                    approved_by, "AR storniert"
                )
                post_status_update(
                    f"🗑️ Anzahlungsrechnung {state.get('ar_invoice_number')} storniert"
                )
            sevdesk_send_invoice(invoice_id, contact_email)
            upsert_invoice_state(deal_id, sr_status="sent", phase="completed")
            add_agent_note(deal_id, "SR (100%) versendet nach Storno-Freigabe")
            log_approval_event(
                "sent", approval_request["id"], deal_id, req_type,
                approved_by, "SR nach Storno"
            )
            post_status_update(
                f"✅ Schlussrechnung {approval_request['invoice_number']} (100%) "
                f"versendet an {contact_email}"
            )
            return {"success": True, "action": "storno_and_sr_sent"}

        elif req_type == "dunning":
            sevdesk_send_dunning(invoice_id, contact_email)
            add_agent_note(deal_id, f"Mahnung {approval_request['invoice_number']} versendet")
            log_approval_event("sent", approval_request["id"], deal_id, req_type, approved_by)
            post_status_update(
                f"🔔 Mahnung {approval_request['invoice_number']} versendet an {contact_email}"
            )
            return {"success": True, "action": "dunning_sent"}

        else:
            return {"success": False, "action": "unknown_type", "message": req_type}

    except Exception as e:
        error_msg = f"Fehler beim Versand: {e}"
        add_agent_note(deal_id, f"FEHLER: {error_msg}")
        log_approval_event(
            "failed", approval_request.get("id"), deal_id, req_type,
            note=error_msg[:200]
        )
        post_status_update(f"❌ {error_msg}")
        return {"success": False, "action": "error", "message": error_msg}
