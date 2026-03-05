#!/usr/bin/env python3
"""
Pricing Engine – Vollpension Generationendialog GmbH
======================================================
Berechnet Angebotspositionen deterministisch aus Deal-Daten.
Alle Preise stammen aus dem Produktkatalog (products-22223086-88.xlsx, Stand 2026-03).

Steuerlogik (Österreich):
  10% MwSt  → Lebensmittel: Buchteln, Eisbuchteln, Torten, Kekse, Catering-Speisen
  20% MwSt  → Dienstleistungen: Miete, Staff, Transport, Equipment, Backkurse, Weihnachtsfeier
   0% MwSt  → Sonderposten: Spedition Ausland, Diäten, Reisekosten
"""

from __future__ import annotations
import json
from datetime import datetime
from pipedrive_fields import (
    FIELD_PERSONENANZAHL, FIELD_EVENT_DATUM, FIELD_INTERESSENSGEBIET,
    FIELD_MIETDAUER, FIELD_ZIEL_ADRESSE, FIELD_CATERING,
    FIELD_BUCHTELMOBIL_INKL, FIELD_MODERATIONSKOFFER,
    FIELD_POPUP_CAFE_GROESSE, FIELD_DATUM_STUDIOMIETE,
    FIELD_SHOWCASE_SPECIAL, FIELD_ZIEL_ADMIN1,
    FIELD_ZIEL_FORMATTED, FIELD_ZIEL_LOCALITY,
    Interesse, Mietdauer, Showcase,
    ist_ganztags, get_interessensgebiet_ids, get_ziel_adresse, get_showcase_options,
    OPT_JA_CATERING,
)

# ═══════════════════════════════════════════════════════════════════
# PREISTABELLEN (aus Produktkatalog)
# ═══════════════════════════════════════════════════════════════════

# Buchteln (MwSt 10%)
BUCHTEL_PREISE = [
    (100,  6.00),
    (250,  5.50),
    (500,  5.00),
    (1000, 4.70),
]
EISBUCHTEL_PREISE = [        # Staffelpreise Eisbuchteln (MwSt 10%)
    (100,  8.50),
    (250,  8.00),
    (500,  7.50),
    (1000, 7.00),
]

# Buchtelmobil (MwSt 20%)
BUCHTELMOBIL_HALBTAGS        = 250.00
BUCHTELMOBIL_GANZTAGS        = 500.00
STAFF_GENERATIONENTEAM_HALB  = 240.00
STAFF_GENERATIONENTEAM_GANZ  = 480.00
STAFF_VORBEREITUNG           = 240.00   # Aufbau/Abbau & Logistik, pauschal
STAFF_BACKSTUBE_PRO_H        = 30.00    # 1h pro 100 Buchteln
GENERATIONENTEAM_ANREISE     = 30.00    # pro Stunde An-/Abreise
EQUIPMENT_VOR_ORT            = 1.00     # pro Stück Buchteln (MwSt 20%)
EXTRA_KUCHENSTUECK           = 5.50     # pro Stück (MwSt 10%)

# Transport Buchtelmobil (MwSt 20%)
TRANSPORT_BUCHTELMOBIL = {
    "wien":    250.00,
    "50km":    350.00,
    "150km":   500.00,
    "ausland": 700.00,
}

# Pop-up Café Miete nach Größe (MwSt 20%)
POPUP_MIETE = {
    "klein":  3000.00,   # bis ~45m²
    "mittel": 4000.00,   # 45–65m² (Standard)
    "gross":  5000.00,   # >65m²
}
POPUP_STAFF_AUFBAU    = 240.00   # Aufbau/Abbau & Logistik Pop-up Café (MwSt 20%)

# Transport Pop-up Café (MwSt 20%)
TRANSPORT_POPUP = {
    "wien":    500.00,
    "50km":    650.00,
    "150km":   750.00,
    "ausland": 1000.00,
}

