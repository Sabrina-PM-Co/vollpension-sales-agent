#!/usr/bin/env python3
"""
Slack Approval Handler
======================
Verwaltet alle Freigabeanfragen über Slack.

Designprinzipien:
  - OR-Logik: Eine von zwei Personen genügt zur Freigabe
  - Sonderfall: Nur Person 2 (Buchhaltung) bei unbezahlter AR
  - Text-Trigger: "freigabe", "freigeben", "ok", "ja", "approved" → Agent handelt
  - Kein Auto-Versand ohne menschliche Bestätigung
  - Slack-Nachrichten werden nach Entscheidung aktualisiert (Status-Update)

Voraussetzungen (Slack App):
  - Bot Token Scopes: chat:write, chat:write.public, users:read
  - Event Subscriptions: message.channels, message.im (für Text-Trigger)
  - Interactivity: aktiviert (für Buttons)
"""

import json
import os
import re
import httpx
from config import (
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_OFFERS,
    SLACK_CHANNEL_INVOICES,
    SLACK_PERSON1_ID,
    SLACK_PERSON2_ID,
)

def _channel_for(request_type: str) -> str:
    """Gibt den richtigen Slack-Channel je nach Request-Typ zurück."""
    if request_type == "offer":
        return SLACK_CHANNEL_OFFERS
    return SLACK_CHANNEL_INVOICES
from state_manager import (
    get_approval_request,
    set_approval_slack_ref,
    approve_request,
    set_awaiting_changes,
    append_feedback,
    get_awaiting_changes_by_ts,
    get_pending_approval_by_ts,
)

SLACK_API = "https://slack.com/api"
HEADERS   = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type":  "application/json",
}

# Wörter die als "Freigabe" interpretiert werden
APPROVAL_KEYWORDS = re.compile(
    r"\b(freigabe|freigeben|freigegeben|ok|okay|ja|approved|bestätigt|bestätigen|senden|versenden|abschicken)\b",
    re.IGNORECASE | re.UNICODE,
)

# Wörter die als "Ablehnung" interpretiert werden
REJECTION_KEYWORDS = re.compile(
    r"\b(ablehnen|abgelehnt|nein|stop|nicht senden|korrigieren|ändern|warten)\b",
    re.IGNORECASE | re.UNICODE,
)


# ─── Slack API Helpers ────────────────────────────────────────────────────────

