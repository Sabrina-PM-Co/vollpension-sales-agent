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

from config          import ANTHROPIC_API_KEY, PIPEDRIVE_STAGE_IN_BEARBEITUNG, PIPEDRIVE_DOMAIN
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
from slack_approval  import send_approval_request, post_status_update, post_deal_notification
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
        # Suche nach dem LETZTEN/GRÖSSTEN JSON-Block (greedy, von rechts)
        # Der Agent gibt oft zuerst Erklärtext aus, dann das JSON
        decoder = json.JSONDecoder()
        text = result["summary"]
        idx = 0
        while idx < len(text):
            brace = text.find('{', idx)
            if brace == -1:
                break
            try:
                parsed, end_idx = decoder.raw_decode(text, brace)
                if isinstance(parsed, dict) and parsed.get("order_id"):
                    summary_data = parsed
                    break
                # Auch ohne order_id merken, falls nichts Besseres kommt
                if isinstance(parsed, dict) and len(parsed) > len(summary_data):
                    summary_data = parsed
                idx = brace + 1
            except json.JSONDecodeError:
                idx = brace + 1
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
        warning_text=extra_hinweis,
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


# ─── Interim Workflow (Sevdesk vorübergehend nicht verfügbar) ──────────────────

INTERIM_SYSTEM_PROMPT = """Du bist ein B2B-Vertriebsassistent der Vollpension Generationendialog GmbH.
Du bearbeitest neue Deals in Pipedrive – OHNE Sevdesk (vorübergehender Interim-Modus).

═══════════════════════════════════════════════════════════
FIRMEN-KONTEXT (unbedingt beachten!)
═══════════════════════════════════════════════════════════
• Vollpension hat ihren Standort in WIEN und liefert/fährt österreichweit.
• ALLE Einsätze innerhalb Österreichs sind INLAND – auch Graz, Salzburg,
  Linz, Innsbruck, Klagenfurt usw.
• NIEMALS ein Produkt mit „Ausland" oder „International" wählen!
• Sprache der Notizen: Deutsch oder Englisch – beides ist normal.

═══════════════════════════════════════════════════════════
SCHRITT-FÜR-SCHRITT WORKFLOW
═══════════════════════════════════════════════════════════

1. DEAL LADEN
   → pipedrive_get_deal(deal_id)
   → pipedrive_get_deal_notes(deal_id)  ← Kundenanfrage-Freitext!
   → pipedrive_get_person(person_id)
   → pipedrive_get_organization(org_id) falls vorhanden

2. PRODUKTE AUS KATALOG LADEN
   → pipedrive_list_products()
   → Liste alle Produkte auf, lies ihre Namen genau.

3. PASSENDE PRODUKTE HINZUFÜGEN
   Wähle Produkte die zur Anfrage passen (Deal-Titel + Notizen).

   ── TRANSPORTKOSTEN BUCHTELMOBIL ──────────────────────────────
   Ausgangsort: IMMER Wien. Wähle GENAU EINES dieser Produkte:

   • "Transportkosten Buchtelmobil innerhalb Wien"
     → Einsatzort liegt IN Wien (alle Bezirke inkl. Randbezirke)

   • "Transportkosten Buchtelmobil Österreich kleiner 50km"
     → ~0–50 km von Wien: Mödling, Baden, Klosterneuburg,
       Schwechat, Bruck an der Leitha

   • "Transportkosten Buchtelmobil Österreich kleiner 150km"
     → ~50–150 km von Wien: St. Pölten (~65 km), Krems (~80 km),
       Wiener Neustadt (~60 km), Eisenstadt (~55 km), Amstetten (~130 km)

   • "Transportkosten Buchtelmobil Österreich größer 150km"
     → > 150 km von Wien – STANDARD für andere Bundesländer:
       Graz (~200 km), Linz (~190 km), Salzburg (~295 km),
       Innsbruck (~490 km), Klagenfurt (~310 km), Bregenz (~700 km)

   NIEMALS „Speditionskosten Ausland" oder ähnliches – Österreich ist INLAND!

   ── SONSTIGE PRODUKTE ─────────────────────────────────────────
   Alle weiteren Produkte nach Passform zur Anfrage auswählen.

   → pipedrive_add_deal_product(deal_id, product_id, item_price, quantity)
      für jedes passende Produkt separat aufrufen.

4. STAGE AKTUALISIEREN
   → pipedrive_update_deal mit:
     - "stage_id": {stage_in_bearbeitung}   ← IMMER setzen
     - "{ust_id}": "AT..."  nur wenn UST-ID explizit in Notizen/Firmendaten steht
   (Das Interessensgebiet-Feld wird automatisch gesetzt – du musst es NICHT setzen.)

5. JSON-ZUSAMMENFASSUNG ausgeben:
{{
  "deal_id": 123,
  "deal_title": "...",
  "contact_name": "...",
  "contact_email": "...",
  "einsatzort": "Graz",
  "einsatzort_km_von_wien": 200,
  "kategorie": "Buchtelmobil|Backkurs|Studio|Pop-up Café|...",
  "products_added": [
    {{"product_id": 1, "name": "...", "price": 0.0, "quantity": 1}}
  ],
  "ust_id_gesetzt": false,
  "hinweise": "Offene Fragen oder Interpretation (leer wenn alles klar)"
}}
""".format(
    stage_in_bearbeitung=PIPEDRIVE_STAGE_IN_BEARBEITUNG,
    ust_id="7301275cac15a5a489babf360802632b40633b59",  # FIELD_UST_ID
)