# Studiomiete (MwSt 20%)
STUDIO_HALBTAGS       = 590.00
STUDIO_GANZTAGS       = 990.00
STUDIO_REINIGUNG      = 120.00
STUDIO_MAX_PERSONEN   = 45
MODERATIONSKOFFER_EP  = 35.00    # (MwSt 20%)

# Studio-Catering (MwSt 10% außer Getränke alkoholisch)
KUCHEN_BUFFET_PP      = 10.00    # Süßes Catering: Kuchen- und Tortenbuffet p.P. (10%)
GETRAENKE_PP          = 15.00    # Getränkepauschale antialkoholisch p.P. (10%)
GETRAENKE_ALKOHOL_PP  = 20.00    # Getränkepauschale alkoholisch p.P. (20%)
OMA_LUNCH_PP          = 18.00    # Oma-Lunch p.P. (10%)

# Teambuilding-Backkurse (MwSt 20%)
BACKKURS_PP           = 120.00
KEKSBACKKURS_PP       = 140.00
BACKKURS_MIN_PAX      = 12       # Mindest-Teilnehmerzahl
AI_BACKCHALLENGE_FLAT = 4000.00  # pauschal, min. 20 Pax
AI_BACKCHALLENGE_ZUSATZ = 100.00 # pro Zusatzperson über 20

# Weihnachtsfeier (MwSt 20%)
WEIHNACHTSFEIER_PP    = 130.00   # ohne Backkurs (alle 3 Locations)
WEIHNACHTSFEIER_BACK_PP = 220.00 # inkl. Backkurs
PUNSCHEMPFANG_PP      = 10.00    # optional (MwSt 20%)

# Buchtelcatering Transport (MwSt 20%)
TRANSPORT_BUCHTELCATERING = {
    "wien":   100.00,
    "50km":   150.00,
    "150km":  200.00,
    "ausland": 350.00,
}

# Torten Lieferung (MwSt 20%)
TORTEN_LIEFERUNG_STUECK  = 15.00
TORTEN_LIEFERUNG_PAUSCH  = 150.00   # ab ~4 Stück günstiger

# Keynote (MwSt 20%)
KEYNOTE_PREIS = 1000.00


# ═══════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN
# ═══════════════════════════════════════════════════════════════════

def _pos(name: str, qty: float, price: float, description: str = "", tax: int = 20) -> dict:
    return {"name": name, "quantity": qty, "price": price, "tax_rate": tax, "description": description}


def _buchtel_stueckpreis(anzahl: int) -> float:
    for limit, preis in BUCHTEL_PREISE:
        if anzahl <= limit:
            return preis
    return 4.70


def _eisbuchtel_stueckpreis(anzahl: int) -> float:
    for limit, preis in EISBUCHTEL_PREISE:
        if anzahl <= limit:
            return preis
    return 7.00


def _transport_zone(einsatzort: str) -> str:
    """Ermittelt Transportzone aus Adressstring."""
    ort = (einsatzort or "").lower()
    if not ort or "wien" in ort or any(plz in ort for plz in ["1010","1020","1030","1040","1050","1060","1070","1080","1090","1100","1110","1120","1130","1140","1150","1160","1170","1180","1190","1200","1210","1220","1230"]):
        return "wien"
    elif any(kw in ort for kw in ["niederösterreich", "burgenland", "nö", "bgld"]):
        return "50km"
    elif any(kw in ort for kw in ["steiermark","oberösterreich","salzburg","tirol","vorarlberg","kärnten","oö","stmk","sbg","ktn","vbg"]):
        return "150km"
    else:
        return "ausland"


def _popup_groesse(groesse_raw) -> str:
    """Ermittelt Größenkategorie aus FIELD_POPUP_CAFE_GROESSE (varchar, z.B. '50m2' oder '50')."""
    import re
    try:
        # Nur die führenden Ziffern nehmen (vor dem ersten Buchstaben)
        match = re.match(r"(\d+)", str(groesse_raw or "").strip())
        m2 = int(match.group(1)) if match else 0
        if m2 <= 45:
            return "klein"
        elif m2 <= 65:
            return "mittel"
        else:
            return "gross"
    except Exception:
        return "mittel"   # Standard laut Screenshot


