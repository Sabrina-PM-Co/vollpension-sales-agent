#!/usr/bin/env python3
"""
Pipedrive API Tools
===================
Wrapper-Funktionen für die Pipedrive REST API v1.
Benötigt: PIPEDRIVE_API_TOKEN und PIPEDRIVE_COMPANY_DOMAIN in .env

Docs: https://developers.pipedrive.com/docs/api/v1
"""

import json
import httpx
from typing import Any, Optional
from config import PIPEDRIVE_API_TOKEN, PIPEDRIVE_DOMAIN

BASE = f"https://{PIPEDRIVE_DOMAIN}.pipedrive.com/api/v1"


def _params(extra: Optional[dict] = None) -> dict:
    p = {"api_token": PIPEDRIVE_API_TOKEN}
    if extra:
        p.update(extra)
    return p


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        return f"Pipedrive API Fehler {code}: {detail}"
    if isinstance(e, httpx.TimeoutException):
        return "Pipedrive Timeout – bitte erneut versuchen."
    return f"Unerwarteter Fehler ({type(e).__name__}): {e}"


# ─── Tools ────────────────────────────────────────────────────────────────────

def pipedrive_get_deal(deal_id: int) -> str:
    """
    Ruft alle Details eines Pipedrive-Deals ab.

    Args:
        deal_id: Numerische ID des Deals (aus dem Webhook-Payload).

    Returns:
        JSON-String mit allen Deal-Feldern (Name, Person, Org, Custom Fields, etc.)
    """
    try:
        with httpx.Client() as c:
            r = c.get(f"{BASE}/deals/{deal_id}", params=_params(), timeout=30)
            r.raise_for_status()
            data = r.json().get("data", {})
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def pipedrive_update_deal(deal_id: int, fields: dict) -> str:
    """
    Aktualisiert Felder eines Pipedrive-Deals (PATCH).
    Nur übergebene Felder werden geändert.

    Args:
        deal_id: ID des Deals.
        fields:  Dict mit Feldern, z.B.
                 {"status": "open", "custom_field_abc123": "Wert"}
                 Custom-Field-Keys findest du in Pipedrive → Einstellungen → Custom Fields.

    Returns:
        JSON-String des aktualisierten Deals.
    """
    try:
        with httpx.Client() as c:
            r = c.put(
                f"{BASE}/deals/{deal_id}",
                params=_params(),
                json=fields,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("data", {})
        return json.dumps({"status": "updated", "deal": data}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def pipedrive_get_person(person_id: int) -> str:
    """
    Ruft Kontaktdaten der dem Deal zugeordneten Person ab.

    Args:
        person_id: ID der Person (aus deal['person_id']['value']).

    Returns:
        JSON-String mit Name, E-Mail, Telefon, Organisation.
    """
    try:
        with httpx.Client() as c:
            r = c.get(f"{BASE}/persons/{person_id}", params=_params(), timeout=30)
            r.raise_for_status()
            data = r.json().get("data", {})
        # Relevante Felder extrahieren
        result = {
            "id": data.get("id"),
            "name": data.get("name"),
            "email": data.get("email", [{}])[0].get("value") if data.get("email") else None,
            "phone": data.get("phone", [{}])[0].get("value") if data.get("phone") else None,
            "org_id": data.get("org_id", {}).get("value") if data.get("org_id") else None,
            "org_name": data.get("org_id", {}).get("name") if data.get("org_id") else None,
        }
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def pipedrive_get_organization(org_id: int) -> str:
    """
    Ruft Firmendaten der dem Deal zugeordneten Organisation ab.

    Args:
        org_id: ID der Organisation (aus deal['org_id']['value']).

    Returns:
        JSON-String mit Firmenname, Adresse, Website.
    """
    try:
        with httpx.Client() as c:
            r = c.get(f"{BASE}/organizations/{org_id}", params=_params(), timeout=30)
            r.raise_for_status()
            data = r.json().get("data", {})
        result = {
            "id": data.get("id"),
            "name": data.get("name"),
            "address": data.get("address"),
            "cc_email": data.get("cc_email"),
        }
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def pipedrive_get_deal_notes(deal_id: int) -> str:
    """
    Ruft alle Notizen (Notes) eines Pipedrive-Deals ab.

    Notizen sind KEIN Custom Field – sie werden über einen eigenen Endpunkt
    abgerufen: GET /deals/{id}/notes
    Hier landet z.B. der Freitext aus dem Kontaktformular (Kundenanfrage).

    Args:
        deal_id: ID des Deals.

    Returns:
        JSON-String mit Liste aller Notizen, sortiert nach Erstellungsdatum.
        Jede Notiz enthält: id, content (Text), add_time, user (Ersteller).
        Gibt {"notes": [], "count": 0} zurück, wenn keine Notizen vorhanden.
    """
    try:
        with httpx.Client() as c:
            r = c.get(
                f"{BASE}/deals/{deal_id}/notes",
                params=_params({"sort": "add_time DESC", "limit": 50}),
                timeout=30,
            )
            r.raise_for_status()
            items = r.json().get("data") or []

        notes = [
            {
                "id": n.get("id"),
                "content": n.get("content", "").strip(),
                "add_time": n.get("add_time"),
                "update_time": n.get("update_time"),
                "user": n.get("user", {}).get("name") if n.get("user") else None,
            }
            for n in items
            if n.get("content", "").strip()  # leere Notizen überspringen
        ]
        return json.dumps({"notes": notes, "count": len(notes)}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def pipedrive_list_products() -> str:
    """
    Listet alle verfügbaren Produkte aus dem Pipedrive-Produktkatalog auf.

    Returns:
        JSON-String mit allen Produkten (id, name, code, unit, tax, prices).
        Prices enthält Preis und Währung je Produkt.
    """
    try:
        with httpx.Client() as c:
            r = c.get(f"{BASE}/products", params=_params({"limit": 100}), timeout=30)
            r.raise_for_status()
            items = r.json().get("data") or []
        products = [
            {
                "id":          p.get("id"),
                "name":        p.get("name"),
                "code":        p.get("code"),
                "description": p.get("description"),
                "unit":        p.get("unit"),
                "tax":         p.get("tax"),
                "prices":      p.get("prices"),
            }
            for p in items
        ]
        return json.dumps({"products": products, "count": len(products)}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def pipedrive_add_deal_product(
    deal_id: int,
    product_id: int,
    item_price: float,
    quantity: int = 1,
    discount: float = 0.0,
) -> str:
    """
    Fügt ein Produkt aus dem Pipedrive-Katalog zu einem Deal hinzu.

    Args:
        deal_id:    ID des Deals.
        product_id: ID des Produkts (aus pipedrive_list_products).
        item_price: Preis pro Einheit in EUR.
        quantity:   Menge (Standard: 1).
        discount:   Rabatt in Prozent (Standard: 0).

    Returns:
        JSON-String mit Status und den hinzugefügten Deal-Produkt-Daten.
    """
    try:
        with httpx.Client() as c:
            r = c.post(
                f"{BASE}/deals/{deal_id}/products",
                params=_params(),
                json={
                    "product_id":          product_id,
                    "item_price":          item_price,
                    "quantity":            quantity,
                    "discount_percentage": discount,
                    "enabled_flag":        1,
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("data", {})
        return json.dumps({"status": "added", "deal_product": data}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _err(e)


# ─── Verfügbare Tools für Claude ──────────────────────────────────────────────

PIPEDRIVE_TOOL_DEFINITIONS = [
    {
        "name": "pipedrive_get_deal",
        "description": (
            "Ruft alle Details eines Pipedrive-Deals ab, inkl. Kontaktperson, "
            "Organisation, Custom Fields und Pipeline-Status. "
            "Immer zuerst aufrufen, um die Deal-Daten zu laden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id": {
                    "type": "integer",
                    "description": "Numerische ID des Pipedrive-Deals."
                }
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "pipedrive_update_deal",
        "description": (
            "Aktualisiert Felder eines Pipedrive-Deals. "
            "Verwenden, um Custom Fields oder Status nach der Lead-Qualifizierung zu befüllen, "
            "z.B. 'sevdesk_angebot_nr' oder 'angebot_status'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "integer", "description": "ID des Deals."},
                "fields": {
                    "type": "object",
                    "description": (
                        "Dict mit zu aktualisierenden Feldern. "
                        "Beispiel: {\"title\": \"Neuer Name\", \"custom_field_key\": \"Wert\"}"
                    ),
                }
            },
            "required": ["deal_id", "fields"],
        },
    },
    {
        "name": "pipedrive_get_person",
        "description": (
            "Ruft Kontaktdaten (Name, E-Mail, Telefon) der dem Deal zugeordneten Person ab. "
            "Benötigt, um den Ansprechpartner für das Sevdesk-Angebot zu ermitteln."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id": {
                    "type": "integer",
                    "description": "ID der Person aus dem Deal (deal['person_id']['value'])."
                }
            },
            "required": ["person_id"],
        },
    },
    {
        "name": "pipedrive_get_organization",
        "description": (
            "Ruft Firmendaten (Name, Adresse) der dem Deal zugeordneten Organisation ab. "
            "Benötigt für die Angebotserstellung in Sevdesk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "org_id": {
                    "type": "integer",
                    "description": "ID der Organisation aus dem Deal (deal['org_id']['value'])."
                }
            },
            "required": ["org_id"],
        },
    },
    {
        "name": "pipedrive_get_deal_notes",
        "description": (
            "Ruft alle Notizen (Notes) eines Pipedrive-Deals ab. "
            "Hier steht der Freitext der Kundenanfrage aus dem Kontaktformular. "
            "Aufrufen nach pipedrive_get_deal, um die Anfrage-Details des Kunden zu lesen "
            "(Wünsche, Anlass, Personenzahl-Hinweise, Datum, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id": {
                    "type": "integer",
                    "description": "ID des Deals (gleiche ID wie bei pipedrive_get_deal)."
                }
            },
            "required": ["deal_id"],
        },
    },
    {
        "name": "pipedrive_list_products",
        "description": (
            "Listet alle verfügbaren Produkte aus dem Pipedrive-Produktkatalog auf. "
            "Gibt id, name, unit, tax und prices (Preis/Währung) zurück. "
            "Aufrufen bevor Produkte zu einem Deal hinzugefügt werden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "pipedrive_add_deal_product",
        "description": (
            "Fügt ein Produkt aus dem Pipedrive-Katalog zu einem Deal hinzu. "
            "Benötigt product_id (aus pipedrive_list_products) und item_price. "
            "Für jedes passende Produkt separat aufrufen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id":    {"type": "integer", "description": "ID des Deals."},
                "product_id": {"type": "integer", "description": "ID des Produkts aus dem Katalog."},
                "item_price": {"type": "number",  "description": "Preis pro Einheit in EUR."},
                "quantity":   {"type": "integer", "description": "Menge (Standard: 1)."},
                "discount":   {"type": "number",  "description": "Rabatt in Prozent (Standard: 0)."},
            },
            "required": ["deal_id", "product_id", "item_price"],
        },
    },
]

PIPEDRIVE_TOOL_MAP = {
    "pipedrive_get_deal":          lambda d: pipedrive_get_deal(d["deal_id"]),
    "pipedrive_update_deal":       lambda d: pipedrive_update_deal(d["deal_id"], d["fields"]),
    "pipedrive_get_person":        lambda d: pipedrive_get_person(d["person_id"]),
    "pipedrive_get_organization":  lambda d: pipedrive_get_organization(d["org_id"]),
    "pipedrive_get_deal_notes":    lambda d: pipedrive_get_deal_notes(d["deal_id"]),
    "pipedrive_list_products":     lambda d: pipedrive_list_products(),
    "pipedrive_add_deal_product":  lambda d: pipedrive_add_deal_product(
        d["deal_id"], d["product_id"], d["item_price"],
        d.get("quantity", 1), d.get("discount", 0.0),
    ),
}
