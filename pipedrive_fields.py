#!/usr/bin/env python3
"""
Pipedrive Lead Field Reference
================================
Alle Custom- und System-Feldkeys für Vollpension Generationendialog GmbH.
Quelle: GET /leadFields (Stand: 2026-03-05)

Konventionen:
  - FIELD_*  → Feldkey (Hash-String) für den API-Zugriff
  - OPT_*    → Option-IDs für enum/set-Felder

Importiert von: pricing_engine.py, offer_workflow.py, invoice_workflow.py
"""

# ═══════════════════════════════════════════════════════════════════
# SYSTEM-FELDER (Standard-Pipedrive, keine Custom Keys)
# ═══════════════════════════════════════════════════════════════════

F_TITLE              = "title"
F_OWNER_ID           = "owner_id"
F_DEAL_VALUE         = "deal_value"
F_DEAL_CURRENCY      = "deal_currency"
F_RELATED_PERSON_ID  = "related_person_id"
F_RELATED_ORG_ID     = "related_org_id"
F_EXPECTED_CLOSE     = "deal_expected_close_date"
F_LABELS             = "labels"
F_DEAL_SOURCE        = "source"
F_ORIGIN             = "origin"           # ManuallyCreated / WebForms / Chatbot / ...
F_ADD_TIME           = "add_time"

# ═══════════════════════════════════════════════════════════════════
# EVENT / BOOKING FELDER (Gruppe 18)
# ═══════════════════════════════════════════════════════════════════

FIELD_INTERESSENSGEBIET   = "9791c786bbfec33c7e13870ca2c1bdaefd16d0e8"  # set (Mehrfachauswahl)
FIELD_PERSONENANZAHL      = "f3bdbc7fbe2c057b655c2ce0d327b14339c41619"  # double
FIELD_EVENT_DATUM         = "9912f2105cd7a2981b72a3fe34fffaf741a33f3c"  # date
FIELD_MIETDAUER           = "f8938617aed3bff8951e198c6b08c1b0e99f9b21"  # enum
FIELD_ZIEL_ADRESSE        = "673b1f9de49bb122d9b73a64d553beba26fd7677"  # address
FIELD_UHRZEIT             = "0f064a100c1ac8934ad487d53dcee30c146f6488"  # varchar
FIELD_EVENT_STARTZEIT     = "b9462f4c5e8f53454406cc05372a6fb324430b0d"  # time
FIELD_EVENT_ZEITRAUM      = "02726eb1d5c2fe3e53a86438a1662ba36b2851ef"  # timerange
FIELD_ZEITBEREICH         = "098b3b31fae056596561f3ee655cb703fe7cf51e"  # timerange
FIELD_CATERING            = "adb6cec43c3c38b46775af315df502d711f55561"  # enum ja/nein
FIELD_BUCHTELMOBIL_INKL   = "ca9d733c28927747f3797788cf7b465cf45cff65"  # enum ja/nein (Pop-up Café)
FIELD_MODERATIONSKOFFER   = "b9221a7a1c8fe18f77df1e88945f039ee36f38a5"  # enum ja/nein
FIELD_LOCATION_TEAMBR     = "9afc816f98572f793b01e3dc120afd79bb91f24c"  # set (Teamfrühstück-Location)
FIELD_POPUP_CAFE_GROESSE  = "94ad4666dd6d35dd31632a7ae553ec306d8f523d"  # varchar (35–85m²)
FIELD_DATUM_STUDIOMIETE   = "cc186077ca44162b918b038a1bdd732f78cc00ea"  # daterange
FIELD_TORTENBESTELLUNG    = "ff1e5c8ad3cfd4fc3c214a51d98cbc3ee18f039a"  # set
FIELD_UST_ID              = "7301275cac15a5a489babf360802632b40633b59"   # varchar
FIELD_ANDERES_ANLIEGEN    = "50f2b5c8c072c59703a8550b32aa08738e44c2be"  # varchar

# Adress-Subfelder von FIELD_ZIEL_ADRESSE
FIELD_ZIEL_FORMATTED      = "673b1f9de49bb122d9b73a64d553beba26fd7677_formatted_address"
FIELD_ZIEL_LOCALITY       = "673b1f9de49bb122d9b73a64d553beba26fd7677_locality"
FIELD_ZIEL_ROUTE          = "673b1f9de49bb122d9b73a64d553beba26fd7677_route"
FIELD_ZIEL_POSTAL         = "673b1f9de49bb122d9b73a64d553beba26fd7677_postal_code"
FIELD_ZIEL_COUNTRY        = "673b1f9de49bb122d9b73a64d553beba26fd7677_country"
FIELD_ZIEL_ADMIN1         = "673b1f9de49bb122d9b73a64d553beba26fd7677_admin_area_level_1"  # Bundesland