def _pax_min(personenanzahl: int, minimum: int) -> tuple[int, str | None]:
    """Stellt Mindest-Personenanzahl sicher. Gibt (effektive_pax, hinweis) zurück."""
    if personenanzahl and personenanzahl < minimum:
        return minimum, f"Personenanzahl {personenanzahl} unter Mindest-Teilnehmerzahl {minimum} – mit {minimum} Pax kalkuliert."
    return (personenanzahl or minimum), None


def _studio_tage(deal_data: dict) -> int:
    """Berechnet Anzahl Miettage aus FIELD_DATUM_STUDIOMIETE (daterange)."""
    start = deal_data.get(FIELD_DATUM_STUDIOMIETE)
    end   = deal_data.get(FIELD_DATUM_STUDIOMIETE + "_until")
    try:
        d1 = datetime.fromisoformat(str(start))
        d2 = datetime.fromisoformat(str(end))
        return max((d2 - d1).days, 1)
    except Exception:
        return 1


# ═══════════════════════════════════════════════════════════════════
# SHOWCASE-BONUS-POSITIONEN
# ═══════════════════════════════════════════════════════════════════

def _showcase_positionen(deal_data: dict) -> list[dict]:
    active = get_showcase_options(deal_data)
    extras = []
    if Showcase.BUCHTELMOBIL_TORTE in active:
        extras.append(_pos("Showcase-Bonus: Sachertorte fürs Office",
                           1, 0.0, "Gratis Sachertorte inklusive (Showcase März 2026 Special)"))
    if Showcase.BACKKURS_GRATIS_PAX in active:
        extras.append(_pos("Showcase-Bonus: 1 Gratis-Teilnehmer",
                           1, 0.0, "1 Person gratis bei Buchung ab 12 Pax (Showcase Special)"))
    if Showcase.STUDIO_BUCHTELN_INKL in active:
        extras.append(_pos("Showcase-Bonus: Buchteln inklusive",
                           1, 0.0, "Buchteln für alle Teilnehmer inklusive (Showcase Special)"))
    return extras


# ═══════════════════════════════════════════════════════════════════
# KATEGORIE-RECHNER
# ═══════════════════════════════════════════════════════════════════

def _calc_buchtelmobil(pax: int, ganztags: bool, deal_data: dict) -> tuple[list[dict], list[str]]:
    hinweise = []
    einsatzort = get_ziel_adresse(deal_data)
    zone = _transport_zone(einsatzort)

    # Buchteln: 2 pro Person, min. 100
    buchtel_anzahl = max(pax * 2, 100) if pax else 200
    sp = _buchtel_stueckpreis(buchtel_anzahl)
    positions = [
        _pos(f"Buchteln {_buchtel_tier(buchtel_anzahl)}", buchtel_anzahl, sp,
             f"Frisch gebackene Buchteln, Staffelpreis {sp:.2f} €/Stk.", tax=10),
        _pos("Equipment für Buchteln vor Ort", buchtel_anzahl, EQUIPMENT_VOR_ORT,
             "Besteck und Schälchen (Einweg)", tax=20),
        _pos(f"Miete Buchtelmobil ({'ganztags' if ganztags else 'halbtags'})", 1,
             BUCHTELMOBIL_GANZTAGS if ganztags else BUCHTELMOBIL_HALBTAGS,
             "mit oder ohne Live Backen", tax=20),
        _pos(f"Staff: Buchtelmobil Generationenteam ({'ganztags' if ganztags else 'halbtags'})", 1,
             STAFF_GENERATIONENTEAM_GANZ if ganztags else STAFF_GENERATIONENTEAM_HALB,
             "1 Jungspund + 1 Oma/Opa" + (", ganztags" if ganztags else ", 4h"), tax=20),
        _pos("Staff: Vorbereitung, Aufbau/Abbau & Logistik Buchtelmobil", 1, STAFF_VORBEREITUNG,
             "2 Pax je 4h", tax=20),
        _pos(f"Transportkosten Buchtelmobil {_transport_label_buchtel(zone)}", 1,
             TRANSPORT_BUCHTELMOBIL[zone], tax=20),
    ]
    # An- und Abreise nur außerhalb Wien
    if zone != "wien":
        positions.insert(4, _pos("Generationenteam An- und Abreise", 2, GENERATIONENTEAM_ANREISE,
                                  "Richtwert 2h – bei Bedarf anpassen", tax=20))
    return positions, hinweise


