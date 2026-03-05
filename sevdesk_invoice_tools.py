#!/usr/bin/env python3
"""
Sevdesk Invoice Tools
=====================
API-Wrapper für den Rechnungs- und Mahnungsworkflow.

Sevdesk-Rechnungstypen:
  RE  = Normale Rechnung (Schlussrechnung)
  AR  = Anzahlungsrechnung / Abschlagsrechnung
  MA  = Mahnung (Dunning)

Statuswerte (Invoice):
  100 = Entwurf
  200 = Versendet
  1000 = Teilbezahlt
  2000 = Bezahlt / Abgeschlossen

Hinweis: Storno erfolgt über /Invoice/{id}/cancelInvoice
→ Sevdesk erstellt automatisch eine Gutschrift/Stornorechnung.
"""

import json
import httpx
from datetime import date, timedelta
from config import SEVDESK_API_TOKEN

BASE_URL = "https://my.sevdesk.de/api/v1"
HEADERS  = {"Authorization": SEVDESK_API_TOKEN, "Content-Type": "application/json"}


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict:
    r = httpx.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _post(path: str, payload: dict) -> dict:
    r = httpx.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def _put(path: str, payload: dict) -> dict:
    r = httpx.put(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


# ─── Tool-Funktionen ──────────────────────────────────────────────────────────

def sevdesk_create_invoice(
    contact_id: str,
    positions: list,
    invoice_type: str = "RE",
    deal_id: int = None,
    reference_invoice_id: str = None,
    intro_text: str = "",
    outro_text: str = "",
    currency: str = "EUR",
) -> str:
    """
    Erstellt eine Rechnung als Entwurf in Sevdesk.

    Args:
        contact_id:           Sevdesk-Kontakt-ID
        positions:            Liste von {name, quantity, price, tax}
        invoice_type:         "RE" (Schlussrechnung) oder "AR" (Anzahlungsrechnung)
        deal_id:              Pipedrive-Deal-ID (für Referenz im Beschreibungsfeld)
        reference_invoice_id: Bei SR: Sevdesk-ID der zugehörigen AR
        intro_text:           Einleitungstext auf der Rechnung
        outro_text:           Schlusstext (Zahlungsziel, Bankdaten etc.)

    Returns:
        JSON-String mit invoice_id, invoice_number, sevdesk_link, invoice_date
    """
    invoice_date = date.today().isoformat()

    # Basis-Rechnungsobjekt
    invoice_payload = {
        "invoice": {
            "invoiceType": invoice_type,
            "status": "100",                # Entwurf
            "invoiceDate": invoice_date,
            "currency": currency,
            "contact": {"id": contact_id, "objectName": "Contact"},
            "contactPerson": {"id": "0", "objectName": "SevUser"},
            "header": f"Rechnung" if invoice_type == "RE" else "Anzahlungsrechnung",
            "headText": intro_text or _default_intro(invoice_type, reference_invoice_id),
            "footText": outro_text or "Zahlungsziel: 14 Tage netto. Vielen Dank für Ihr Vertrauen.",
            "taxType": "default",
            "mapAll": "true",
        },
        "invoicePosSave": [],
        "invoicePosDelete": None,
        "discountSave": [],
        "discountDelete": None,
    }

    # Positionen aufbauen
    for pos in positions:
        invoice_payload["invoicePosSave"].append({
            "objectName": "InvoicePos",
            "mapAll": "true",
            "quantity": str(pos.get("quantity", 1)),
            "price": str(pos.get("price", 0)),
            "name": pos.get("name", ""),
            "unity": {"id": "1", "objectName": "Unity"},
            "taxRate": str(pos.get("tax", 20)),
        })

    # Referenz auf Anzahlungsrechnung bei Schlussrechnung
    if reference_invoice_id:
        invoice_payload["invoice"]["origin"] = {
            "id": reference_invoice_id,
            "objectName": "Invoice",
        }

    resp = _post("/Invoice/Factory/saveInvoice", invoice_payload)
    obj  = resp.get("objects", {}).get("invoice", {})

    invoice_id     = obj.get("id", "")
    invoice_number = obj.get("invoiceNumber", "")
    sevdesk_link   = f"https://my.sevdesk.de/#/fi/invoice/{invoice_id}" if invoice_id else ""

    return json.dumps({
        "invoice_id":     invoice_id,
        "invoice_number": invoice_number,
        "invoice_type":   invoice_type,
        "invoice_date":   invoice_date,
        "sevdesk_link":   sevdesk_link,
        "status":         "draft",
    }, ensure_ascii=False)


def sevdesk_get_invoices_for_contact(contact_id: str) -> str:
    """
    Gibt alle Rechnungen eines Kontakts zurück.
    Nützlich um zu prüfen ob eine Anzahlungsrechnung existiert.

    Returns:
        JSON-String mit Liste von Rechnungen (id, number, type, status, amount)
    """
    resp = _get("/Invoice", params={
        "contact[id]":       contact_id,
        "contact[objectName]": "Contact",
        "limit":             50,
        "embed":             "invoicePos",
    })

    invoices = []
    for inv in resp.get("objects", []):
        invoices.append({
            "invoice_id":     inv.get("id"),
            "invoice_number": inv.get("invoiceNumber"),
            "invoice_type":   inv.get("invoiceType"),   # RE, AR, MA
            "status":         inv.get("status"),         # 100=Entwurf, 200=Versendet, 2000=Bezahlt
            "invoice_date":   inv.get("invoiceDate"),
            "sum_gross":      inv.get("sumGross"),
            "currency":       inv.get("currency"),
            "sevdesk_link":   f"https://my.sevdesk.de/#/fi/invoice/{inv.get('id')}",
        })

    return json.dumps({"invoices": invoices, "count": len(invoices)}, ensure_ascii=False)


def sevdesk_check_payment_status(invoice_id: str) -> str:
    """
    Prüft ob eine Rechnung bezahlt wurde.
    Status 2000 = bezahlt/abgeschlossen, 1000 = teilbezahlt.

    Returns:
        JSON mit status_code, is_paid, pay_date, sum_gross, sum_net
    """
    resp = _get(f"/Invoice/{invoice_id}")
    obj  = resp.get("objects", [{}])
    if isinstance(obj, list):
        obj = obj[0] if obj else {}

    status_code = int(obj.get("status", 100))
    is_paid = status_code >= 2000

    return json.dumps({
        "invoice_id":     invoice_id,
        "invoice_number": obj.get("invoiceNumber"),
        "status_code":    status_code,
        "is_paid":        is_paid,
        "pay_date":       obj.get("payDate"),
        "sum_gross":      obj.get("sumGross"),
        "invoice_date":   obj.get("invoiceDate"),
        "invoice_type":   obj.get("invoiceType"),
    }, ensure_ascii=False)


def sevdesk_cancel_invoice(invoice_id: str) -> str:
    """
    Storniert eine Rechnung in Sevdesk.
    Sevdesk erstellt automatisch eine Stornorechnung (Gutschrift).

    WICHTIG: Nur nach expliziter Slack-Freigabe aufrufen!

    Returns:
        JSON mit cancel_invoice_id, cancel_invoice_number, sevdesk_link
    """
    resp = _post(f"/Invoice/{invoice_id}/cancelInvoice", {})
    obj  = resp.get("objects", {})

    # Sevdesk gibt die neue Stornorechnung zurück
    if isinstance(obj, list):
        obj = obj[0] if obj else {}

    cancel_id     = obj.get("id", "")
    cancel_number = obj.get("invoiceNumber", "")
    sevdesk_link  = f"https://my.sevdesk.de/#/fi/invoice/{cancel_id}" if cancel_id else ""

    return json.dumps({
        "original_invoice_id":  invoice_id,
        "cancel_invoice_id":    cancel_id,
        "cancel_invoice_number": cancel_number,
        "sevdesk_link":         sevdesk_link,
        "status":               "cancelled",
    }, ensure_ascii=False)


def sevdesk_send_invoice(invoice_id: str, recipient_email: str, subject: str = "", body: str = "") -> str:
    """
    Versendet eine Rechnung per E-Mail über Sevdesk.

    WICHTIG: Nur nach expliziter Slack-Freigabe aufrufen!

    Args:
        invoice_id:       Sevdesk-Rechnungs-ID
        recipient_email:  E-Mail-Adresse des Empfängers
        subject:          Betreff (optional, Sevdesk-Standard wenn leer)
        body:             E-Mail-Text (optional)

    Returns:
        JSON mit success, message
    """
    payload = {
        "toEmail": recipient_email,
        "subject": subject or "Ihre Rechnung",
        "text": body or "Bitte finden Sie Ihre Rechnung im Anhang.",
        "sendType": "VM",    # VM = Versandt per Mail
        "copy": False,
    }

    resp = _post(f"/Invoice/{invoice_id}/sendViaEmail", payload)

    return json.dumps({
        "success":      True,
        "invoice_id":   invoice_id,
        "sent_to":      recipient_email,
        "message":      "Rechnung erfolgreich versendet.",
    }, ensure_ascii=False)


def sevdesk_create_dunning(invoice_id: str) -> str:
    """
    Erstellt eine Mahnung (Dunning) zu einer offenen Rechnung in Sevdesk.

    Sevdesk erstellt ein neues Mahndokument (Typ MA) mit Bezug auf die Rechnung.
    Die Mahnung ist zunächst ein Entwurf – kein Auto-Versand.

    Returns:
        JSON mit dunning_id, dunning_number, sevdesk_link
    """
    # Sevdesk Mahnungs-API: Mahnung über Remind-Endpoint
    resp = _post(f"/Invoice/{invoice_id}/remind", {
        "reminderDeadline": (date.today() + timedelta(days=7)).isoformat(),
        "reminderSendDate": date.today().isoformat(),
    })

    obj = resp.get("objects", {})
    if isinstance(obj, list):
        obj = obj[0] if obj else {}

    dunning_id     = obj.get("id", "")
    dunning_number = obj.get("invoiceNumber", obj.get("reminderNumber", ""))
    sevdesk_link   = f"https://my.sevdesk.de/#/fi/invoice/{dunning_id}" if dunning_id else ""

    return json.dumps({
        "dunning_id":     dunning_id,
        "dunning_number": dunning_number,
        "original_invoice_id": invoice_id,
        "sevdesk_link":   sevdesk_link,
        "status":         "draft",
        "deadline":       (date.today() + timedelta(days=7)).isoformat(),
    }, ensure_ascii=False)


def sevdesk_send_dunning(dunning_id: str, recipient_email: str, subject: str = "", body: str = "") -> str:
    """
    Versendet eine Mahnung per E-Mail.

    WICHTIG: Nur nach expliziter Slack-Freigabe aufrufen!
    """
    payload = {
        "toEmail": recipient_email,
        "subject": subject or "Zahlungserinnerung / Mahnung",
        "text": body or (
            "Bitte beachten Sie, dass die unten genannte Rechnung noch offen ist. "
            "Wir bitten Sie um rasche Begleichung des offenen Betrags."
        ),
        "sendType": "VM",
        "copy": False,
    }

    _post(f"/Invoice/{dunning_id}/sendViaEmail", payload)

    return json.dumps({
        "success":    True,
        "dunning_id": dunning_id,
        "sent_to":    recipient_email,
        "message":    "Mahnung erfolgreich versendet.",
    }, ensure_ascii=False)


def sevdesk_get_overdue_invoices() -> str:
    """
    Gibt alle offenen/versendeten Rechnungen zurück (Status 200 = versendet, nicht bezahlt).
    Wird vom täglichen Cron-Job für das Mahnwesen verwendet.

    Returns:
        JSON mit Liste offener Rechnungen
    """
    resp = _get("/Invoice", params={
        "status": "200",     # Versendet aber nicht bezahlt
        "invoiceType": "RE", # Nur Schlussrechnungen (keine Anzahlungen)
        "limit": 100,
    })

    invoices = []
    for inv in resp.get("objects", []):
        invoices.append({
            "invoice_id":     inv.get("id"),
            "invoice_number": inv.get("invoiceNumber"),
            "invoice_date":   inv.get("invoiceDate"),
            "sum_gross":      inv.get("sumGross"),
            "contact_id":     inv.get("contact", {}).get("id"),
            "sevdesk_link":   f"https://my.sevdesk.de/#/fi/invoice/{inv.get('id')}",
        })

    return json.dumps({"overdue_candidates": invoices, "count": len(invoices)}, ensure_ascii=False)


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _default_intro(invoice_type: str, reference_invoice_id: str = None) -> str:
    if invoice_type == "AR":
        return (
            "Vielen Dank für Ihren Auftrag. "
            "Gemäß unserer Vereinbarung stellen wir Ihnen hiermit die Anzahlung in Rechnung."
        )
    if invoice_type == "RE" and reference_invoice_id:
        return (
            "Hiermit stellen wir Ihnen die Schlussrechnung für die erbrachten Leistungen. "
            "Die geleistete Anzahlung wurde bereits berücksichtigt."
        )
    return "Hiermit stellen wir Ihnen folgende Leistungen in Rechnung."


# ─── Tool-Definitionen für Claude ─────────────────────────────────────────────

SEVDESK_INVOICE_TOOL_DEFINITIONS = [
    {
        "name": "sevdesk_create_invoice",
        "description": (
            "Erstellt eine Rechnung als Entwurf in Sevdesk. "
            "Für Anzahlungsrechnung invoice_type='AR', für Schlussrechnung invoice_type='RE'. "
            "Kein Versand – nur Entwurf. Versand erst nach Slack-Freigabe via sevdesk_send_invoice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id":           {"type": "string", "description": "Sevdesk-Kontakt-ID"},
                "positions":            {
                    "type": "array",
                    "description": "Rechnungspositionen",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":     {"type": "string"},
                            "quantity": {"type": "number"},
                            "price":    {"type": "number"},
                            "tax":      {"type": "number", "description": "Steuersatz in %, Standard 20"},
                        },
                        "required": ["name", "quantity", "price"],
                    },
                },
                "invoice_type":         {"type": "string", "enum": ["RE", "AR"], "description": "RE=Schlussrechnung, AR=Anzahlungsrechnung"},
                "deal_id":              {"type": "integer", "description": "Pipedrive-Deal-ID für Referenz"},
                "reference_invoice_id": {"type": "string", "description": "Bei SR: Sevdesk-ID der Anzahlungsrechnung"},
                "intro_text":           {"type": "string"},
                "outro_text":           {"type": "string"},
                "currency":             {"type": "string", "default": "EUR"},
            },
            "required": ["contact_id", "positions", "invoice_type"],
        },
    },
    {
        "name": "sevdesk_get_invoices_for_contact",
        "description": "Gibt alle Rechnungen eines Sevdesk-Kontakts zurück. Zum Prüfen ob AR vorhanden und ob bezahlt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "Sevdesk-Kontakt-ID"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "sevdesk_check_payment_status",
        "description": "Prüft ob eine Rechnung in Sevdesk als bezahlt markiert ist. Status 2000 = bezahlt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string", "description": "Sevdesk-Rechnungs-ID"},
            },
            "required": ["invoice_id"],
        },
    },
    {
        "name": "sevdesk_cancel_invoice",
        "description": (
            "Storniert eine Rechnung in Sevdesk (erstellt Stornorechnung). "
            "NUR nach expliziter Slack-Freigabe aufrufen! "
            "Sevdesk erstellt automatisch eine Gutschrift."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string", "description": "Sevdesk-ID der zu stornierenden Rechnung"},
            },
            "required": ["invoice_id"],
        },
    },
    {
        "name": "sevdesk_send_invoice",
        "description": (
            "Versendet eine Rechnung per E-Mail über Sevdesk. "
            "NUR nach expliziter menschlicher Freigabe über Slack aufrufen!"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id":      {"type": "string"},
                "recipient_email": {"type": "string"},
                "subject":         {"type": "string"},
                "body":            {"type": "string"},
            },
            "required": ["invoice_id", "recipient_email"],
        },
    },
    {
        "name": "sevdesk_create_dunning",
        "description": "Erstellt eine Mahnung zu einer offenen Rechnung als Entwurf. Kein Auto-Versand.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string", "description": "Sevdesk-ID der offenen Rechnung"},
            },
            "required": ["invoice_id"],
        },
    },
    {
        "name": "sevdesk_send_dunning",
        "description": (
            "Versendet eine Mahnung per E-Mail. "
            "NUR nach expliziter Slack-Freigabe aufrufen!"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dunning_id":      {"type": "string"},
                "recipient_email": {"type": "string"},
                "subject":         {"type": "string"},
                "body":            {"type": "string"},
            },
            "required": ["dunning_id", "recipient_email"],
        },
    },
    {
        "name": "sevdesk_get_overdue_invoices",
        "description": "Gibt alle versendeten, noch unbezahlten Rechnungen zurück (für Mahnwesen-Cron-Job).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

SEVDESK_INVOICE_TOOL_MAP = {
    "sevdesk_create_invoice":           lambda i: sevdesk_create_invoice(**i),
    "sevdesk_get_invoices_for_contact": lambda i: sevdesk_get_invoices_for_contact(**i),
    "sevdesk_check_payment_status":     lambda i: sevdesk_check_payment_status(**i),
    "sevdesk_cancel_invoice":           lambda i: sevdesk_cancel_invoice(**i),
    "sevdesk_send_invoice":             lambda i: sevdesk_send_invoice(**i),
    "sevdesk_create_dunning":           lambda i: sevdesk_create_dunning(**i),
    "sevdesk_send_dunning":             lambda i: sevdesk_send_dunning(**i),
    "sevdesk_get_overdue_invoices":     lambda i: sevdesk_get_overdue_invoices(),
}
