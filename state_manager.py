#!/usr/bin/env python3
"""
State Manager – SQLite-basiertes Zustandsmanagement
=====================================================
Verwaltet drei Tabellen:
  1. invoice_state     – Rechnungsworkflow pro Deal
  2. approval_requests – Offene Slack-Freigabeanfragen
  3. dunning_state     – Mahnwesen pro Rechnung

Designprinzip: Jede State-Änderung wird mit Timestamp geloggt.
Kein Auto-Versand ohne vorherige menschliche Freigabe über Slack.
"""

import os
import sqlite3
import json
from datetime import datetime, date
from pathlib import Path

# DB_PATH kann via Umgebungsvariable überschrieben werden (z.B. Docker-Volume)
DB_PATH = Path(os.environ.get("DB_PATH", "") or (Path(__file__).parent / "workflow_state.db"))


# ─── DB-Initialisierung ────────────────────────────────────────────────────────

def init_db():
    """Erstellt alle Tabellen falls nicht vorhanden."""
    with _conn() as con:
        con.executescript("""
        -- ── Rechnungsworkflow pro Deal ──────────────────────────────────────
        CREATE TABLE IF NOT EXISTS invoice_state (
            deal_id             INTEGER PRIMARY KEY,
            contact_id_sdsk     TEXT,
            contact_email       TEXT,
            deal_title          TEXT,
            deal_value          REAL,
            event_datum         TEXT,      -- ISO-Datum YYYY-MM-DD

            -- Anzahlungsrechnung (AR)
            ar_sevdesk_id       TEXT,
            ar_invoice_number   TEXT,
            ar_amount           REAL,
            ar_status           TEXT DEFAULT 'none',
            -- none | draft | sent | paid | cancelled

            -- Schlussrechnung (SR)
            sr_sevdesk_id       TEXT,
            sr_invoice_number   TEXT,
            sr_amount           REAL,
            sr_status           TEXT DEFAULT 'pending',
            -- pending | draft | approved | sent

            -- Workflow-Phase
            phase               TEXT DEFAULT 'awaiting_event',
            -- awaiting_event | final_invoice | dunning | completed

            -- Agent-Notizen (JSON-Liste von Strings)
            agent_notes         TEXT DEFAULT '[]',

            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        );

        -- ── Slack-Freigabeanfragen ───────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS approval_requests (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            request_type        TEXT NOT NULL,
            -- ar_invoice | sr_invoice | sr_storno | dunning

            deal_id             INTEGER,
            invoice_id          TEXT,      -- Sevdesk-ID
            invoice_number      TEXT,

            -- Routing: wer soll informiert werden?
            notify_person1      INTEGER DEFAULT 1,   -- Vertrieb/Geschäftsführung
            notify_person2      INTEGER DEFAULT 1,   -- Buchhaltung
            -- Bei Sonderfall (AR nicht gefunden): nur person2 = 1, person1 = 0

            -- OR-Logik: genügt eine Freigabe
            approved_by         TEXT,      -- Slack-User-ID wer freigegeben hat
            approved_at         TEXT,

            -- Änderungswünsche
            changes_requested_by  TEXT,    -- Slack-User-ID wer Änderung angefragt hat
            changes_requested_at  TEXT,
            feedback_history      TEXT DEFAULT '[]',  -- JSON-Liste aller Feedback-Runden
            revision_count        INTEGER DEFAULT 0,  -- Wie oft wurde überarbeitet?

            -- Slack-Referenz für Message-Updates
            slack_channel       TEXT,
            slack_ts            TEXT,      -- message timestamp für chat.update

            status              TEXT DEFAULT 'pending',
            -- pending | approved | awaiting_changes | superseded | expired

            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        );

        -- ── Mahnwesen pro Rechnung ───────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS dunning_state (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_sevdesk_id  TEXT NOT NULL,
            invoice_number      TEXT,
            deal_id             INTEGER,
            contact_email       TEXT,
            invoice_date        TEXT,      -- ISO-Datum
            due_date            TEXT,      -- invoice_date + 14 Tage
            invoice_amount      REAL,

            -- Mahnstatus
            dunning_level       INTEGER DEFAULT 0,   -- 0=keine, 1=erste, 2=zweite...
            dunning_id_sdsk     TEXT,      -- Sevdesk-Mahnungs-ID
            dunning_sent_at     TEXT,

            -- Approval
            approval_request_id INTEGER,
            status              TEXT DEFAULT 'monitoring',
            -- monitoring | approval_pending | sent | paid | cancelled

            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        );
        """)
    print("✅ DB initialisiert:", DB_PATH)


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _now():
    return datetime.utcnow().isoformat()