def _calc_popup_cafe(pax: int, ganztags: bool, deal_data: dict) -> tuple[list[dict], list[str]]:
    hinweise = []
    einsatzort = get_ziel_adresse(deal_data)
    zone = _transport_zone(einsatzort)
    groesse = _popup_groesse(deal_data.get(FIELD_POPUP_CAFE_GROESSE))
    miete = POPUP_MIETE[groesse]

    buchtel_anzahl = max(pax, 100) if pax else 150
    sp = _buchtel_stueckpreis(buchtel_anzahl)

    positions = [
        _pos(f"Miete Pop-up Café {groesse} pro Tag", 1, miete,
             f"bis {'45' if groesse=='klein' else '65' if groesse=='mittel' else '85'}m2", tax=20),
        # Buchtelmobil ist IMMER inklusive → Preis €0
        _pos("Miete Buchtelmobil", 1, 0.0, "mit oder ohne Live Backen", tax=20),
        _pos(f"Buchteln {_buchtel_tier(buchtel_anzahl)}", buchtel_anzahl, sp,
             f"Staffelpreis {sp:.2f} €/Stk.", tax=10),
        _pos("Equipment für Buchteln vor Ort", buchtel_anzahl, EQUIPMENT_VOR_ORT,
             "Besteck und Schälchen (Einweg)", tax=20),
        _pos(f"Staff: Buchtelmobil Generationenteam ({'ganztags' if ganztags else 'halbtags'})", 1,
             STAFF_GENERATIONENTEAM_GANZ if ganztags else STAFF_GENERATIONENTEAM_HALB,
             "1 Jungspund + 1 Oma/Opa", tax=20),
        _pos("Staff: Aufbau/Abbau & Logistik Pop-up Café", 1, POPUP_STAFF_AUFBAU, tax=20),
    ]

    if zone == "wien":
        # Wien: Fixpreis aus Katalog
        positions.append(_pos("Transportkosten Pop-up Café innerhalb Wiens", 1,
                               TRANSPORT_POPUP["wien"], tax=20))
    else:
        # Außerhalb Wien: Logistikfirma anfragen → Platzhalter + Hinweis
        positions.append(_pos(
            f"Transportkosten Pop-up Café {_transport_label_popup(zone)} – BITTE ANFRAGEN",
            1, 0.0,
            "⚠ Preis muss bei Logistikfirma angefragt werden – noch nicht im Angebot enthalten!",
            tax=20
        ))
        hinweise.append(
            f"⚠️ ACHTUNG: Pop-up Café außerhalb Wien ({einsatzort or zone}) – "
            "Transportkosten müssen bei der Logistikfirma angefragt werden. "
            "Position ist mit €0 als Platzhalter im Angebot, bitte vor dem Versand aktualisieren!"
        )

    return positions, hinweise


