#!/usr/bin/env python3
"""
Angebots-Workflow
=================
Verwaltet alles rund um Sevdesk-Angebote (Angebotsentwürfe, Revisionen, Löschung).

Einstiegspunkte:
  - execute_after_change_request(approval_request, feedback_text)
      Wird aufgerufen wenn jemand über den Slack-"Ändern"-Button und Thread-Feedback
      eine Überarbeitung anfordert.
      Ablauf: Agent lädt bestehendes Angebot → wendet Änderungen an → neuer Entwurf
              → alte Slack-Nachricht als 'superseded' → neue Slack-Nachricht mit Buttons.

History-Typ in approval_requests: request_type = 'offer'
Sichtbar unter: GET /admin/history/offers

Hinweis: Die initiale Angebotserstellung läuft über agent.py (process_new_deal).
"""

import json
import re

from pipedrive_tools import PIPEDRIVE_TOOL_DEFINITIONS, PIPEDRIVE_TOOL_MAP
from sevdesk_tools   import SEVDESK_TOOL_DEFINITIONS,   SEVDESK_TOOL_MAP
from agent_runner    import run_agent
from state_manager   import (
    get_invoice_state,
    add_agent_note,
    create_approval_request,
    supersede_request,
    append_feedback,
    append_history_event,
)
from slack_approval  import (
    send_revised_approval_request,
    post_status_update,
)
from audit_logger    import log_approval_event

# Angebots-Workflow nutzt nur Pipedrive + Sevdesk-Angebots-Tools (keine Invoice-Tools)
OFFER_TOOLS    = PIPEDRIVE_TOOL_DEFINITIONS + SEVDESK_TOOL_DEFINITIONS
OFFER_TOOL_MAP = {**PIPEDRIVE_TOOL_MAP, **SEVDESK_TOOL_MAP}

OFFER_SYSTEM_PROMPT = """Du bist ein B2B-Vertriebsassistent für Angebotserstellung und -überarbeitung.

WICHTIGE REGELN:
1. Erstelle Angebote NUR als Entwurf (Status 100 in Sevdesk).
2. Sende NIEMALS ein Angebot ohne vorherige menschliche Freigabe.
3. Bei Überarbeitungen: Lade das bestehende Angebot zuerst und übernimm alle
   unveränderten Positionen – ändere NUR was explizit angefragt wurde.
4. Bei Löschanfragen: Nutze sevdesk_delete_offer – nur Entwürfe können gelöscht werden.
5. Gib am Ende eine strukturierte JSON-Zusammenfassung zurück.

Sprache: Deutsch. Ton: professionell, präzise."""


# ─── Angebotsüberarbeitung nach Änderungswunsch ───────────────────────────────

