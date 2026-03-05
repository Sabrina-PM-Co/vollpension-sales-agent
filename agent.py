#!/usr/bin/env python3
"""
Angebots-Agent – Vollpension Generationendialog GmbH
=====================================================
Verarbeitet neue Deals aus der Pipeline "Product Sales", Stage "Anfragen".

Ablauf:
  1. Deal + Kontaktdaten + Deal-Notizen aus Pipedrive laden
  2. Pricing Engine kalkuliert Positionen deterministisch
  3. Bei unbekanntem Interessensgebiet: Interpretationsversuch aus Freitext
  4. Sevdesk-Kontakt suchen / anlegen
  5. Angebotsentwurf in Sevdesk erstellen
  6. Slack-Nachricht mit Freigabe-Buttons senden
  7. Pipedrive-Deal mit Angebot-ID/-Link aktualisieren
"""

import json
import re
import anthropic

from config          import ANTHROPIC_API_KEY, PIPEDRIVE_STAGE_IN_BEARBEITUNG
from pipedrive_tools import PIPEDRIVE_TOOL_DEFINITIONS, PIPEDRIVE_TOOL_MAP
from sevdesk_tools   import SEVDESK_TOOL_DEFINITIONS,   SEVDESK_TOOL_MAP
from agent_runner    import run_agent
from pricing_engine  import calculate_offer_positions
from pipedrive_fields import (
    FIELD_INTERESSENSGEBIET,
    FIELD_BOT_ANGEBOT_ID, FIELD_BOT_ANGEBOT_LINK,
    get_interessensgebiet_ids,
)
from state_manager   import create_approval_request, add_agent_note, get_or_create_invoice_state
from slack_approval  import send_approval_request, post_status_update
from audit_logger    import start_run, finish_run

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

OFFER_TOOLS    = PIPEDRIVE_TOOL_DEFINITIONS + SEVDESK_TOOL_DEFINITIONS
OFFER_TOOL_MAP = {**PIPEDRIVE_TOOL_MAP, **SEVDESK_TOOL_MAP}


# ─── System Prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Du bist ein B2B-Vertriebsassistent der Vollpension Generationendialog GmbH.
Du erstellst automatisch Angebotsentwürfe für neue Anfragen aus Pipedrive.

═══════════════════════════════════════════════════════════
SCHRITT-FÜR-SCHRITT WORKFLOW
═══════════════════════════════════════════════════════════

1. DEAL LADEN
   → pipedrive_get_deal(deal_id)
   → pipedrive_get_deal_notes(deal_id)  ← Kundenanfrage-Freitext!
   → pipedrive_get_person(person_id)
   → pipedrive_get_organization(org_id) falls vorhanden

2. POSITIONEN
   Dir wird bereits eine fertige Positionsliste aus der Pricing Engine übergeben.
   → Prüfe ob die Positionen zur Anfrage passen (Freitext aus Notizen!)
   → Passe Mengen an wenn in der Notiz explizit andere Zahlen stehen
   → Bei unklarer Anfrage: erstelle trotzdem einen Entwurf mit Richtwerten +
     notiere Rückfragen in einem Hinweis-Abschnitt

3. SEVDESK-KONTAKT
   → sevdesk_search_contact(name/email) – Duplikat-Check!
   → Falls nicht gefunden: sevdesk_create_contact(...)

4. ANGEBOT ERSTELLEN
   → sevdesk_create_offer_draft(contact_id, positions, subject, ...)
   → NUR als Entwurf (Status 100) – NIEMALS versenden!
   → Betreff: "[Kategorie] | [Firmenname]" z.B. "Buchtelmobil | MSD"

5. PIPEDRIVE AKTUALISIEREN
   → pipedrive_update_deal mit:
     - "{bot_angebot_id}": sevdesk_order_id
     - "{bot_angebot_link}": sevdesk_link
     - "stage_id": {stage_in_bearbeitung}    ← Deal in Stage "In Bearbeitung" schieben

6. JSON-ZUSAMMENFASSUNG ausgeben:
{{
  "contact_id": "...",
  "contact_name": "...",
  "contact_email": "...",
  "order_id": "...",
  "order_number": "...",
  "sevdesk_link": "...",
  "total_net": 0.0,
  "deal_title": "...",
  "anfrage_typ": "bekannt|generisch|unvollstaendig",
  "hinweise_fuer_team": "Offene Fragen oder Interpretationen hier eintragen"
}}