def _calc_studio(pax: int, ganztags: bool, deal_data: dict) -> tuple[list[dict], list[str]]:
    hinweise = []

    # Personenanzahl-Check
    if pax and pax > STUDIO_MAX_PERSONEN:
        hinweise.append(
            f"⚠️ ACHTUNG: {pax} Personen angegeben – Studio fasst maximal {STUDIO_MAX_PERSONEN} Personen! "
            "Bitte mit Kundin abstimmen, ob zwei Sessions oder ein externer Veranstaltungsort nötig ist."
        )

    # Mietdauer
    mietdauer_id = deal_data.get(FIELD_MIETDAUER)
    tage = _studio_tage(deal_data)

    if mietdauer_id in (Mietdauer.MEHRERE_TAGE, str(Mietdauer.MEHRERE_TAGE)):
        miete = STUDIO_GANZTAGS * tage
        label = f"Studiomiete {tage} Tage"
        beschr = f"{tage} × Ganztag (Datumsbereich aus Deal)"
    elif ganztags:
        miete = STUDIO_GANZTAGS
        label = "Studiomiete ganztags"
        beschr = ""
    else:
        miete = STUDIO_HALBTAGS
        label = "Studiomiete halbtags"
        beschr = "Vormittag oder Nachmittag"

    positions = [
        _pos(label, 1, miete, beschr, tax=20),
        _pos("Reinigungspauschale", 1, STUDIO_REINIGUNG, tax=20),
        # Moderationskoffer immer bei Studiomiete
        _pos("Moderationskoffer inkl. Verbrauchsmaterialien", 1, MODERATIONSKOFFER_EP, tax=20),
    ]

    # Catering
    if deal_data.get(FIELD_CATERING) in (OPT_JA_CATERING, str(OPT_JA_CATERING)):
        eff_pax = max(pax or 1, 1)
        positions += [
            _pos("Süßes Catering: Kuchen- und Tortenbuffet p.P.", eff_pax,
                 KUCHEN_BUFFET_PP, "Kuchen- und Tortenbuffet", tax=10),
            _pos("Getränkepauschale antialkoholisch p.P.", eff_pax,
                 GETRAENKE_PP, "Kaffee, Tee, Wasser, Säfte", tax=10),
        ]

    return positions, hinweise


def _calc_teambuilding(pax: int, ganztags: bool, deal_data: dict,
                       keks: bool = False) -> tuple[list[dict], list[str]]:
    hinweise = []
    eff_pax, hint = _pax_min(pax, BACKKURS_MIN_PAX)
    if hint:
        hinweise.append(hint)

    preis = KEKSBACKKURS_PP if keks else BACKKURS_PP
    name  = "Teambuilding Keksbackkurs" if keks else "Teambuilding Backkurs"
    beschr = "inkl. Zutaten, Studio, Personal, Getränke, Urkunde" + \
             (" (Kekse backen)" if keks else " (Buchteln, Strudel o. Guglhupf)")

    positions = [_pos(name, eff_pax, preis, beschr, tax=20)]
    return positions, hinweise


def _calc_ai_backchallenge(pax: int, ganztags: bool, deal_data: dict) -> tuple[list[dict], list[str]]:
    hinweise = []
    MIN_PAX = 20
    positions = [
        _pos("AI Back-Challenge (min. 20 Pax)", 1, AI_BACKCHALLENGE_FLAT,
             "Pauschalpreis für bis zu 20 Personen", tax=20)
    ]
    if pax and pax > MIN_PAX:
        zusatz = pax - MIN_PAX
        positions.append(_pos("AI Back-Challenge Zusatzperson", zusatz,
                               AI_BACKCHALLENGE_ZUSATZ, tax=20))
    elif pax and pax < MIN_PAX:
        hinweise.append(f"AI Back-Challenge: Mindest-Teilnehmerzahl 20 Pax – Pauschalpreis gilt trotzdem.")
    return positions, hinweise


def _calc_weihnachtsfeier(pax: int, ganztags: bool, deal_data: dict,
                          mit_backkurs: bool = False) -> tuple[list[dict], list[str]]:
    hinweise = []
    eff_pax = max(pax or 1, 1)

    preis = WEIHNACHTSFEIER_BACK_PP if mit_backkurs else WEIHNACHTSFEIER_PP
    name  = ("Weihnachtsfeier inkl. Backkurs" if mit_backkurs
             else "Weihnachtsfeier Schleifmühlgasse")
    beschr = "pro Person, inkl. Programm, Mehlspeisen, Getränke"
    if mit_backkurs:
        beschr += " + Backkurs"

    positions = [_pos(name, eff_pax, preis, beschr, tax=20)]
    return positions, hinweise