def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


# ─── invoice_state CRUD ────────────────────────────────────────────────────────

def upsert_invoice_state(deal_id: int, **kwargs) -> dict:
    """
    Legt einen neuen Eintrag an oder aktualisiert ihn.
    Akzeptiert alle Spalten aus invoice_state als kwargs.
    """
    kwargs["updated_at"] = _now()
    existing = get_invoice_state(deal_id)

    with _conn() as con:
        if existing:
            cols = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [deal_id]
            con.execute(f"UPDATE invoice_state SET {cols} WHERE deal_id = ?", vals)
        else:
            kwargs["deal_id"] = deal_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            con.execute(
                f"INSERT INTO invoice_state ({cols}) VALUES ({placeholders})",
                list(kwargs.values())
            )
    return get_invoice_state(deal_id)


def get_or_create_invoice_state(deal_id: int) -> dict:
    """Gibt den bestehenden Invoice-State zurück oder legt einen neuen an."""
    return upsert_invoice_state(deal_id)


def get_invoice_state(deal_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM invoice_state WHERE deal_id = ?", (deal_id,)
        ).fetchone()
    return _row_to_dict(row)


def add_agent_note(deal_id: int, note: str):
    """Fügt eine Agent-Notiz zur JSON-Liste hinzu."""
    state = get_invoice_state(deal_id)
    if not state:
        return
    notes = json.loads(state.get("agent_notes") or "[]")
    notes.append(f"[{_now()}] {note}")
    upsert_invoice_state(deal_id, agent_notes=json.dumps(notes, ensure_ascii=False))


# ─── approval_requests CRUD ───────────────────────────────────────────────────

def create_approval_request(
    request_type: str,
    deal_id: int,
    invoice_id: str,
    invoice_number: str,
    notify_person1: bool = True,
    notify_person2: bool = True,
) -> dict:
    """Erstellt eine neue Freigabeanfrage (Status: pending)."""
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO approval_requests
               (request_type, deal_id, invoice_id, invoice_number,
                notify_person1, notify_person2)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (request_type, deal_id, invoice_id, invoice_number,
             int(notify_person1), int(notify_person2))
        )
        row_id = cur.lastrowid
    return get_approval_request(row_id)