def _slack_post(method: str, payload: dict) -> dict:
    r = httpx.post(f"{SLACK_API}/{method}", headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API Fehler [{method}]: {data.get('error')}")
    return data


def _slack_update(channel: str, ts: str, blocks: list, text: str = ""):
    _slack_post("chat.update", {
        "channel": channel,
        "ts":      ts,
        "text":    text,
        "blocks":  blocks,
    })


def _build_action_buttons(request_id: int, sevdesk_link: str) -> dict:
    """Erstellt den wiederverwendbaren Button-Block: Freigeben | Ändern | Sevdesk öffnen."""
    return {
        "type": "actions",
        "block_id": f"approval_{request_id}",
        "elements": [
            {
                "type":      "button",
                "text":      {"type": "plain_text", "text": "✅ Freigeben & Senden"},
                "style":     "primary",
                "action_id": "approve",
                "value":     str(request_id),
                "confirm": {
                    "title":   {"type": "plain_text", "text": "Wirklich freigeben?"},
                    "text":    {"type": "plain_text", "text": "Das Dokument wird versendet."},
                    "confirm": {"type": "plain_text", "text": "Ja, senden"},
                    "deny":    {"type": "plain_text", "text": "Abbrechen"},
                },
            },
            {
                "type":      "button",
                "text":      {"type": "plain_text", "text": "✏️ Ändern"},
                "action_id": "request_changes",
                "value":     str(request_id),
            },
            {
                "type":      "button",
                "text":      {"type": "plain_text", "text": "🔗 In Sevdesk öffnen"},
                "url":       sevdesk_link,
                "action_id": "open_sevdesk",
            },
        ],
    }


# ─── Approval-Nachricht senden ────────────────────────────────────────────────

def send_approval_request(
    request_id: int,
    request_type: str,
    deal_title: str,
    invoice_number: str,
    invoice_amount: float,
    sevdesk_link: str,
    contact_name: str,
    contact_email: str,
    notify_person1: bool = True,
    notify_person2: bool = True,
    warning_text: str = "",
) -> dict:
    """
    Postet eine Freigabeanfrage in den Slack-Freigabe-Channel.

    Bei Sonderfall (AR nicht gefunden/bezahlt): notify_person1=False, nur Buchhaltung.

    Returns:
        {"channel": ..., "ts": ...} – Referenz für spätere Updates
    """
    type_labels = {
        "ar_invoice": "Anzahlungsrechnung",
        "sr_invoice": "Schlussrechnung",
        "sr_storno":  "⚠️ Schlussrechnung + AR-Storno",
        "dunning":    "Mahnung",
    }
    emoji_map = {
        "ar_invoice": "💶",
        "sr_invoice": "🧾",
        "sr_storno":  "⚠️",
        "dunning":    "🔔",
    }

    label = type_labels.get(request_type, request_type)
    emoji = emoji_map.get(request_type, "📄")

    # Mention: wen informieren?
    mentions = []
    if notify_person1:
        mentions.append(f"<@{SLACK_PERSON1_ID}>")
    if notify_person2:
        mentions.append(f"<@{SLACK_PERSON2_ID}>")
    mention_str = " ".join(mentions) if mentions else ""

    # Warning-Block (nur bei Sonderfall)
    warning_block = []
    if warning_text:
        warning_block = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚠️ *Achtung:* {warning_text}",
                },
            },
            {"type": "divider"},
        ]

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} Freigabe erforderlich: {label}"},
        },
        *warning_block,
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Kunde:*\n{contact_name}"},
                {"type": "mrkdwn", "text": f"*E-Mail:*\n{contact_email}"},
                {"type": "mrkdwn", "text": f"*Deal:*\n{deal_title}"},
                {"type": "mrkdwn", "text": f"*Betrag:*\n€ {invoice_amount:,.2f}"},
                {"type": "mrkdwn", "text": f"*Dok.-Nr.:*\n{invoice_number}"},
                {"type": "mrkdwn", "text": f"*Typ:*\n{label}"},
            ],
        },
        _build_action_buttons(request_id, sevdesk_link),
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Freigabe durch: {mention_str} (OR-Logik: eine Person genügt)  •  "
                        "Alternativ: Schreibe *freigabe* in diesen Thread"
                    ),
                }
            ],
        },
    ]

    resp = _slack_post("chat.postMessage", {
        "channel": _channel_for(request_type),
        "text":    f"{mention_str} Freigabe erforderlich: {label} für {contact_name}",
        "blocks":  blocks,
    })

    ts      = resp["ts"]
    channel = resp["channel"]

    # Referenz in DB speichern
    set_approval_slack_ref(request_id, channel, ts)

    return {"channel": channel, "ts": ts}


# ─── Slack-Message nach Entscheidung aktualisieren ────────────────────────────

def update_approval_message_approved(request_id: int, approved_by_name: str):
    """Aktualisiert die Slack-Nachricht nach Freigabe (Buttons entfernen, Status anzeigen)."""
    req = get_approval_request(request_id)
    if not req or not req.get("slack_ts"):
        return

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅ *Freigegeben und versendet* von {approved_by_name}",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Freigabe: {req.get('approved_at', '')} · Dok.-Nr.: {req.get('invoice_number', '')}"}
            ],
        },
    ]

    _slack_update(req["slack_channel"], req["slack_ts"], blocks,
                  text=f"✅ Freigegeben von {approved_by_name}")


