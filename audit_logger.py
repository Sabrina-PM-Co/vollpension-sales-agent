#!/usr/bin/env python3
"""
Audit Logger – DSGVO-konformes Metadaten-Logging
=================================================
Speichert KEINE Prompts, Kundennamen, E-Mails oder sonstige PII.
Nur technische Metadaten für Debugging, Kostenverfolgung und Audit.

Was wird geloggt:
  ✅ Timestamp, Workflow-Typ, Model, Token-Anzahl, Tool-Namen, Status, Fehler
  ✅ Deal-ID als anonyme Referenz (kein personenbezogenes Datum per se)
  ❌ Prompt-Inhalte, Tool-Inputs/-Outputs, Kundendaten, E-Mails

Tabellen (in derselben workflow_state.db wie state_manager.py):
  - workflow_runs    → Ein Eintrag pro Workflow-Ausführung
  - agent_turns      → Ein Eintrag pro Claude-API-Call innerhalb eines Runs
  - approval_events  → Audit-Trail für alle Freigabe-Entscheidungen

Aufbewahrung:
  - Empfohlen: 90 Tage (cleanup_old_logs() aufrufen)
  - Datenschutzerklärung anpassen falls länger gespeichert wird

Abfragen:
  - get_run_summary(run_id) → Token-Kosten, Tool-Calls, Status
  - get_deal_history(deal_id) → Alle Runs für einen Deal
  - get_cost_report(days=30) → Kostenübersicht für Abrechnungszeitraum
  - get_approval_audit(days=30) → Wer hat wann was freigegeben
"""

import sqlite3
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# Selbe DB wie state_manager.py
DB_PATH = Path(__file__).parent / "workflow_state.db"

# Claude Opus 4.5 Preise (USD per 1M Tokens) – bei Modellwechsel anpassen
COST_PER_M_INPUT  = 5.00
COST_PER_M_OUTPUT = 25.00


# ─── Tabellen-Setup ───────────────────────────────────────────────────────────

def init_audit_tables():
    """Erstellt Audit-Tabellen falls nicht vorhanden."""
    with _conn() as con:
        con.executescript("""
        -- ── Workflow-Runs ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id          TEXT PRIMARY KEY,   -- UUID
            deal_id         INTEGER,
            workflow_type   TEXT NOT NULL,
            -- offer | ar_invoice | sr_invoice | sr_storno | dunning | dunning_check

            model           TEXT,
            status          TEXT DEFAULT 'running',
            -- running | completed | failed | max_turns

            total_turns     INTEGER DEFAULT 0,
            total_input_tk  INTEGER DEFAULT 0,
            total_output_tk INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0.0,

            error_message   TEXT,
            started_at      TEXT DEFAULT (datetime('now')),
            finished_at     TEXT
        );

        -- ── Agent-Turns (ein Eintrag pro Claude-API-Call) ─────────────────
        CREATE TABLE IF NOT EXISTS agent_turns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT NOT NULL,
            turn_number     INTEGER NOT NULL,

            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            cost_usd        REAL DEFAULT 0.0,

            -- Tool-Namen als JSON-Array, keine Inputs/Outputs (PII!)
            tool_names      TEXT DEFAULT '[]',
            -- z.B. ["pipedrive_get_deal", "sevdesk_create_invoice"]

            stop_reason     TEXT,
            -- end_turn | tool_use | max_tokens | error

            timestamp       TEXT DEFAULT (datetime('now'))
        );

        -- ── Freigabe-Events (Audit-Trail für Compliance) ──────────────────
        CREATE TABLE IF NOT EXISTS approval_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_request_id INTEGER,
            deal_id         INTEGER,
            workflow_type   TEXT,
            -- ar_invoice | sr_invoice | sr_storno | dunning

            event_type      TEXT NOT NULL,
            -- requested | approved | rejected | sent | failed

            actor_slack_id  TEXT,   -- Wer hat gehandelt (kein Name, nur ID)
            note            TEXT,   -- Freitext (max 500 Zeichen, kein PII!)

            timestamp       TEXT DEFAULT (datetime('now'))
        );

        -- ── Indizes für Performance ───────────────────────────────────────
        CREATE INDEX IF NOT EXISTS idx_runs_deal_id   ON workflow_runs(deal_id);
        CREATE INDEX IF NOT EXISTS idx_runs_started   ON workflow_runs(started_at);
        CREATE INDEX IF NOT EXISTS idx_turns_run_id   ON agent_turns(run_id);
        CREATE INDEX IF NOT EXISTS idx_approvals_deal ON approval_events(deal_id);
        """)


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _now():
    return datetime.utcnow().isoformat()