# ═══════════════════════════════════════════════════════════════════
# SEVDESK FELDER – BOT-TRACKING (Gruppe 16)
# ═══════════════════════════════════════════════════════════════════

# Bot-Felder (vom Agenten befüllt/gelesen):
FIELD_BOT_ANGEBOT_ID      = "c8a36ba694c2e7c3446a28b016b8e030f1fcc5d8"   # Bot Angebot ID (sevdesk)
FIELD_BOT_ANGEBOT_LINK    = "caae4e48b7292787905825bb811bb181d2778256"    # Bot Angebot Link (sevDesk)
FIELD_BOT_RECHNUNG_ID     = "661a610102789b967dd713dc668ec3b4ca3d07dc"    # Bot Rechnung ID (sevdesk)
FIELD_BOT_RECHNUNG_LINK   = "27247cd93ac60d754013c641b0929f375e8ca383"    # Bot Rechnung Link (sevDesk)
FIELD_BOT_AUFTRBESTID     = "785ea85cc4606362820e57d7eec07f6644359363"    # Bot Auftragsbestätigung ID (sevdesk)

# Manuell gepflegte sevdesk-Felder (Nummern sichtbar im UI):
FIELD_ANGEBOTSNUMMER      = "6237a253d9e3e52013c2a606f28948dd7f003f74"   # Angebotsnummer (sevdesk)
FIELD_ANGEBOT_ID          = "bcea83f2fd3a01dbfaf58175c71ea929cdd68907"   # Angebot-ID (sevdesk)
FIELD_RECHNUNGSNUMMER     = "2d23adf48283397bbdbfc0126249c23e935786d0"   # Rechnungsnummer (sevdesk)
FIELD_RECHNUNG_ID         = "190f43ba76e6d9be285d95e3e6cd89ceb680717c"   # Rechnung-ID (sevdesk)
FIELD_AUFTRBESTNUMMER     = "87c4c84ebcb23648d310c69c8a6c3a8ca76e6c24"   # Auftragsbestätigungsnummer (sevdesk)
FIELD_AUFTRBESTLINK       = "3212392ba02afe367fcd412f9a3ac5b5946bd87e"   # Auftragsbestätigung (sevdesk)
FIELD_AUFTRBESTID_ALT     = "e46458354821ae4b3bb8eeac514add901d08ba11"   # Auftragsbestätigung-ID (sevdesk)

# ═══════════════════════════════════════════════════════════════════
# SALES / SONSTIGES
# ═══════════════════════════════════════════════════════════════════

FIELD_DEAL_SOURCE         = "c05fd2514bd44c32163ca279a63ac1fb71786263"   # Deal Source (enum)
FIELD_FUNKTION            = "9ac54773f790dfb4a7e99ed95ffd038957ac608c"   # Funktion der Kontaktperson (enum)
FIELD_CHATBOT_URL         = "7adf59d667411f9cd11c60073b85c00aeea11157"   # Chatbot-Konvertierungs-URL

# ═══════════════════════════════════════════════════════════════════
# SHOWCASE / SPECIAL DEALS (Gruppe 12)
# ═══════════════════════════════════════════════════════════════════
# Hinweis: In Pipedrive als "Showcase Feb26 Special" gespeichert
FIELD_SHOWCASE_SPECIAL    = "38c04fd3ab5088f4cc1cee4aaadadf2e4f6ac231"   # set

# ═══════════════════════════════════════════════════════════════════
# OPTION-IDs: INTERESSENSGEBIET (set, Mehrfachauswahl)
# ═══════════════════════════════════════════════════════════════════

class Interesse:
    TORTENABO              = 57
    TORTEN_KEKS            = 108
    BUCHTELMOBIL           = 30
    POPUP_CAFE             = 56   # Pop-up Café inkl. Buchtelmobil
    WEIHNACHTSFEIER        = 54
    WEIHNACHTSFEIER_BACK   = 387  # Weihnachtsfeier inkl. Backkurs
    STUDIO_MIETE           = 55
    STUDIO_MIT_BACKKURS    = 392
    PRIVATE_STUDIO         = 203
    TEAMBUILDING_BACKKURS  = 28
    AI_BACKCHALLENGE       = 331
    CATERING               = 187
    GESCHENKE              = 29   # Geschenke mit sozialer Mission
    WORKSHOP_NGO           = 53
    GENERATIONENMANAGEMENT = 27
    FRANCHISE              = 58
    PARTNERSCHAFT          = 188
    CONTENT_PRODUKTION     = 202
    KEYNOTE                = 362
    BUSINESS_FRUEHSTUECK   = 199  # Business-Frühstück im Café
    PRIVATE_FEIER          = 200
    REISEGRUPPE            = 201
    GUTSCHEINE             = 339

    # Gruppen für Routing-Logik
    BRAUCHT_BUCHTELMOBIL   = {30, 56}           # Buchtelmobil oder Pop-up Café
    BRAUCHT_ZIELADRESSE    = {30, 56, 187}      # Lieferung außer Haus
    STUDIO_EVENTS          = {55, 392, 203, 28} # Alles im Studio
    WEIHNACHTS_EVENTS      = {54, 387}