def update_approval_message_awaiting_changes(request_id: int, requested_by_name: str):
    """Aktualisiert die Nachricht nach Klick auf 'Ändern' – Buttons bleiben sichtbar."""
    req = get_approval_request(request_id)
    if not req or not req.get("slack_ts"):
        return

    revision_hint = ""
    revision_count = req.get("revision_count", 0)
    if revision_count and revision_count > 0:
        revision_hint = f" _(Revision {revision_count})_"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✏️ *{requested_by_name}* möchte Änderungen{revision_hint}\n"
                    "Der Agent wartet auf dein Feedback. "
                    "Schreibe deine Änderungswünsche als Antwort in *diesen Thread* ↓"
                ),
            },
        },
    ]

    _slack_update(req["slack_channel"], req["slack_ts"], blocks,
                  text=f"✏️ {requested_by_name} möchte Änderungen – Feedback im Thread eingeben")


def post_change_prompt_in_thread(request_id: int, requested_by_name: str):
    """
    Postet eine Aufforderung im Thread des Angebots.
    Gibt dem User klare Orientierung was er schreiben soll.
    """
    req = get_approval_request(request_id)
    if not req or not req.get("slack_ts"):
        return

    revision_count = req.get("revision_count", 0) or 0
    revision_label = f"Revision {revision_count + 1}" if revision_count > 0 else "Überarbeitung"

    _slack_post("chat.postMessage", {
        "channel":   req["slack_channel"],
        "thread_ts": req["slack_ts"],
        "text": (
            "Änderungswünsche hier als Antwort eingeben – "
            "der Agent überarbeitet das Angebot und postet danach eine neue Version zum Freigeben.\n\n"
            "*Beispiele:*\n"
            '- "Preis fuer Position 1 auf 1.800 EUR erhoehen"\n'
            '- "Zahlungsziel auf 14 Tage aendern und eine Support-Position hinzufuegen"\n'
            '- "Laufzeit auf 6 Monate und 10% Gesamtrabatt erganzen"'
        ),
    })


def send_revised_approval_request(
    request_id: int,
    request_type: str,
    deal_title: str,
    invoice_number: str,
    invoice_amount: float,
    sevdesk_link: str,
    contact_name: str,
    contact_email: str,
    revision_count: int,
    feedback_summary: str,
    notify_person1: bool = True,
    notify_person2: bool = True,
) -> dict:
    """
    Postet eine neue Freigabeanfrage nach Agent-Überarbeitung.
    Erscheint als neue Nachricht im Channel (nicht als Thread-Antwort)
    mit frischen [✅ Freigeben] [✏️ Ändern] [🔗 In Sevdesk öffnen] Buttons.

    Returns:
        {"channel": ..., "ts": ...} – Referenz für spätere Updates
    """
    type_labels = {
        "ar_invoice": "Anzahlungsrechnung",
        "sr_invoice": "Schlussrechnung",
        "sr_storno":  "⚠️ Schlussrechnung + AR-Storno",
        "dunning":    "Mahnung",
    }
    label = type_labels.get(request_type, request_type)

    mentions = []
    if notify_person1:
        mentions.append(f"<@{SLACK_PERSON1_ID}>")
    if notify_person2:
        mentions.append(f"<@{SLACK_PERSON2_ID}>")
    mention_str = " ".join(mentions) if mentions else ""

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔄 Revision {revision_count} bereit: {label} – {deal_title}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Angefordertes Feedback wurde umgesetzt:*\n> {feedback_summary}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Kunde:*\n{contact_name}"},
                {"type": "mrkdwn", "text": f"*E-Mail:*\n{contact_email}"},
                {"type": "mrkdwn", "text": f"*Betrag:*\n€ {invoice_amount:,.2f}"},
                {"type": "mrkdwn", "text": f"*Dok.-Nr.:*\n{invoice_number}"},
            ],
        },
        _build_action_buttons(request_id, sevdesk_link),
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Freigabe durch: {mention_str} (OR-Logik: eine Person genügt)  •  "
                        "Alternativ: Schreibe *freigabe* in diesen Thread"
                    ),
                }
            ],
        },
    ]

    resp = _slack_post("chat.postMessage", {
        "channel": _channel_for(request_type),
        "text":    f"{mention_str} Revision {revision_count} bereit zur Freigabe: {label} für {contact_name}",
        "blocks":  blocks,
    })

    ts      = resp["ts"]
    channel = resp["channel"]
    set_approval_slack_ref(request_id, channel, ts)

    return {"channel": channel, "ts": ts}