def _calc_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * COST_PER_M_INPUT
        + (output_tokens / 1_000_000) * COST_PER_M_OUTPUT,
        6
    )


# ─── Run-Lifecycle ────────────────────────────────────────────────────────────

def start_run(workflow_type: str, deal_id: int = None, model: str = "claude-opus-4-5-20251101") -> str:
    """
    Startet einen neuen Workflow-Run und gibt die run_id zurück.
    Sofort am Anfang jedes Workflows aufrufen.

    Returns:
        run_id (UUID-String) – weitergeben an log_turn() und finish_run()
    """
    run_id = str(uuid.uuid4())
    with _conn() as con:
        con.execute(
            """INSERT INTO workflow_runs
               (run_id, deal_id, workflow_type, model, status)
               VALUES (?, ?, ?, ?, 'running')""",
            (run_id, deal_id, workflow_type, model)
        )
    return run_id


def log_turn(
    run_id: str,
    turn_number: int,
    input_tokens: int,
    output_tokens: int,
    tool_names: list[str],
    stop_reason: str,
):
    """
    Loggt einen einzelnen Claude-API-Call (Turn).
    Nur Token-Anzahl und Tool-Namen – keine Inhalte.

    Args:
        run_id:        UUID aus start_run()
        turn_number:   Schleifenzähler (1, 2, 3, ...)
        input_tokens:  response.usage.input_tokens
        output_tokens: response.usage.output_tokens
        tool_names:    [tb.name for tb in tool_use_blocks]
        stop_reason:   response.stop_reason
    """
    cost = _calc_cost(input_tokens, output_tokens)

    with _conn() as con:
        con.execute(
            """INSERT INTO agent_turns
               (run_id, turn_number, input_tokens, output_tokens,
                cost_usd, tool_names, stop_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, turn_number, input_tokens, output_tokens,
             cost, json.dumps(tool_names), stop_reason)
        )
        # Run-Summen aktualisieren
        con.execute(
            """UPDATE workflow_runs SET
               total_turns = total_turns + 1,
               total_input_tk  = total_input_tk  + ?,
               total_output_tk = total_output_tk + ?,
               estimated_cost_usd = estimated_cost_usd + ?
               WHERE run_id = ?""",
            (input_tokens, output_tokens, cost, run_id)
        )


def finish_run(run_id: str, status: str = "completed", error: str = None):
    """
    Schließt einen Run ab. Status: 'completed', 'failed', 'max_turns'.
    """
    with _conn() as con:
        con.execute(
            """UPDATE workflow_runs
               SET status=?, error_message=?, finished_at=?
               WHERE run_id=?""",
            (status, error, _now(), run_id)
        )


# ─── Approval-Audit ───────────────────────────────────────────────────────────

def log_approval_event(
    event_type: str,
    approval_request_id: int = None,
    deal_id: int = None,
    workflow_type: str = None,
    actor_slack_id: str = None,
    note: str = None,
):
    """
    Loggt ein Freigabe-Event für den Compliance-Audit-Trail.

    event_type: 'requested' | 'approved' | 'rejected' | 'sent' | 'failed'
    """
    # Note auf 500 Zeichen kürzen + keine E-Mails/Namen speichern
    if note and len(note) > 500:
        note = note[:497] + "..."

    with _conn() as con:
        con.execute(
            """INSERT INTO approval_events
               (approval_request_id, deal_id, workflow_type,
                event_type, actor_slack_id, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (approval_request_id, deal_id, workflow_type,
             event_type, actor_slack_id, note)
        )


# ─── Abfragen ─────────────────────────────────────────────────────────────────

def get_run_summary(run_id: str) -> dict | None:
    """Gibt die Zusammenfassung eines einzelnen Runs zurück."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    # Turns dazuladen
    with _conn() as con:
        turns = con.execute(
            "SELECT * FROM agent_turns WHERE run_id = ? ORDER BY turn_number",
            (run_id,)
        ).fetchall()
    d["turns"] = [dict(t) for t in turns]
    return d


def get_deal_history(deal_id: int) -> list[dict]:
    """Alle Runs für einen bestimmten Deal."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM workflow_runs
               WHERE deal_id = ? ORDER BY started_at DESC""",
            (deal_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_cost_report(days: int = 30) -> dict:
    """
    Kostenübersicht für die letzten N Tage.
    Nützlich für die monatliche Kundenabrechnung.

    Returns:
        {
          "period_days": int,
          "total_runs": int,
          "total_input_tokens": int,
          "total_output_tokens": int,
          "total_cost_usd": float,
          "by_workflow_type": {...},
          "by_day": [...]
        }
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()

    with _conn() as con:
        # Gesamt
        total = con.execute(
            """SELECT
               COUNT(*)           AS runs,
               SUM(total_input_tk)  AS input_tk,
               SUM(total_output_tk) AS output_tk,
               SUM(estimated_cost_usd) AS cost_usd
               FROM workflow_runs
               WHERE started_at >= ? AND status != 'running'""",
            (since,)
        ).fetchone()

        # Nach Workflow-Typ
        by_type = con.execute(
            """SELECT workflow_type,
               COUNT(*) AS runs,
               SUM(total_input_tk)  AS input_tk,
               SUM(total_output_tk) AS output_tk,
               SUM(estimated_cost_usd) AS cost_usd
               FROM workflow_runs
               WHERE started_at >= ? AND status != 'running'
               GROUP BY workflow_type""",
            (since,)
        ).fetchall()

        # Nach Tag (letzte 30 Tage)
        by_day = con.execute(
            """SELECT
               DATE(started_at) AS day,
               COUNT(*) AS runs,
               SUM(estimated_cost_usd) AS cost_usd
               FROM workflow_runs
               WHERE started_at >= ? AND status != 'running'
               GROUP BY DATE(started_at)
               ORDER BY day DESC""",
            (since,)
        ).fetchall()

    return {
        "period_days":         days,
        "total_runs":          total["runs"] or 0,
        "total_input_tokens":  total["input_tk"] or 0,
        "total_output_tokens": total["output_tk"] or 0,
        "total_cost_usd":      round(total["cost_usd"] or 0, 4),
        "by_workflow_type":    {r["workflow_type"]: {
                                    "runs": r["runs"],
                                    "cost_usd": round(r["cost_usd"] or 0, 4)
                                } for r in by_type},
        "by_day":              [{"day": r["day"], "runs": r["runs"],
                                  "cost_usd": round(r["cost_usd"] or 0, 4)}
                                 for r in by_day],
    }


def get_approval_audit(days: int = 30) -> list[dict]:
    """
    Audit-Trail aller Freigabe-Entscheidungen der letzten N Tage.
    Für DSGVO-Auskunft und interne Compliance.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM approval_events
               WHERE timestamp >= ?
               ORDER BY timestamp DESC""",
            (since,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_failed_runs(days: int = 7) -> list[dict]:
    """Fehlgeschlagene Runs der letzten N Tage – für Monitoring und Alerts."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with _conn() as con:
        rows = con.execute(
            """SELECT run_id, deal_id, workflow_type, error_message, started_at
               FROM workflow_runs
               WHERE status = 'failed' AND started_at >= ?
               ORDER BY started_at DESC""",
            (since,)
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Wartung / DSGVO-Aufbewahrung ────────────────────────────────────────────

def cleanup_old_logs(retention_days: int = 90):
    """
    Löscht Logs die älter als retention_days sind.
    Empfehlung: täglich via Cron aufrufen (z.B. zusammen mit dunning_check).

    Retention:
      - 90 Tage für operatives Debugging
      - approval_events: 3 Jahre (Rechnungsbelege-Aufbewahrungspflicht AT)
    """
    cutoff_runs = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
    cutoff_approvals = (datetime.utcnow() - timedelta(days=1095)).isoformat()  # 3 Jahre

    with _conn() as con:
        # Alte Runs und Turns löschen
        deleted_turns = con.execute(
            """DELETE FROM agent_turns WHERE run_id IN (
               SELECT run_id FROM workflow_runs WHERE started_at < ?)""",
            (cutoff_runs,)
        ).rowcount

        deleted_runs = con.execute(
            "DELETE FROM workflow_runs WHERE started_at < ?",
            (cutoff_runs,)
        ).rowcount

        # Approval-Events länger aufbewahren (Rechnungslegung)
        deleted_approvals = con.execute(
            "DELETE FROM approval_events WHERE timestamp < ?",
            (cutoff_approvals,)
        ).rowcount

    print(
        f"🧹 Cleanup: {deleted_runs} Runs, {deleted_turns} Turns, "
        f"{deleted_approvals} Approval-Events gelöscht."
    )
    return {
        "deleted_runs":      deleted_runs,
        "deleted_turns":     deleted_turns,
        "deleted_approvals": deleted_approvals,
    }


# ─── CLI: Schnellabfragen ─────────────────────────────────────────────────────

def print_cost_report(days: int = 30):
    """Gibt den Kostenbericht in der Konsole aus."""
    r = get_cost_report(days)
    print(f"\n{'='*50}")
    print(f"💰 Kostenübersicht – letzte {days} Tage")
    print(f"{'='*50}")
    print(f"  Runs gesamt:     {r['total_runs']}")
    print(f"  Input-Tokens:    {r['total_input_tokens']:,}")
    print(f"  Output-Tokens:   {r['total_output_tokens']:,}")
    print(f"  Kosten (USD):    ${r['total_cost_usd']:.4f}")
    print(f"  Kosten (EUR ~):  €{r['total_cost_usd'] * 0.92:.4f}")
    print(f"\n  Nach Workflow-Typ:")
    for wf_type, data in r["by_workflow_type"].items():
        print(f"    {wf_type:<20} {data['runs']:>3} Runs  ${data['cost_usd']:.4f}")
    print(f"{'='*50}\n")


# ─── Startup ──────────────────────────────────────────────────────────────────

init_audit_tables()


# ─── CLI-Direktaufruf ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "report":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        print_cost_report(days)

    elif cmd == "deal":
        deal_id = int(sys.argv[2])
        history = get_deal_history(deal_id)
        print(f"\nDeal {deal_id} – {len(history)} Run(s):")
        for r in history:
            print(f"  [{r['started_at'][:19]}] {r['workflow_type']:<20} "
                  f"{r['status']:<12} €{r['estimated_cost_usd'] * 0.92:.5f}  "
                  f"{r['total_turns']} Turns")

    elif cmd == "failed":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        failed = get_failed_runs(days)
        print(f"\n❌ Fehlgeschlagene Runs (letzte {days} Tage): {len(failed)}")
        for r in failed:
            print(f"  Deal {r['deal_id']} | {r['workflow_type']} | "
                  f"{r['started_at'][:19]} | {r['error_message'][:80]}")

    elif cmd == "audit":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        events = get_approval_audit(days)
        print(f"\n📋 Freigabe-Audit (letzte {days} Tage): {len(events)} Events")
        for e in events:
            print(f"  [{e['timestamp'][:19]}] {e['event_type']:<12} "
                  f"Deal {e['deal_id']} | {e['workflow_type']} | "
                  f"Actor: {e['actor_slack_id'] or '–'}")

    elif cmd == "cleanup":
        cleanup_old_logs()

    else:
        print("Verwendung: python audit_logger.py [report|deal <id>|failed|audit|cleanup] [days]")