def _calc_keynote(pax: int, ganztags: bool, deal_data: dict) -> tuple[list[dict], list[str]]:
    einsatzort = get_ziel_adresse(deal_data)
    zone = _transport_zone(einsatzort)
    positions = [
        _pos("Keynote", 1, KEYNOTE_PREIS,
             "Keynote-Vortrag zum Thema Generationendialog (ca. 45–60 Min.)", tax=20),
    ]
    # An/Abreise + Reisekosten nur außerhalb Wien
    if zone != "wien":
        positions.append(_pos("Generationenteam An- und Abreise", 2, GENERATIONENTEAM_ANREISE,
                               "Richtwert 2h An-/Abreise", tax=20))
        t = TRANSPORT_BUCHTELMOBIL[zone]
        positions.append(_pos(f"Reisekosten {_transport_label_buchtel(zone)}", 1, t, tax=20))
    return positions, []


def _calc_buchtelcatering(pax: int, ganztags: bool, deal_data: dict) -> tuple[list[dict], list[str]]:
    einsatzort = get_ziel_adresse(deal_data)
    zone = _transport_zone(einsatzort)
    buchtel_anzahl = max(pax * 2, 100) if pax else 200
    sp = _buchtel_stueckpreis(buchtel_anzahl)
    positions = [
        _pos(f"Buchtelcatering – Buchteln {_buchtel_tier(buchtel_anzahl)}", buchtel_anzahl, sp,
             "Lieferung fertig gebackener Buchteln", tax=10),
        _pos("Verpackung & Handling", 1, 30.00, "Boxen, Etiketten", tax=20),
        _pos(f"Transportkosten Buchtelcatering {_transport_label_catering(zone)}", 1,
             TRANSPORT_BUCHTELCATERING[zone], tax=20),
    ]
    return positions, []


def _calc_torten(pax: int, ganztags: bool, deal_data: dict) -> tuple[list[dict], list[str]]:
    """
    Tortenbestellung: Exakte Torten-Positionen aus Deal-Notiz entnehmen.
    Hier wird nur die Lieferpauschale kalkuliert; Torten werden als Hinweis markiert.
    """
    einsatzort = get_ziel_adresse(deal_data)
    zone = _transport_zone(einsatzort)
    hinweise = [
        "Tortenbestellung: Bitte Tortentypen und Mengen aus der Deal-Notiz entnehmen "
        "und als separate Positionen hinzufügen (Preise im Produktkatalog je nach Sorte und Größe). "
        "Lieferposition wurde automatisch ergänzt."
    ]
    # Lieferung: pauschal wenn wahrscheinlich mehrere Torten, sonst pro Stück
    lieferpreis = TORTEN_LIEFERUNG_PAUSCH if (pax and pax >= 4) else TORTEN_LIEFERUNG_STUECK
    positions = [
        _pos("Torten / Kuchen Lieferung", 1, lieferpreis,
             "Lieferpauschale, ggf. anpassen je nach Stückzahl", tax=20),
    ]
    return positions, hinweise


# ═══════════════════════════════════════════════════════════════════
# LABEL-HELPER
# ═══════════════════════════════════════════════════════════════════

def _buchtel_tier(anzahl: int) -> str:
    if anzahl <= 100: return "bis 100 Stk"
    if anzahl <= 250: return "101 bis 250 Stk"
    if anzahl <= 500: return "251 bis 500 Stk"
    return "501 bis 1000 Stk"

def _transport_label_buchtel(zone: str) -> str:
    return {"wien":"innerhalb Wien","50km":"Österreich kleiner 50km",
            "150km":"Österreich kleiner 150km","ausland":"Österreich größer 150km"}.get(zone,"")

def _transport_label_popup(zone: str) -> str:
    return {"wien":"innerhalb Wiens","50km":"Österreich kleiner 50km",
            "150km":"Österreich kleiner 150km","ausland":"Österreich größer 150km"}.get(zone,"")