# ─── Keyword → Interessensgebiet Option-ID (Python, kein LLM nötig) ──────────

def _detect_interessensgebiet(title: str, notes: str, kategorie: str) -> str | None:
    """
    Erkennt das Interessensgebiet aus Deal-Titel, Notizen und Agenten-Kategorie.
    Gibt Option-IDs als komma-getrennten String zurück (Pipedrive Set-Feld Format).
    Spezifischere Muster zuerst, um Fehl-Matches zu vermeiden.
    """
    text = " ".join([title, notes, kategorie]).lower()
    ids: list[int] = []

    # Weihnachtsfeier (spezifisch zuerst)
    if "weihnachtsfeier" in text and ("backkurs" in text or "backworkshop" in text):
        ids.append(387)
    elif "weihnachtsfeier" in text:
        ids.append(54)

    # Studio + Backkurs (spezifisch vor reinem Studio)
    if "studio" in text and ("backkurs" in text or "backworkshop" in text) and 387 not in ids:
        ids.append(392)
    elif "studio" in text and 387 not in ids and 392 not in ids:
        ids.append(55)

    # Pop-up Café (vor Buchtelmobil prüfen – ist spezifischer)
    if any(w in text for w in ["pop-up café", "popup café", "pop-up cafe", "popup cafe", "pop up café"]):
        ids.append(56)
    elif any(w in text for w in ["buchtelmobil", "buchtel mobil", "buchtel"]):
        if 56 not in ids:
            ids.append(30)

    # Backkurs standalone
    if ("backkurs" in text or "backworkshop" in text or "back-kurs" in text) \
            and not any(x in ids for x in [28, 387, 392]):
        ids.append(28)

    # Torten / Abo
    if "tortenabo" in text or "torten-abo" in text:
        ids.append(57)
    elif any(w in text for w in ["torten", "kekse", "gebäck"]) and 57 not in ids:
        ids.append(108)

    # Catering (nur wenn kein Pop-up Café erkannt)
    if "catering" in text and 56 not in ids:
        ids.append(187)

    # Sonstige Kategorien
    if any(w in text for w in ["frühstück", "fruehstueck", "breakfast", "business breakfast"]):
        ids.append(199)
    if "gutschein" in text:
        ids.append(339)
    if "keynote" in text:
        ids.append(362)
    if "ngo" in text:
        ids.append(53)
    if "generationenmanagement" in text:
        ids.append(27)
    if "ai backchallenge" in text or ("ai" in text and "back" in text and "challenge" in text):
        ids.append(331)
    if "franchise" in text:
        ids.append(58)
    if "partnerschaft" in text:
        ids.append(188)
    if "content" in text and "produktion" in text:
        ids.append(202)
    if any(w in text for w in ["private feier", "privatfeier", "geburtstagsfeier", "privatveranstaltung"]):
        ids.append(200)
    if "reisegruppe" in text:
        ids.append(201)
    if "geschenke" in text:
        ids.append(29)

    if not ids:
        return None
    # Duplikate entfernen, Reihenfolge beibehalten
    seen: set[int] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    return ",".join(str(i) for i in unique)