def post_status_update(message: str, thread_ts: str = None, request_type: str = "offer"):
    """Postet ein Status-Update in den passenden Slack-Channel (optional als Thread-Antwort)."""
    payload = {
        "channel": _channel_for(request_type),
        "text":    message,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    _slack_post("chat.postMessage", payload)


# ─── Interim-Workflow: Deal-Benachrichtigung (ohne Freigabe-Buttons) ──────────

def post_deal_notification(
    deal_id: int,
    deal_title: str,
    contact_name: str,
    contact_email: str,
    pipedrive_link: str,
    kategorie: str,
    products_added: list,
    hinweis: str = "",
) -> dict:
    """
    Postet eine Benachrichtigung über einen neu bearbeiteten Deal ohne Freigabe-Buttons.
    Verwendet im Interim-Workflow (solange Sevdesk POST /Order nicht verfügbar ist).

    Args:
        deal_id:        Pipedrive Deal-ID
        deal_title:     Titel des Deals
        contact_name:   Name der Kontaktperson
        contact_email:  E-Mail der Kontaktperson
        pipedrive_link: Link zum Deal in Pipedrive
        kategorie:      Erkannte Kategorie (z.B. Buchtelmobil, Backkurs)
        products_added: Liste der hinzugefügten Produkte [{"name": ..., "price": ..., "quantity": ...}]
        hinweis:        Optionaler Hinweis / Interpretation für das Team

    Returns:
        {"channel": ..., "ts": ...}
    """
    if products_added:
        products_text = "\n".join(
            f"• {p.get('name', '?')} × {p.get('quantity', 1)}  (€ {float(p.get('price', 0)):,.2f})"
            for p in products_added
        )
    else:
        products_text = "_Keine Produkte hinzugefügt – bitte manuell prüfen_"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📥 Neuer Deal bearbeitet: {deal_title}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Kontakt:*\n{contact_name}"},
                {"type": "mrkdwn", "text": f"*E-Mail:*\n{contact_email or '–'}"},
                {"type": "mrkdwn", "text": f"*Kategorie:*\n{kategorie or '?'}"},
                {"type": "mrkdwn", "text": f"*Deal-ID:*\n#{deal_id}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Produkte hinzugefügt:*\n{products_text}",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "🔗 In Pipedrive öffnen"},
                    "url":       pipedrive_link,
                    "action_id": "open_pipedrive",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "ℹ️ Sevdesk-Angebot folgt sobald der Helpdesk das POST /Order Problem behoben hat.",
                }
            ],
        },
    ]

    # Optionaler Hinweis-Block (bei generischen / unklaren Anfragen)
    if hinweis:
        blocks.insert(3, {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"💡 *Hinweis:* {hinweis}",
            },
        })

    resp = _slack_post("chat.postMessage", {
        "channel": SLACK_CHANNEL_OFFERS,
        "text":    f"📥 Neuer Deal bearbeitet: {deal_title} ({contact_name})",
        "blocks":  blocks,
    })

    return {"channel": resp["channel"], "ts": resp["ts"]}


# ─── Event Handler: Button-Klick ──────────────────────────────────────────────