def _transport_label_catering(zone: str) -> str:
    return {"wien":"innerhalb von Wien","50km":"<50km",
            "150km":"<150km","ausland":">150km"}.get(zone,"")


# ═══════════════════════════════════════════════════════════════════
# ROUTING-TABELLE
# ═══════════════════════════════════════════════════════════════════

_INTERESSE_HANDLER: dict[int, tuple] = {
    Interesse.BUCHTELMOBIL:          (_calc_buchtelmobil,    {}),
    Interesse.POPUP_CAFE:            (_calc_popup_cafe,      {}),
    Interesse.STUDIO_MIETE:          (_calc_studio,          {}),
    Interesse.STUDIO_MIT_BACKKURS:   (_calc_studio,          {}),
    Interesse.PRIVATE_STUDIO:        (_calc_studio,          {}),
    Interesse.TEAMBUILDING_BACKKURS: (_calc_teambuilding,    {}),
    Interesse.AI_BACKCHALLENGE:      (_calc_ai_backchallenge,{}),
    Interesse.WEIHNACHTSFEIER:       (_calc_weihnachtsfeier, {}),
    Interesse.WEIHNACHTSFEIER_BACK:  (_calc_weihnachtsfeier, {"mit_backkurs": True}),
    Interesse.KEYNOTE:               (_calc_keynote,         {}),
    Interesse.CATERING:              (_calc_buchtelcatering, {}),
    Interesse.TORTEN_KEKS:           (_calc_torten,          {}),
}

# Keksbackkurs: Keks-Flag setzen wenn Weihnachtsfeier + Backkurs + explizit Keks aus Notiz
# → Claude entscheidet per system prompt; direkt aufrufbar:
def calc_keksbackkurs(pax: int, deal_data: dict) -> tuple[list[dict], list[str]]:
    return _calc_teambuilding(pax, False, deal_data, keks=True)


# ═══════════════════════════════════════════════════════════════════
# HAUPT-API
# ═══════════════════════════════════════════════════════════════════

def calculate_offer_positions(deal_data: dict) -> dict:
    """
    Berechnet Angebotspositionen aus Deal-Daten.

    Args:
        deal_data: Vollständiger Deal-Dict aus pipedrive_get_deal().

    Returns:
        {
          "positions":   Liste für sevdesk_create_offer()
          "total_net":   Netto-Gesamtsumme
          "total_gross": Brutto-Gesamtsumme
          "kategorien":  Erkannte Interessensgebiet-IDs
          "hinweise":    Hinweise für den Agenten (Warnungen, fehlende Felder)
        }
    """
    alle_hinweise: list[str] = []

    # ── Stammdaten ─────────────────────────────────────────────────
    pax_raw = deal_data.get(FIELD_PERSONENANZAHL)
    pax = int(float(pax_raw)) if pax_raw else 0
    if not pax:
        alle_hinweise.append("Personenanzahl fehlt – Positionen basieren auf Richtwerten.")

    mietdauer_raw = deal_data.get(FIELD_MIETDAUER)
    ganztags = ist_ganztags(mietdauer_raw)
    if not mietdauer_raw:
        alle_hinweise.append("Mietdauer nicht angegeben – halbtags angenommen.")

    interesse_ids = get_interessensgebiet_ids(deal_data.get(FIELD_INTERESSENSGEBIET))
    if not interesse_ids:
        alle_hinweise.append(
            "Interessensgebiet nicht gesetzt – keine automatische Kalkulation möglich. "
            "Positionen bitte manuell zusammenstellen."
        )
        return {
            "positions": [], "total_net": 0.0, "total_gross": 0.0,
            "kategorien": [], "hinweise": alle_hinweise,
        }

    # ── Positionen je Kategorie ────────────────────────────────────
    all_positions: list[dict] = []
    matched_ids: list[int] = []

    for iid in interesse_ids:
        if iid in _INTERESSE_HANDLER:
            fn, kwargs = _INTERESSE_HANDLER[iid]
            pos, hints = fn(pax, ganztags, deal_data, **kwargs)
            all_positions.extend(pos)
            alle_hinweise.extend(hints)
            matched_ids.append(iid)
        else:
            alle_hinweise.append(
                f"Interessensgebiet-ID {iid} hat noch keinen automatischen Rechner – "
                "Positionen bitte manuell ergänzen."
            )

    # ── Showcase-Extras ────────────────────────────────────────────
    showcase_extras = _showcase_positionen(deal_data)
    if showcase_extras:
        all_positions.extend(showcase_extras)
        alle_hinweise.append(f"{len(showcase_extras)} Showcase-Sonderkondition(en) hinzugefügt.")

    # ── Summen ─────────────────────────────────────────────────────
    total_net   = sum(p["quantity"] * p["price"] for p in all_positions)
    total_gross = sum(
        p["quantity"] * p["price"] * (1 + p.get("tax_rate", 20) / 100)
        for p in all_positions
    )

    return {
        "positions":   all_positions,
        "total_net":   round(total_net, 2),
        "total_gross": round(total_gross, 2),
        "kategorien":  matched_ids,
        "hinweise":    alle_hinweise,
    }


