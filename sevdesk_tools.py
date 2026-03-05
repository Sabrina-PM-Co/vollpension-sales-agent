#!/usr/bin/env python3
"""
Sevdesk API Tools
=================
Wrapper-Funktionen für die Sevdesk REST API v1.
Benötigt: SEVDESK_API_TOKEN in .env

Docs: https://api.sevdesk.de/ (OpenAPI unter https://my.sevdesk.de/api/v1)

Wichtige Hinweise zu Sevdesk-Endpunkten:
- Kontakte:  GET/POST /Contact
- Angebote:  POST /Order (orderType = "AN")
- Positionen: POST /OrderPos
- Status-Werte für Angebot: 100 = Entwurf, 200 = Versendet, 300 = Angenommen
"""

import json
import httpx
from typing import Any, Optional
from config import SEVDESK_API_TOKEN

BASE = "https://my.sevdesk.de/api/v1"


def _headers() -> dict:
    return {
        "Authorization": SEVDESK_API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        return f"Sevdesk API Fehler {code}: {detail}"
    if isinstance(e, httpx.TimeoutException):
        return "Sevdesk Timeout – bitte erneut versuchen."
    return f"Unerwarteter Fehler ({type(e).__name__}): {e}"


# ─── Tools ────────────────────────────────────────────────────────────────────

def sevdesk_search_contact(name: str = "", email: str = "") -> str:
    """
    Sucht nach einem bestehenden Sevdesk-Kontakt anhand von Name oder E-Mail.

    Args:
        name:  Firmen- oder Personenname (Teilstring, case-insensitive).
        email: E-Mail-Adresse des Kontakts.

    Returns:
        JSON-String mit Liste der Treffer (id, name, email, customerNumber).
        Leer = kein bestehender Kontakt gefunden.
    """
    try:
        params: dict = {"depth": "0", "limit": "10"}
        if name:
            params["name"] = name
        if email:
            params["email"] = email
        with httpx.Client() as c:
            r = c.get(f"{BASE}/Contact", headers=_headers(), params=params, timeout=30)
            r.raise_for_status()
            contacts = r.json().get("objects", [])

        result = [
            {
                "id": ct.get("id"),
                "name": ct.get("name"),
                "surename": ct.get("surename", ""),
                "familyname": ct.get("familyname", ""),
                "email": ct.get("email", ""),
                "customerNumber": ct.get("customerNumber", ""),
                "category": ct.get("category", {}).get("translation", "") if ct.get("category") else "",
            }
            for ct in contacts
        ]
        return json.dumps({"count": len(result), "contacts": result}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def sevdesk_create_contact(
    name: str,
    email: str = "",
    phone: str = "",
    street: str = "",
    city: str = "",
    zip_code: str = "",
    country: str = "DE",
    contact_type: str = "COMPANY",  # COMPANY oder PERSON
) -> str:
    """
    Erstellt einen neuen Kontakt (Firma oder Person) in Sevdesk.

    Args:
        name:         Firmenname ODER Vorname+Nachname für Personen.
        email:        E-Mail-Adresse.
        phone:        Telefonnummer.
        street:       Straße + Hausnummer.
        city:         Stadt.
        zip_code:     PLZ.
        country:      Länderkürzel (Standard: DE).
        contact_type: "COMPANY" für Firma, "PERSON" für Einzelperson.

    Returns:
        JSON-String mit id und Daten des neu erstellten Kontakts.
    """
    try:
        # Sevdesk category: 3 = Kunde (Debitor)
        payload: dict = {
            "name": name,
            "email": email,
            "phone": phone,
            "status": "100",  # Aktiv
            "category": {"id": "3", "objectName": "Category"},  # Kunde
        }

        with httpx.Client() as c:
            # Kontakt anlegen
            r = c.post(
                f"{BASE}/Contact",
                headers=_headers(),
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            contact = r.json().get("objects", {})
            contact_id = contact.get("id")

            # Adresse hinzufügen (wenn vorhanden)
            if street or city or zip_code:
                addr_payload = {
                    "contact": {"id": str(contact_id), "objectName": "Contact"},
                    "street": street,
                    "city": city,
                    "zip": zip_code,
                    "country": {"id": "1", "objectName": "StaticCountry"},  # DE=1
                    "category": {"id": "48", "objectName": "Category"},  # Hauptadresse
                }
                c.post(f"{BASE}/ContactAddress", headers=_headers(), json=addr_payload, timeout=30)

        return json.dumps(
            {"status": "created", "contact_id": contact_id, "name": name},
            indent=2, ensure_ascii=False
        )
    except Exception as e:
        return _err(e)


def sevdesk_create_offer_draft(
    contact_id: str,
    deal_title: str,
    positions: list,
    intro_text: str = "",
    outro_text: str = "",
    currency: str = "EUR",
) -> str:
    """
    Erstellt ein Angebot (Entwurf) in Sevdesk und verknüpft es mit einem Kontakt.

    Args:
        contact_id:  Sevdesk-Kontakt-ID (aus sevdesk_search_contact oder sevdesk_create_contact).
        deal_title:  Titel / Betreff des Angebots (wird als Angebots-Header angezeigt).
        positions:   Liste von Positionen, jede als Dict:
                     [
                       {
                         "name": "Beratungspaket S",
                         "quantity": 1,
                         "price": 1500.00,
                         "unit": "Stk.",           # optional
                         "tax_rate": 20,           # MwSt in Prozent (0, 10, 20)
                         "description": "...",     # optional
                       }, ...
                     ]
        intro_text:  Einleitungstext des Angebots (optional).
        outro_text:  Schlusstext / Zahlungsbedingungen (optional).
        currency:    Währung (Standard: EUR).

    Returns:
        JSON-String mit Angebots-ID, Angebotsnummer und direktem Link in Sevdesk.
    """
    try:
        from datetime import date, timedelta

        today = date.today().isoformat()
        valid_until = (date.today() + timedelta(days=30)).isoformat()

        # Angebot anlegen (Status 100 = Entwurf)
        order_payload = {
            "header": deal_title,
            "headText": intro_text or "Vielen Dank für Ihr Interesse. Gerne unterbreiten wir Ihnen folgendes Angebot:",
            "footText": outro_text or "Bei Fragen stehen wir Ihnen jederzeit zur Verfügung.",
            "orderDate": today,
            "deliveryDate": valid_until,
            "status": 100,              # Integer! 100 = Entwurf
            "orderType": "AN",          # AN = Angebot
            "currency": currency,
            "taxType": "default",
            "smallSettlement": False,
            "contact": {"id": str(contact_id), "objectName": "Contact"},
            "showNet": 1,               # Integer!
        }

        with httpx.Client() as c:
            r = c.post(
                f"{BASE}/Order",
                headers=_headers(),
                json=order_payload,
                timeout=30,
            )
            r.raise_for_status()
            order_data = r.json().get("objects", {})
            order_id = order_data.get("id")
            order_number = order_data.get("orderNumber", "")

            # Positionen anlegen
            for pos in positions:
                pos_payload = {
                    "order": {"id": str(order_id), "objectName": "Order"},
                    "name": pos.get("name", "Position"),
                    "quantity": float(pos.get("quantity", 1)),
                    "price": float(pos.get("price", 0.0)),
                    "unity": {"id": "1", "objectName": "Unity"},  # 1 = Stück
                    "taxRate": float(pos.get("tax_rate", 20)),
                    "text": pos.get("description", ""),
                }
                c.post(
                    f"{BASE}/OrderPos",
                    headers=_headers(),
                    json=pos_payload,
                    timeout=30,
                )

        # Direktlink zum Angebot in Sevdesk
        sevdesk_link = f"https://my.sevdesk.de/#/fi/order/{order_id}"

        return json.dumps(
            {
                "status": "draft_created",
                "order_id": order_id,
                "order_number": order_number,
                "sevdesk_link": sevdesk_link,
                "valid_until": valid_until,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        return _err(e)


def sevdesk_get_offer(order_id: str) -> str:
    """
    Ruft Details eines bestehenden Sevdesk-Angebots ab.

    Args:
        order_id: ID des Angebots (aus sevdesk_create_offer_draft).

    Returns:
        JSON-String mit Angebotsdaten inkl. Status und Positionen.
    """
    try:
        with httpx.Client() as c:
            r = c.get(f"{BASE}/Order/{order_id}", headers=_headers(), timeout=30)
            r.raise_for_status()
            data = r.json().get("objects", {})
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def sevdesk_delete_offer(order_id: str) -> str:
    """
    Löscht ein Sevdesk-Angebot (Entwurf) unwiderruflich.

    Wichtig: Nur Angebote im Status Entwurf (100) können gelöscht werden.
    Versendete oder angenommene Angebote werden von Sevdesk abgelehnt.

    Args:
        order_id: Sevdesk-Angebots-ID (aus sevdesk_create_offer_draft oder sevdesk_get_offer).

    Returns:
        JSON-String mit Bestätigung oder Fehlermeldung.
    """
    try:
        with httpx.Client() as c:
            # Zuerst Status prüfen – nur Entwürfe (100) löschen
            r_check = c.get(f"{BASE}/Order/{order_id}", headers=_headers(), timeout=30)
            r_check.raise_for_status()
            offer = r_check.json().get("objects", {})
            status_code = str(offer.get("status", ""))
            order_number = offer.get("orderNumber", order_id)

            if status_code not in ("100", ""):
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"Angebot {order_number} hat Status {status_code} (nicht Entwurf) "
                        "und kann daher nicht geloescht werden. "
                        "Nur Entwaerfe (Status 100) duerfen geloescht werden."
                    ),
                }, ensure_ascii=False)

            # Löschen
            r_del = c.delete(f"{BASE}/Order/{order_id}", headers=_headers(), timeout=30)
            r_del.raise_for_status()

        return json.dumps({
            "status": "deleted",
            "order_id": order_id,
            "order_number": order_number,
            "message": f"Angebot {order_number} wurde erfolgreich geloescht.",
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


# ─── Verfügbare Tools für Claude ──────────────────────────────────────────────

SEVDESK_TOOL_DEFINITIONS = [
    {
        "name": "sevdesk_search_contact",
        "description": (
            "Sucht in Sevdesk nach einem bestehenden Kontakt (Firma oder Person) "
            "anhand von Name oder E-Mail. Immer ZUERST ausführen, bevor ein neuer "
            "Kontakt angelegt wird, um Duplikate zu vermeiden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":  {"type": "string", "description": "Firmen- oder Personenname (Teilsuche möglich)."},
                "email": {"type": "string", "description": "E-Mail-Adresse des Kontakts."},
            },
        },
    },
    {
        "name": "sevdesk_create_contact",
        "description": (
            "Erstellt einen neuen Kontakt in Sevdesk. "
            "Nur aufrufen, wenn sevdesk_search_contact keinen Treffer geliefert hat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":         {"type": "string",  "description": "Firmen- oder Personenname."},
                "email":        {"type": "string",  "description": "E-Mail-Adresse."},
                "phone":        {"type": "string",  "description": "Telefonnummer."},
                "street":       {"type": "string",  "description": "Straße + Hausnummer."},
                "city":         {"type": "string",  "description": "Stadt."},
                "zip_code":     {"type": "string",  "description": "Postleitzahl."},
                "country":      {"type": "string",  "description": "Länderkürzel, Standard: DE."},
                "contact_type": {"type": "string",  "description": "COMPANY oder PERSON."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "sevdesk_create_offer_draft",
        "description": (
            "Erstellt ein Angebot als Entwurf (Status: Entwurf) in Sevdesk. "
            "Befüllt Kopf, Positionen, Gültigkeitsdatum und verknüpft es mit dem Kontakt. "
            "Gibt Angebots-ID, -Nummer und direkten Sevdesk-Link zurück."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "Sevdesk-Kontakt-ID (aus Suche oder Erstellung).",
                },
                "deal_title": {
                    "type": "string",
                    "description": "Betreff / Titel des Angebots, z.B. 'Angebot – CRM-Einführung Muster GmbH'.",
                },
                "positions": {
                    "type": "array",
                    "description": "Liste der Angebotspositionen.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":        {"type": "string",  "description": "Bezeichnung der Position."},
                            "quantity":    {"type": "number",  "description": "Menge."},
                            "price":       {"type": "number",  "description": "Einzelpreis in EUR (netto)."},
                            "unit":        {"type": "string",  "description": "Einheit, z.B. Std., Stk., Paket."},
                            "tax_rate":    {"type": "integer", "description": "MwSt-Satz: 0, 10 oder 20."},
                            "description": {"type": "string",  "description": "Positionsbeschreibung (optional)."},
                        },
                        "required": ["name", "quantity", "price"],
                    },
                },
                "intro_text": {"type": "string", "description": "Einleitungstext (optional)."},
                "outro_text": {"type": "string", "description": "Schlusstext / Zahlungsbedingungen (optional)."},
                "currency":   {"type": "string", "description": "Währung, Standard: EUR."},
            },
            "required": ["contact_id", "deal_title", "positions"],
        },
    },
    {
        "name": "sevdesk_get_offer",
        "description": "Ruft Details eines bestehenden Sevdesk-Angebots ab.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Sevdesk-Angebots-ID."},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "sevdesk_delete_offer",
        "description": (
            "Loescht ein Angebots-Entwurf in Sevdesk unwiderruflich. "
            "Nur aufrufen wenn der User explizit bittet, das Angebot zu loeschen oder zu verwerfen. "
            "Prueft automatisch ob das Angebot noch im Entwurfsstatus ist – "
            "versendete oder angenommene Angebote koennen NICHT geloescht werden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Sevdesk-Angebots-ID des zu loeschenden Entwurfs.",
                },
            },
            "required": ["order_id"],
        },
    },
]

SEVDESK_TOOL_MAP = {
    "sevdesk_search_contact":     lambda d: sevdesk_search_contact(d.get("name", ""), d.get("email", "")),
    "sevdesk_create_contact":     lambda d: sevdesk_create_contact(**d),
    "sevdesk_create_offer_draft": lambda d: sevdesk_create_offer_draft(**d),
    "sevdesk_get_offer":          lambda d: sevdesk_get_offer(d["order_id"]),
    "sevdesk_delete_offer":       lambda d: sevdesk_delete_offer(d["order_id"]),
}
