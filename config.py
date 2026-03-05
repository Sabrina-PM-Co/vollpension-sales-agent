#!/usr/bin/env python3
"""
Konfiguration – lädt alle Umgebungsvariablen aus .env oder dem System.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()  # Lädt .env-Datei aus dem Projektordner

# ─── Pflichtfelder ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_DOMAIN    = os.environ.get("PIPEDRIVE_DOMAIN", "")       # z.B. "meinefirma"
SEVDESK_API_TOKEN   = os.environ.get("SEVDESK_API_TOKEN", "")

# ─── Webhook-Sicherheit ────────────────────────────────────────────────────────

WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "")       # Pipedrive Basic-Auth Passwort
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "") # Slack App Signing Secret
CRON_SECRET          = os.environ.get("CRON_SECRET", "")          # Schutz für /cron/dunning-check
ADMIN_SECRET         = os.environ.get("ADMIN_SECRET", "")         # Schutz für /admin/* Endpoints

# ─── Slack ─────────────────────────────────────────────────────────────────────

SLACK_BOT_TOKEN        = os.environ.get("SLACK_BOT_TOKEN", "")         # xoxb-...
SLACK_CHANNEL_OFFERS   = os.environ.get("SLACK_CHANNEL_OFFERS", "")   # Channel für Angebots-Freigaben
SLACK_CHANNEL_INVOICES = os.environ.get("SLACK_CHANNEL_INVOICES", "") # Channel für Rechnungen + Mahnungen
SLACK_PERSON1_ID       = os.environ.get("SLACK_PERSON1_ID", "")       # Slack User-ID Person 1 (Vertrieb)
SLACK_PERSON2_ID       = os.environ.get("SLACK_PERSON2_ID", "")       # Slack User-ID Person 2 (Buchhaltung)

# ─── E-Mail-Benachrichtigung ───────────────────────────────────────────────────

NOTIFY_EMAIL_TO      = os.environ.get("NOTIFY_EMAIL_TO", "")      # Empfänger der Benachrichtigung
NOTIFY_EMAIL_FROM    = os.environ.get("NOTIFY_EMAIL_FROM", "")    # Absender
SMTP_HOST            = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT            = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER            = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD        = os.environ.get("SMTP_PASSWORD", "")

# ─── Pipedrive Pipeline / Stage ───────────────────────────────────────────────
# Webhook triggert wenn ein Deal in diese Stage bewegt wird.
# IDs findest du in Pipedrive → Einstellungen → Pipelines
# oder via API: GET /stages?pipeline_id=X

# Pipeline "Product Sales" und ihre Stages
# Eintragen nach: Pipedrive → Einstellungen → Pipelines (IDs in der URL sichtbar)
# oder API: GET /stages?pipeline_id=X  → id = Stage-ID
PIPEDRIVE_PIPELINE_ID           = int(os.environ.get("PIPEDRIVE_PIPELINE_ID",           "0"))
PIPEDRIVE_STAGE_ANFRAGEN        = int(os.environ.get("PIPEDRIVE_STAGE_ANFRAGEN",        "0"))
PIPEDRIVE_STAGE_IN_BEARBEITUNG  = int(os.environ.get("PIPEDRIVE_STAGE_IN_BEARBEITUNG",  "0"))
PIPEDRIVE_STAGE_ANGEBOT_GELEGT  = int(os.environ.get("PIPEDRIVE_STAGE_ANGEBOT_GELEGT",  "0"))
# PIPEDRIVE_STAGE_GEWONNEN      = int(os.environ.get("PIPEDRIVE_STAGE_GEWONNEN",        "0"))

# ─── Validierung beim Start ────────────────────────────────────────────────────

_missing = []
for var, val in [
    ("ANTHROPIC_API_KEY",   ANTHROPIC_API_KEY),
    ("PIPEDRIVE_API_TOKEN",  PIPEDRIVE_API_TOKEN),
    ("PIPEDRIVE_DOMAIN",     PIPEDRIVE_DOMAIN),
    ("SEVDESK_API_TOKEN",    SEVDESK_API_TOKEN),
    ("SLACK_BOT_TOKEN",        SLACK_BOT_TOKEN),
    ("SLACK_CHANNEL_OFFERS",   SLACK_CHANNEL_OFFERS),
    ("SLACK_CHANNEL_INVOICES", SLACK_CHANNEL_INVOICES),
]:
    if not val:
        _missing.append(var)

if _missing:
    print(
        f"FEHLER: Folgende Umgebungsvariablen fehlen in der .env:\n"
        + "\n".join(f"  - {v}" for v in _missing),
        file=sys.stderr,
    )
    sys.exit(1)