═══════════════════════════════════════════════════════════
UMGANG MIT GENERISCHEN / UNVOLLSTÄNDIGEN ANFRAGEN
═══════════════════════════════════════════════════════════

Wenn kein Interessensgebiet gesetzt ist oder die Anfrage keiner Kategorie
eindeutig zugeordnet werden kann:

a) Lies den Freitext aus den Deal-Notizen sorgfältig
b) Versuche die Kategorie zu erkennen (Buchtelmobil, Backkurs, Studio, etc.)
c) Erstelle einen Entwurf mit Richtwert-Positionen für die wahrscheinlichste Kategorie
d) Setze anfrage_typ = "generisch" und trage in hinweise_fuer_team ein:
   - Deine Interpretation ("Ich interpretiere dies als Buchtelmobil-Anfrage, weil...")
   - Fehlende Infos ("Unklar: Datum, Personenanzahl, Ort")
   - Rückfragen ("Ist Ganztags oder Halbtags gewünscht?")

Erstelle IMMER einen Entwurf – auch wenn Infos fehlen.
Das Team kann im Slack-Approval-Thread korrigieren.

═══════════════════════════════════════════════════════════
REGELN
═══════════════════════════════════════════════════════════
- Erstelle Angebote ausschließlich als ENTWURF (Status 100)
- Sende NIEMALS ohne Freigabe
- MwSt: Lebensmittel (Buchteln, Torten) 10% – Dienstleistungen 20%
- Währung: EUR
- Sprache Angebot: Deutsch
- Gültigkeitsdauer: 30 Tage ab heute
""".format(
    bot_angebot_id=FIELD_BOT_ANGEBOT_ID,
    bot_angebot_link=FIELD_BOT_ANGEBOT_LINK,
    stage_in_bearbeitung=PIPEDRIVE_STAGE_IN_BEARBEITUNG,
)


# ─── Haupt-Funktion ───────────────────────────────────────────────────────────

def process_new_deal(deal_id: int, deal_data: dict | None = None) -> dict:
    """
    Verarbeitet einen neuen Deal aus der Pipedrive-Stage "Anfragen".

    Args:
        deal_id:   Pipedrive Deal-ID aus dem Webhook
        deal_data: Optional vorgeladene Deal-Daten (z.B. aus Webhook-Payload)

    Returns:
        {"success": bool, "deal_id": int, "offer_number": str,
         "sevdesk_link": str, "request_id": int, "error": str|None}
    """
    print(f"\n{'='*60}")
    print(f"🚀 Neuer Deal: {deal_id}")
    print(f"{'='*60}")

    run_id = start_run(workflow_type="offer", deal_id=deal_id, model="claude-opus-4-5-20251101")

    # ── Pricing Engine: Positionen vorberechnen ────────────────────────────
    pricing_result = {"positions": [], "hinweise": ["Deal-Daten noch nicht geladen"]}
    if deal_data:
        try:
            pricing_result = calculate_offer_positions(deal_data)
            print(f"   💶 Pricing Engine: {len(pricing_result['positions'])} Positionen, "
                  f"Netto {pricing_result['total_net']} EUR")
        except Exception as pe:
            pricing_result["hinweise"].append(f"Pricing Engine Fehler: {pe}")

    hat_bekanntes_interesse = bool(
        deal_data and get_interessensgebiet_ids(deal_data.get(FIELD_INTERESSENSGEBIET))
    )

    # ── Initiale Nachricht an Agent ────────────────────────────────────────
    positionen_json = json.dumps(pricing_result["positions"], indent=2, ensure_ascii=False)
    pricing_hinweise = "\n".join(f"- {h}" for h in pricing_result["hinweise"]) or "– keine –"

    initial_message = f"""Neuer Deal eingegangen!

Deal-ID: {deal_id}
Bekanntes Interessensgebiet: {"ja" if hat_bekanntes_interesse else "NEIN – generische Anfrage, bitte aus Notizen ableiten"}

═══════ VORBERECHNETE POSITIONEN (Pricing Engine) ═══════
{positionen_json}

═══════ HINWEISE DER PRICING ENGINE ═══════
{pricing_hinweise}