def execute_after_change_request(approval_request: dict, feedback_text: str) -> dict:
    """
    Wird aufgerufen wenn jemand über Slack Feedback zu einem Angebot gibt.

    Ablauf:
    1. Claude-Agent lädt das bestehende Angebot + wendet Änderungen an
    2. Erstellt einen neuen überarbeiteten Entwurf in Sevdesk
    3. Postet neue Slack-Nachricht mit frischen Freigabe-Buttons [✅][✏️][🔗]
    4. Alte Freigabeanfrage wird als 'superseded' geschlossen

    Sonderfall Löschung:
    Wenn der Agent sevdesk_delete_offer aufruft (User bat um Löschung), wird
    kein neuer Approval Request erstellt, sondern nur History + Status-Update.

    Args:
        approval_request: Datensatz aus approval_requests (deal_id, invoice_id etc.)
        feedback_text:    Änderungswunsch-Text aus dem Slack-Thread

    Returns:
        {"success": bool, "action": str, ...}
    """
    old_request_id = approval_request["id"]
    deal_id        = approval_request.get("deal_id", 0)
    old_invoice_id = approval_request.get("invoice_id", "")
    revision_count = (approval_request.get("revision_count") or 0) + 1

    print(f"\n{'='*60}")
    print(f"✏️  ANGEBOT ÄNDERUNGSWUNSCH: Deal {deal_id} | Revision {revision_count}")
    print(f"   Feedback: {feedback_text[:120]}")
    print(f"{'='*60}")

    state = get_invoice_state(deal_id) if deal_id else {}

    # Feedback in History speichern (bevor Agent läuft)
    append_feedback(
        old_request_id,
        user_id=approval_request.get("changes_requested_by", ""),
        feedback_text=feedback_text,
    )
    add_agent_note(
        deal_id,
        f"Angebotsänderung angefragt (Rev. {revision_count}): {feedback_text[:200]}"
    )

    initial_message = f"""Ein bestehendes Angebot soll überarbeitet werden.

Deal-ID: {deal_id}
Aktuelles Angebot (Sevdesk-ID): {old_invoice_id}
Bekannter State:
{json.dumps(state or {}, indent=2, ensure_ascii=False)}

Änderungswünsche:
{feedback_text}

Aufgabe:
1. Lade das bestehende Angebot via sevdesk_get_offer(order_id="{old_invoice_id}")
   → Lies Positionen, Preise, Betreff und Kontakt-ID
2. Lade Deal-Details aus Pipedrive (deal_id: {deal_id}) falls nötig
3. Falls der Nutzer das Angebot LÖSCHEN möchte:
   → Nutze sevdesk_delete_offer(order_id="{old_invoice_id}")
   → Gib danach nur eine kurze Bestätigung zurück
4. Ansonsten: Erstelle ein NEUES überarbeitetes Angebot via sevdesk_create_offer_draft
   → Wende ALLE Änderungswünsche präzise an
   → Behalte unveränderte Positionen
   → Verwende denselben Kontakt wie das alte Angebot
5. Gib am Ende eine strukturierte JSON-Zusammenfassung zurück:
{{
  "contact_id": "...",
  "contact_name": "...",
  "contact_email": "...",
  "new_order_id": "...",
  "new_order_number": "...",
  "new_sevdesk_link": "...",
  "total_amount": 0.0,
  "deal_title": "...",
  "changes_made": "Kurze Beschreibung was geändert wurde"
}}

Wichtig: Nur Entwurf erstellen – nicht versenden!
"""

    result = run_agent(
        system_prompt=OFFER_SYSTEM_PROMPT,
        initial_message=initial_message,
        tools=OFFER_TOOLS,
        tool_map=OFFER_TOOL_MAP,
        workflow_type="offer_revision",
        deal_id=deal_id,
        max_turns=12,
    )

    if result["error"]:
        post_status_update(
            f"❌ Angebotsüberarbeitung fehlgeschlagen (Rev. {revision_count}): {result['error']}"
        )
        return {"success": False, "deal_id": deal_id, "error": result["error"]}

    c = result["tool_results"]

    # ── Sonderfall: Angebot wurde gelöscht ────────────────────────────────────
    if c.get("offer_deleted"):
        deleted_number = c.get("deleted_order_number", old_invoice_id)
        requester_id   = approval_request.get("changes_requested_by", "")

        append_history_event(
            old_request_id,
            event_type="deleted",
            user_id=requester_id,
            note=f"Angebot {deleted_number} auf Nutzeranfrage geloescht.",
        )
        add_agent_note(
            deal_id,
            f"Angebot {deleted_number} (ID: {old_invoice_id}) geloescht."
        )
        supersede_request(old_request_id)

        post_status_update(
            f"🗑️ Angebot {deleted_number} wurde gelöscht. "
            f"Kein offenes Angebot mehr für Deal #{deal_id}."
        )
        print(f"   🗑️  Angebot {deleted_number} gelöscht + History geloggt.")
        return {
            "success":              True,
            "action":               "offer_deleted",
            "deal_id":              deal_id,
            "deleted_order_id":     old_invoice_id,
            "deleted_order_number": deleted_number,
        }

    # ── Überarbeitetes Angebot ────────────────────────────────────────────────
    summary_data: dict = {}
    try:
        json_match = re.search(r'\{[\s\S]*\}', result["summary"])
        if json_match:
            summary_data = json.loads(json_match.group())
    except Exception:
        pass

    new_order_id     = summary_data.get("new_order_id")     or c.get("offer_id", "")
    new_order_number = summary_data.get("new_order_number") or c.get("offer_number", "")
    new_sevdesk_link = summary_data.get("new_sevdesk_link") or c.get("offer_link", "")
    total_amount     = float(summary_data.get("total_amount") or c.get("deal_value") or 0)
    contact_name     = summary_data.get("contact_name")     or c.get("contact_name", "")
    contact_email    = summary_data.get("contact_email")    or c.get("contact_email", "")
    deal_title       = summary_data.get("deal_title")       or c.get("deal_title", f"Deal #{deal_id}")
    changes_made     = summary_data.get("changes_made")     or feedback_text[:150]

    if not new_order_id:
        post_status_update(
            "❌ Überarbeitetes Angebot nicht erstellt – bitte manuell prüfen."
        )
        return {"success": False, "deal_id": deal_id, "error": "Kein neues Angebot im Ergebnis"}

    # Alte Anfrage schließen, neue Anfrage mit request_type='offer' anlegen
    supersede_request(old_request_id)

    notify_p1 = bool(approval_request.get("notify_person1", 1))
    notify_p2 = bool(approval_request.get("notify_person2", 1))

    new_req = create_approval_request(
        request_type="offer",       # ← immer 'offer' für Angebots-Workflow
        deal_id=deal_id,
        invoice_id=new_order_id,
        invoice_number=new_order_number,
        notify_person1=notify_p1,
        notify_person2=notify_p2,
    )

    # revision_count in neuem Request speichern
    from state_manager import _conn, _now
    with _conn() as con:
        con.execute(
            "UPDATE approval_requests SET revision_count=?, updated_at=? WHERE id=?",
            (revision_count, _now(), new_req["id"])
        )

    # Neue Slack-Nachricht mit Buttons
    send_revised_approval_request(
        request_id=new_req["id"],
        request_type="offer",
        deal_title=deal_title,
        invoice_number=new_order_number,
        invoice_amount=total_amount,
        sevdesk_link=new_sevdesk_link,
        contact_name=contact_name,
        contact_email=contact_email,
        revision_count=revision_count,
        feedback_summary=changes_made,
        notify_person1=notify_p1,
        notify_person2=notify_p2,
    )

    add_agent_note(
        deal_id,
        f"Angebotsrevision {revision_count} erstellt: {new_order_number} "
        f"(ID: {new_order_id}), Freigabe ausstehend"
    )
    log_approval_event(
        "requested", new_req["id"], deal_id,
        f"offer_revision_{revision_count}"
    )

    print(f"   ✅ Revision {revision_count} erstellt: {new_order_number} → {new_sevdesk_link}")
    return {
        "success":          True,
        "action":           "offer_revised",
        "new_request_id":   new_req["id"],
        "revised_offer_id": new_order_id,
        "revision_count":   revision_count,
    }