def get_approval_request(request_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_pending_approval_by_ts(slack_ts: str) -> dict | None:
    """Findet eine offene Anfrage anhand des Slack Message Timestamps."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM approval_requests WHERE slack_ts = ? AND status = 'pending'",
            (slack_ts,)
        ).fetchone()
    return _row_to_dict(row)


def set_approval_slack_ref(request_id: int, channel: str, ts: str):
    """Speichert Slack-Channel + Timestamp nach dem Versand der Nachricht."""
    with _conn() as con:
        con.execute(
            "UPDATE approval_requests SET slack_channel=?, slack_ts=?, updated_at=? WHERE id=?",
            (channel, ts, _now(), request_id)
        )


def approve_request(request_id: int, approved_by_slack_id: str) -> dict:
    """OR-Logik: Eine Freigabe genügt. Logt Freigabe auch in feedback_history."""
    now = _now()
    with _conn() as con:
        con.execute(
            """UPDATE approval_requests
               SET status='approved', approved_by=?, approved_at=?, updated_at=?
               WHERE id=?""",
            (approved_by_slack_id, now, now, request_id)
        )
    # Freigabe als History-Eintrag loggen (konsistente Audit-Trail)
    req = get_approval_request(request_id)
    if req:
        history = json.loads(req.get("feedback_history") or "[]")
        history.append({
            "event": "approved",
            "user_id": approved_by_slack_id,
            "timestamp": now,
        })
        with _conn() as con:
            con.execute(
                "UPDATE approval_requests SET feedback_history=?, updated_at=? WHERE id=?",
                (json.dumps(history, ensure_ascii=False), now, request_id)
            )
    return get_approval_request(request_id)


def set_awaiting_changes(request_id: int, user_id: str) -> dict:
    """
    Markiert eine Freigabeanfrage als 'awaiting_changes'.
    Wird aufgerufen wenn jemand den 'Ändern'-Button in Slack drückt.
    """
    with _conn() as con:
        con.execute(
            """UPDATE approval_requests
               SET status='awaiting_changes', changes_requested_by=?,
                   changes_requested_at=?, updated_at=?
               WHERE id=?""",
            (user_id, _now(), _now(), request_id)
        )
    return get_approval_request(request_id)


def append_feedback(request_id: int, user_id: str, feedback_text: str):
    """Fügt einen Feedback-Eintrag (Änderungswunsch) zur JSON-History hinzu."""
    req = get_approval_request(request_id)
    if not req:
        return
    history = json.loads(req.get("feedback_history") or "[]")
    history.append({
        "event": "change_requested",
        "round": len([e for e in history if e.get("event") == "change_requested"]) + 1,
        "user_id": user_id,
        "feedback": feedback_text,
        "timestamp": _now(),
    })
    with _conn() as con:
        con.execute(
            "UPDATE approval_requests SET feedback_history=?, updated_at=? WHERE id=?",
            (json.dumps(history, ensure_ascii=False), _now(), request_id)
        )


def append_history_event(
    request_id: int,
    event_type: str,
    user_id: str = "",
    note: str = "",
):
    """
    Logt ein generisches System-Event in feedback_history.
    Wird genutzt für: 'deleted', 'superseded', und andere nicht-User-Events.

    Args:
        request_id: ID der approval_request.
        event_type: Freitext-Typ, z.B. 'deleted', 'offer_revised', 'error'.
        user_id:    Slack-User-ID (optional, leer für System-Events).
        note:       Zusätzliche Info, z.B. Angebotsnummer oder Fehlermeldung.
    """
    req = get_approval_request(request_id)
    if not req:
        return
    history = json.loads(req.get("feedback_history") or "[]")
    entry: dict = {
        "event": event_type,
        "timestamp": _now(),
    }
    if user_id:
        entry["user_id"] = user_id
    if note:
        entry["note"] = note
    history.append(entry)
    with _conn() as con:
        con.execute(
            "UPDATE approval_requests SET feedback_history=?, updated_at=? WHERE id=?",
            (json.dumps(history, ensure_ascii=False), _now(), request_id)
        )


def supersede_request(request_id: int):
    """Schließt eine alte Anfrage wenn eine neue Revision erstellt wurde."""
    with _conn() as con:
        con.execute(
            "UPDATE approval_requests SET status='superseded', updated_at=? WHERE id=?",
            (_now(), request_id)
        )


def get_awaiting_changes_by_ts(slack_ts: str) -> dict | None:
    """Findet eine Anfrage im Status 'awaiting_changes' anhand des Slack Thread-Timestamps."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM approval_requests WHERE slack_ts = ? AND status = 'awaiting_changes'",
            (slack_ts,)
        ).fetchone()
    return _row_to_dict(row)


# ─── dunning_state CRUD ───────────────────────────────────────────────────────

def create_dunning_entry(
    invoice_sevdesk_id: str,
    invoice_number: str,
    deal_id: int,
    contact_email: str,
    invoice_date: str,
    invoice_amount: float,
) -> dict:
    """
    Legt einen Mahnungs-Monitoring-Eintrag an.
    due_date = invoice_date + 14 Tage.
    """
    inv_date = date.fromisoformat(invoice_date)
    from datetime import timedelta
    due = (inv_date + timedelta(days=14)).isoformat()

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO dunning_state
               (invoice_sevdesk_id, invoice_number, deal_id, contact_email,
                invoice_date, due_date, invoice_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (invoice_sevdesk_id, invoice_number, deal_id, contact_email,
             invoice_date, due, invoice_amount)
        )
        row_id = cur.lastrowid

    with _conn() as con:
        row = con.execute(
            "SELECT * FROM dunning_state WHERE id = ?", (row_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_overdue_for_dunning() -> list[dict]:
    """
    Gibt alle Rechnungen zurück, die:
    - Status 'monitoring'
    - due_date <= heute
    → Kandidaten für Mahnungs-Check.
    """
    today = date.today().isoformat()
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM dunning_state
               WHERE status = 'monitoring' AND due_date <= ?""",
            (today,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_dunning_entry(entry_id: int, **kwargs):
    kwargs["updated_at"] = _now()
    with _conn() as con:
        cols = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [entry_id]
        con.execute(f"UPDATE dunning_state SET {cols} WHERE id = ?", vals)


# ─── Startup ──────────────────────────────────────────────────────────────────

init_db()