═══════ DEINE AUFGABE ═══════
1. Lade Deal-Details, Notizen, Kontaktperson, Organisation aus Pipedrive
2. Prüfe ob die vorberechneten Positionen zur Anfrage passen
   → Passe Mengen/Positionen an wenn der Freitext andere Details nennt
   {"→ Kein Interessensgebiet gesetzt: Interpretiere den Freitext und erstelle Richtwert-Angebot!" if not hat_bekanntes_interesse else ""}
3. Suche/erstelle Sevdesk-Kontakt
4. Erstelle Angebotsentwurf mit den (ggf. angepassten) Positionen
5. Aktualisiere Deal in Pipedrive
6. Gib JSON-Zusammenfassung aus

Wichtig: Nur Entwurf – nicht versenden!
"""

    # ── Agent laufen lassen ────────────────────────────────────────────────
    result = run_agent(
        system_prompt=SYSTEM_PROMPT,
        initial_message=initial_message,
        tools=OFFER_TOOLS,
        tool_map=OFFER_TOOL_MAP,
        workflow_type="offer",
        deal_id=deal_id,
        max_turns=15,
    )

    finish_run(run_id, status="completed" if not result["error"] else "failed",
               error=result.get("error"))

    if result["error"]:
        post_status_update(f"❌ Angebotserstellung fehlgeschlagen für Deal #{deal_id}: {result['error']}")
        return {"success": False, "deal_id": deal_id, "error": result["error"]}

    # ── Ergebnis parsen ───────────────────────────────────────────────────
    summary_data: dict = {}
    try:
        match = re.search(r'\{[\s\S]*?\}', result["summary"])
        if match:
            summary_data = json.loads(match.group())
    except Exception:
        pass

    c = result["tool_results"]
    order_id      = summary_data.get("order_id")      or c.get("offer_id", "")
    order_number  = summary_data.get("order_number")  or c.get("offer_number", "")
    sevdesk_link  = summary_data.get("sevdesk_link")  or c.get("offer_link", "")
    total_net     = float(summary_data.get("total_net") or pricing_result["total_net"] or 0)
    contact_name  = summary_data.get("contact_name")  or c.get("contact_name", "")
    contact_email = summary_data.get("contact_email") or c.get("contact_email", "")
    deal_title    = summary_data.get("deal_title")    or c.get("deal_title", f"Deal #{deal_id}")
    anfrage_typ   = summary_data.get("anfrage_typ", "unbekannt")
    team_hinweis  = summary_data.get("hinweise_fuer_team", "")

    if not order_id:
        post_status_update(f"⚠️ Deal #{deal_id}: Angebot nicht erstellt – bitte manuell prüfen.")
        return {"success": False, "deal_id": deal_id, "error": "Kein Angebot im Ergebnis"}

    # ── State & Approval Request anlegen ─────────────────────────────────
    get_or_create_invoice_state(deal_id)
    add_agent_note(deal_id, f"Angebot {order_number} erstellt (ID: {order_id}), Freigabe ausstehend")

    req = create_approval_request(
        request_type="offer",
        deal_id=deal_id,
        invoice_id=order_id,
        invoice_number=order_number,
        notify_person1=True,
        notify_person2=True,
    )

    # ── Slack-Nachricht mit Freigabe-Buttons ──────────────────────────────
    # Generische Anfragen bekommen einen Hinweis-Banner im Slack
    extra_hinweis = ""
    if anfrage_typ in ("generisch", "unvollstaendig") and team_hinweis:
        extra_hinweis = f"\n💡 *Interpretation:* {team_hinweis}"

    send_approval_request(
        request_id=req["id"],
        request_type="offer",
        deal_title=deal_title,
        invoice_number=order_number,
        invoice_amount=total_net,
        sevdesk_link=sevdesk_link,
        contact_name=contact_name,
        contact_email=contact_email,
        extra_text=extra_hinweis,
        notify_person1=True,
        notify_person2=True,
    )

    print(f"   ✅ Angebot {order_number} erstellt → Slack-Freigabe #{req['id']}")
    return {
        "success":      True,
        "deal_id":      deal_id,
        "offer_number": order_number,
        "sevdesk_link": sevdesk_link,
        "request_id":   req["id"],
        "anfrage_typ":  anfrage_typ,
        "error":        None,
    }