# ═══════════════════════════════════════════════════════════════════
# OPTION-IDs: MIETDAUER
# ═══════════════════════════════════════════════════════════════════

class Mietdauer:
    VORMITTAG    = 160
    NACHMITTAG   = 161
    GANZTAG      = 162
    MEHRERE_TAGE = 166

    GANZTAGS_IDS = {162, 166}  # für Preisberechnung "ganztags"

# ═══════════════════════════════════════════════════════════════════
# OPTION-IDs: CATERING / BUCHTELMOBIL INKLUSIVE
# ═══════════════════════════════════════════════════════════════════

OPT_JA_CATERING         = 158
OPT_NEIN_CATERING       = 159
OPT_JA_BUCHTELMOBIL_INC = 167
OPT_NEIN_BUCHTELMOB_INC = 168
OPT_JA_MODKOFFER        = 156
OPT_NEIN_MODKOFFER      = 157

# ═══════════════════════════════════════════════════════════════════
# OPTION-IDs: LOCATION FÜR TEAMFRÜHSTÜCK
# ═══════════════════════════════════════════════════════════════════

class Location:
    SCHLEI        = 163   # Generationencafé Schleifmühlgasse
    JOHANNESGASSE = 164   # Generationencafé Johannesgasse
    STUDIO_MHG    = 165   # Vollpension Studio Mariahilferstraße

# ═══════════════════════════════════════════════════════════════════
# OPTION-IDs: SHOWCASE SPECIAL
# ═══════════════════════════════════════════════════════════════════

class Showcase:
    BUCHTELMOBIL_TORTE    = 388  # Buchtelmobil + 1 Sachertorte fürs Office on top
    BACKKURS_GRATIS_PAX   = 389  # Backkurs + 1 Person gratis bei Buchung ab 12 Pax
    STUDIO_BUCHTELN_INKL  = 390  # Studiomiete + Buchteln für alle Teilnehmer inklusive

# ═══════════════════════════════════════════════════════════════════
# OPTION-IDs: DEAL SOURCE
# ═══════════════════════════════════════════════════════════════════

class DealSource:
    INBOUND        = 38
    KUNDE          = 37
    B2B_NEWSLETTER = 36
    ROADSHOW       = 367
    HR_SUMMIT      = 33
    PARTNERKONTAKT = 62
    NETZWERK       = 63

# ═══════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN
# ═══════════════════════════════════════════════════════════════════

def ist_ganztags(mietdauer_option_id) -> bool:
    """True wenn Mietdauer Ganztag oder Mehrere Tage."""
    try:
        return int(mietdauer_option_id) in Mietdauer.GANZTAGS_IDS
    except (TypeError, ValueError):
        # Fallback: Textvergleich
        label = str(mietdauer_option_id).lower()
        return "ganz" in label or "mehrere" in label


def get_interessensgebiet_ids(raw_value) -> set:
    """
    Normalisiert den Mehrfachoption-Wert aus Pipedrive zu einem Set von Integer-IDs.
    Pipedrive liefert je nach Endpunkt:
      - Liste von Dicts: [{"id": 30, "label": "Buchtelmobil"}, ...]
      - Komma-String:    "30,56"
      - Einzelner Int:   30
    """
    if not raw_value:
        return set()
    if isinstance(raw_value, list):
        ids = set()
        for item in raw_value:
            if isinstance(item, dict):
                ids.add(int(item["id"]))
            else:
                ids.add(int(item))
        return ids
    if isinstance(raw_value, (int, float)):
        return {int(raw_value)}
    # String: "30,56" oder "Buchtelmobil"
    result = set()
    for part in str(raw_value).split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


def get_ziel_adresse(deal_data: dict) -> str:
    """
    Gibt die formatierte Zieladresse zurück.
    Versucht zuerst den formatted_address Subkey, dann Einzelfelder.
    """
    formatted = deal_data.get(FIELD_ZIEL_FORMATTED, "")
    if formatted:
        return str(formatted).strip()
    # Manuelle Zusammensetzung
    parts = [
        deal_data.get(FIELD_ZIEL_ROUTE, ""),
        deal_data.get(FIELD_ZIEL_LOCALITY, ""),
        deal_data.get(FIELD_ZIEL_COUNTRY, ""),
    ]
    return ", ".join(p for p in parts if p)


def get_showcase_options(deal_data: dict) -> set:
    """Gibt Set der aktiven Showcase-Option-IDs zurück."""
    return get_interessensgebiet_ids(deal_data.get(FIELD_SHOWCASE_SPECIAL))