def handle_interactive_action(payload: dict) -> dict:
    """
    Verarbeitet Button-Klicks aus Slack Interactive Components.

    Args:
        payload: Dekodierter Slack-Interaktions-Payload

    Returns:
        {"action": "approved"|"rejected", "request_id": int, "user_id": str}
    """
    actions   = payload.get("actions", [])
    user      = payload.get("user", {})
    user_id   = user.get("id", "")
    user_name = user.get("name", user_id)

    for action in actions:
        action_id  = action.get("action_id")
        request_id = int(action.get("value", 0))

        if action_id == "approve":
            req = get_approval_request(request_id)
            if not req or req["status"] != "pending":
                return {"action": "already_handled", "request_id": request_id}

            # OR-Logik: prüfen ob Person freigeben darf
            if not _is_authorized(user_id, req):
                post_status_update(
                    f"<@{user_id}> Du bist für diese Freigabe nicht autorisiert.",
                    thread_ts=req.get("slack_ts"),
                )
                return {"action": "unauthorized", "request_id": request_id}

            approve_request(request_id, user_id)
            update_approval_message_approved(request_id, user_name)

            return {
                "action":     "approved",
                "request_id": request_id,
                "user_id":    user_id,
                "request":    get_approval_request(request_id),
            }

        elif action_id == "request_changes":
            req = get_approval_request(request_id)
            if not req or req["status"] not in ("pending", "awaiting_changes"):
                return {"action": "already_handled", "request_id": request_id}

            set_awaiting_changes(request_id, user_id)
            update_approval_message_awaiting_changes(request_id, user_name)
            post_change_prompt_in_thread(request_id, user_name)

            return {
                "action":     "awaiting_changes",
                "request_id": request_id,
                "user_id":    user_id,
                "request":    get_approval_request(request_id),
            }

    return {"action": "unknown"}


# ─── Event Handler: Text-Nachricht ────────────────────────────────────────────

def handle_slack_message(event: dict) -> dict | None:
    """
    Verarbeitet eingehende Slack-Textnachrichten.
    Erkennt Freigabe-Keywords in Thread-Antworten auf Freigabeanfragen.

    Args:
        event: Slack message event

    Returns:
        {"action": "approved"|"rejected"|"feedback", "request_id": int} oder None
    """
    text        = event.get("text", "")
    user_id     = event.get("user", "")
    thread_ts   = event.get("thread_ts")  # nur Thread-Antworten relevant

    if not thread_ts or event.get("bot_id"):
        return None  # Kein Thread oder Bot-Nachricht

    # 1. Prüfen ob eine Anfrage auf Änderungswünsche wartet (Priorität)
    awaiting_req = get_awaiting_changes_by_ts(thread_ts)
    if awaiting_req and not APPROVAL_KEYWORDS.search(text):
        # Jede Nachricht (außer "freigabe") wird als Feedback interpretiert
        request_id = awaiting_req["id"]
        append_feedback(request_id, user_id, text)

        # Sofort im Thread bestätigen dass Agent arbeitet
        _slack_post("chat.postMessage", {
            "channel":   awaiting_req["slack_channel"],
            "thread_ts": thread_ts,
            "text":      f"⚙️ Feedback erhalten! Der Agent überarbeitet das Angebot... Das kann 30–60 Sekunden dauern.",
        })

        return {
            "action":     "change_requested",
            "request_id": request_id,
            "user_id":    user_id,
            "feedback":   text,
            "request":    awaiting_req,
        }

    # 2. Freigabe per Text in einem pending-Thread
    req = get_pending_approval_by_ts(thread_ts)
    if not req:
        return None  # Kein offener Request zu diesem Thread

    if APPROVAL_KEYWORDS.search(text):
        if not _is_authorized(user_id, req):
            return None

        request_id = req["id"]
        approve_request(request_id, user_id)
        update_approval_message_approved(request_id, f"<@{user_id}>")

        return {
            "action":     "approved",
            "request_id": request_id,
            "user_id":    user_id,
            "request":    get_approval_request(request_id),
        }

    else:
        # Freitext im pending-Thread → als Notiz loggen
        return {
            "action":     "feedback",
            "request_id": req["id"],
            "feedback":   text,
        }


# ─── Autorisierungsprüfung ────────────────────────────────────────────────────

def _is_authorized(user_id: str, req: dict) -> bool:
    """
    Prüft ob ein User für diese Freigabe berechtigt ist.
    Berücksichtigt notify_person1 / notify_person2 Routing.
    """
    if req.get("notify_person1") and user_id == SLACK_PERSON1_ID:
        return True
    if req.get("notify_person2") and user_id == SLACK_PERSON2_ID:
        return True
    return False
