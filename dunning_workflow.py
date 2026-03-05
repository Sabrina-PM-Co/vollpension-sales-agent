#!/usr/bin/env python3
"""
Mahnungs-Workflow
=================
Verwaltet den täglichen Mahnwesen-Check.

Einstiegspunkte:
  - process_dunning_check()
      Täglicher Cron-Job (GET /cron/dunning-check).
      Prüft alle Rechnungen mit Fälligkeit > 14 Tage.
      Erstellt Mahnungsentwürfe in Sevdesk + Slack-Freigabe.

History-Typ in approval_requests: request_type = 'dunning'
Sichtbar unter: GET /admin/history/dunning
"""

import json

from pipedrive_tools          import PIPEDRIVE_TOOL_DEFINITIONS, PIPEDRIVE_TOOL_MAP
from sevdesk_tools            import SEVDESK_TOOL_DEFINITIONS,   SEVDESK_TOOL_MAP
from sevdesk_invoice_tools    import (
    SEVDESK_INVOICE_TOOL_DEFINITIONS,
    SEVDESK_INVOICE_TOOL_MAP,
)
from agent_runner             import run_agent
from state_manager            import (
    create_approval_request,
    get_overdue_for_dunning,
    update_dunning_entry,
)
from slack_approval           import send_approval_request

DUNNING_TOOLS = (
    PIPEDRIVE_TOOL_DEFINITIONS
    + SEVDESK_TOOL_DEFINITIONS
    + SEVDESK_INVOICE_TOOL_DEFINITIONS
)
DUNNING_TOOL_MAP = {
    **PIPEDRIVE_TOOL_MAP,
    **SEVDESK_TOOL_MAP,
    **SEVDESK_INVOICE_TOOL_MAP,
}

DUNNING_SYSTEM_PROMPT = """Du bist ein Mahnungsassistent. Prüfe offene Rechnungen
und erstelle Mahnungen als Entwurf für die Freigabe.

REGELN:
1. Erstelle Mahnung als Entwurf – nie automatisch senden.
2. Prüfe vor Mahnung ob Zahlung in Sevdesk eingegangen ist.
3. Gib eine kurze, sachliche Zusammenfassung für den Slack-Freigabe-Hinweis.

Sprache: Deutsch."""


# ─── Trigger 3: Mahnwesen – Täglicher Cron-Job ───────────────────────────────

def process_dunning_check() -> dict:
    """
    Täglicher Cron-Job: Prüft alle Rechnungen auf Fälligkeit (Datum + 14 Tage).
    Erstellt Mahnungen als Entwurf und fordert Slack-Freigabe an.

    Wird aufgerufen von: GET /cron/dunning-check (intern, gesichert via CRON_SECRET)

    Returns:
        {"success": bool, "processed": int, "details": list}
    """
    print(f"\n{'='*60}")
    print(f"🔔 CRON: Mahnwesen-Check")
    print(f"{'='*60}")

    overdue   = get_overdue_for_dunning()
    processed = []

    for entry in overdue:
        invoice_id     = entry["invoice_sevdesk_id"]
        invoice_number = entry["invoice_number"]
        deal_id        = entry.get("deal_id", 0)
        contact_email  = entry.get("contact_email", "")
        invoice_amount = entry.get("invoice_amount", 0)
        entry_id       = entry["id"]

        print(f"\n   🔍 Prüfe Rechnung {invoice_number} (ID: {invoice_id})...")

        result = run_agent(
            system_prompt=DUNNING_SYSTEM_PROMPT,
            initial_message=f"""Prüfe Rechnung ID {invoice_id} ({invoice_number}) auf Zahlungseingang.

1. Zahlungsstatus prüfen via sevdesk_check_payment_status(invoice_id="{invoice_id}")
2. Wenn NICHT bezahlt: Mahnung erstellen via sevdesk_create_dunning(invoice_id="{invoice_id}")
3. Wenn bezahlt: Nur melden, keine Mahnung.

Gib zurück:
{{
  "is_paid": bool,
  "dunning_id": str_oder_null,
  "dunning_number": str_oder_null,
  "dunning_link": str_oder_null
}}""",
            tools=DUNNING_TOOLS,
            tool_map=DUNNING_TOOL_MAP,
            workflow_type="dunning",
            deal_id=deal_id,
        )

        c = result["tool_results"]

        summary_data: dict = {}
        try:
            import re
            json_match = re.search(r'\{[\s\S]*\}', result["summary"])
            if json_match:
                summary_data = json.loads(json_match.group())
        except Exception:
            pass

        if summary_data.get("is_paid"):
            update_dunning_entry(entry_id, status="paid")
            print(f"   ✅ {invoice_number} bezahlt – kein Mahnbedarf.")
            processed.append({"invoice_id": invoice_id, "action": "paid_no_dunning"})
            continue

        dunning_id     = summary_data.get("dunning_id")     or c.get("dunning_id", "")
        dunning_number = summary_data.get("dunning_number") or c.get("dunning_number", "")
        dunning_link   = summary_data.get("dunning_link")   or c.get("dunning_link", "")

        if not dunning_id:
            print(f"   ⚠️  Mahnung für {invoice_number} konnte nicht erstellt werden.")
            processed.append({"invoice_id": invoice_id, "action": "dunning_failed"})
            continue

        update_dunning_entry(
            entry_id,
            status="approval_pending",
            dunning_id_sdsk=dunning_id,
        )

        req = create_approval_request(
            request_type="dunning",
            deal_id=deal_id,
            invoice_id=dunning_id,
            invoice_number=dunning_number,
            notify_person1=True,
            notify_person2=True,
        )

        send_approval_request(
            request_id=req["id"],
            request_type="dunning",
            deal_title=f"Rechnung {invoice_number}",
            invoice_number=dunning_number,
            invoice_amount=invoice_amount,
            sevdesk_link=dunning_link,
            contact_name="",
            contact_email=contact_email,
        )

        print(f"   📬 Mahnung {dunning_number} erstellt, Slack-Freigabe angefordert.")
        processed.append({
            "invoice_id":  invoice_id,
            "action":      "dunning_created",
            "dunning_id":  dunning_id,
            "approval_id": req["id"],
        })

    return {"success": True, "processed": len(overdue), "details": processed}