# ═══════════════════════════════════════════════════════════════════
# CLI-TEST
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from pipedrive_fields import FIELD_PERSONENANZAHL, FIELD_MIETDAUER, FIELD_INTERESSENSGEBIET
    from pipedrive_fields import Interesse, Mietdauer

    def _test(titel, deal):
        r = calculate_offer_positions(deal)
        print(f"\n{'='*60}")
        print(f"TEST: {titel}")
        print(f"{'='*60}")
        for p in r["positions"]:
            gesamt = p["quantity"] * p["price"]
            print(f"  {p['name']:50} {p['quantity']:6.0f} × {p['price']:8.2f} = {gesamt:9.2f} ({p['tax_rate']}%)")
        print(f"  {'─'*75}")
        print(f"  {'NETTO':>64}  {r['total_net']:9.2f}")
        print(f"  {'BRUTTO':>64}  {r['total_gross']:9.2f}")
        if r["hinweise"]:
            for h in r["hinweise"]:
                print(f"  ⚠ {h}")

    _test("Buchtelmobil 80 Pax ganztags Graz", {
        FIELD_PERSONENANZAHL: 80,
        FIELD_MIETDAUER: Mietdauer.GANZTAG,
        FIELD_INTERESSENSGEBIET: [{"id": Interesse.BUCHTELMOBIL}],
        FIELD_ZIEL_FORMATTED: "Graz, Steiermark",
    })

    _test("Pop-up Café halbtags Wien 50m2 (Nachmittag)", {
        FIELD_PERSONENANZAHL: 60,
        FIELD_MIETDAUER: Mietdauer.NACHMITTAG,
        FIELD_INTERESSENSGEBIET: [{"id": Interesse.POPUP_CAFE}],
        FIELD_POPUP_CAFE_GROESSE: "50m2",
    })

    _test("Teambuilding Backkurs 8 Pax (→ wird auf 12 hochgezogen)", {
        FIELD_PERSONENANZAHL: 8,
        FIELD_MIETDAUER: Mietdauer.VORMITTAG,
        FIELD_INTERESSENSGEBIET: [{"id": Interesse.TEAMBUILDING_BACKKURS}],
    })

    _test("Studio Miete ganztags 50 Pax → Warnung!", {
        FIELD_PERSONENANZAHL: 50,
        FIELD_MIETDAUER: Mietdauer.GANZTAG,
        FIELD_INTERESSENSGEBIET: [{"id": Interesse.STUDIO_MIETE}],
        FIELD_CATERING: 158,
    })

    _test("Weihnachtsfeier inkl. Backkurs 25 Pax", {
        FIELD_PERSONENANZAHL: 25,
        FIELD_MIETDAUER: Mietdauer.GANZTAG,
        FIELD_INTERESSENSGEBIET: [{"id": Interesse.WEIHNACHTSFEIER_BACK}],
    })