def process_new_deal_interim(deal_id: int, deal_data: dict | None = None) -> dict:
    """
    Interim-Workflow: Verarbeitet neue Deals OHNE Sevdesk-Angebotserstellung.

    Ablauf:
      1. Agent liest Deal + Notizen + Kontakt, wählt & fügt Produkte hinzu
      2. Python setzt Interessensgebiet direkt via Keyword-Matching (zuverlässig)
      3. Slack-Benachrichtigung mit Pipedrive-Link

    Sobald Sevdesk POST /Order funktioniert:
      webhook_server.py → eine Zeile auf process_new_deal zurückstellen.
    """
    print(f"\n{'='*60}")
    print(f"🚀 [INTERIM] Neuer Deal: {deal_id}")
    print(f"{'='*60}")

    run_id = start_run(workflow_type="interim", deal_id=deal_id, model="claude-opus-4-5-20251101")

    initial_message = f"""Neuer Deal eingegangen! Deal-ID: {deal_id}

Bitte führe die Schritte 1–5 aus dem System-Prompt durch:
1. Deal + Notizen + Kontakt laden
2. Produkte aus dem Katalog abrufen (pipedrive_list_products)
3. Passende Produkte zum Deal hinzufügen – inkl. korrekte Transportkosten!
4. Stage auf „In Bearbeitung" setzen
5. JSON-Zusammenfassung ausgeben
"""

    result = run_agent(
        system_prompt=INTERIM_SYSTEM_PROMPT,
        initial_message=initial_message,
        tools=PIPEDRIVE_TOOL_DEFINITIONS,
        tool_map=PIPEDRIVE_TOOL_MAP,
        workflow_type="interim",
        deal_id=deal_id,
        max_turns=12,
    )

    finish_run(run_id, status="completed" if not result["error"] else "failed",
               error=result.get("error"))

    if result["error"]:
        post_status_update(f"❌ [Interim] Deal #{deal_id} Verarbeitung fehlgeschlagen: {result['error']}")
        return {"success": False, "deal_id": deal_id, "error": result["error"]}

    # ── Ergebnis parsen ───────────────────────────────────────────────────
    summary_data: dict = {}
    try:
        decoder = json.JSONDecoder()
        text = result["summary"]
        idx = 0
        while idx < len(text):
            brace = text.find('{', idx)
            if brace == -1:
                break
            try:
                parsed, _ = decoder.raw_decode(text, brace)
                if isinstance(parsed, dict) and parsed.get("deal_id"):
                    summary_data = parsed
                    break
                if isinstance(parsed, dict) and len(parsed) > len(summary_data):
                    summary_data = parsed
                idx = brace + 1
            except json.JSONDecodeError:
                idx = brace + 1
    except Exception:
        pass

    deal_title     = summary_data.get("deal_title")    or f"Deal #{deal_id}"
    contact_name   = summary_data.get("contact_name")  or ""
    contact_email  = summary_data.get("contact_email") or ""
    kategorie      = summary_data.get("kategorie")     or ""
    products_added = summary_data.get("products_added") or []
    hinweis        = summary_data.get("hinweise")      or ""

    # ── Interessensgebiet direkt in Python setzen (zuverlässiger als via Agent) ──
    title_raw = deal_data.get("title", "") if deal_data else deal_title
    notes_raw = ""  # Agent hat Notizen bereits gelesen; Kategorie ist ausreichend

    interesse_ids = _detect_interessensgebiet(title_raw, notes_raw, kategorie)

    # Fallback: Interessensgebiet war schon im Webhook-Payload gesetzt
    if not interesse_ids and deal_data:
        existing = get_interessensgebiet_ids(deal_data.get(FIELD_INTERESSENSGEBIET))
        if existing:
            interesse_ids = ",".join(str(i) for i in existing)

    if interesse_ids:
        try:
            from pipedrive_tools import pipedrive_update_deal as _pd_update
            _pd_update(deal_id, {FIELD_INTERESSENSGEBIET: interesse_ids})
            print(f"   ✅ Interessensgebiet gesetzt: {interesse_ids}")
        except Exception as e:
            print(f"   ⚠️ Interessensgebiet-Fehler: {e}")
            hinweis = f"Interessensgebiet ({interesse_ids}) konnte nicht gesetzt werden. " + hinweis
    else:
        print(f"   ⚠️ Interessensgebiet: kein Keyword erkannt – bitte manuell prüfen")
        hinweis = "Interessensgebiet unklar – bitte manuell setzen. " + hinweis

    # ── Pipedrive-Link + Slack ─────────────────────────────────────────────
    pipedrive_link = f"https://{PIPEDRIVE_DOMAIN}.pipedrive.com/deal/{deal_id}"
    display_kategorie = kategorie or (f"IDs: {interesse_ids}" if interesse_ids else "Unbekannt")

    try:
        post_deal_notification(
            deal_id=deal_id,
            deal_title=deal_title,
            contact_name=contact_name,
            contact_email=contact_email,
            pipedrive_link=pipedrive_link,
            kategorie=display_kategorie,
            products_added=products_added,
            hinweis=hinweis.strip(),
        )
        print(f"   ✅ [Interim] Deal #{deal_id} → Slack gesendet")
    except Exception as e:
        print(f"   ⚠️ Slack-Fehler: {e}")

    return {
        "success":            True,
        "deal_id":            deal_id,
        "kategorie":          display_kategorie,
        "interessensgebiet":  interesse_ids,
        "products":           len(products_added),
        "error":              None,
    }
