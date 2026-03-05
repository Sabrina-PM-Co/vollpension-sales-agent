#!/bin/bash
# ─── Vollpension Agent – Hetzner Server Setup ─────────────────────────────────
#
# Dieses Script richtet einen frischen Ubuntu 24.04 Hetzner-Server komplett ein:
#   1. System-Updates
#   2. Docker + Docker Compose installieren
#   3. Firewall (UFW) konfigurieren
#   4. App-Verzeichnis anlegen + Code deployen
#   5. .env Datei erstellen (interaktiv)
#   6. Docker Container starten
#   7. Cron-Job für tägliches Mahnwesen einrichten
#
# VERWENDUNG (als root auf dem Hetzner-Server):
#   curl -fsSL https://raw.githubusercontent.com/DEIN-REPO/setup_hetzner.sh | bash
#
# ODER: Script hochladen und ausführen:
#   scp setup_hetzner.sh root@HETZNER-IP:/root/
#   ssh root@HETZNER-IP "bash /root/setup_hetzner.sh"
#
# Voraussetzung: Ubuntu 24.04, frischer Server, als root eingeloggt

set -euo pipefail

# ─── Farben für Output ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
info() { echo -e "${BLUE}[→]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# ─── Konfiguration ────────────────────────────────────────────────────────────
APP_DIR="/opt/vollpension-agent"
APP_USER="agent"

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║     Vollpension Agent – Hetzner Setup                     ║"
echo "║     Pipedrive → Sevdesk Agentic Workflow                  ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# ─── 1. System-Updates ────────────────────────────────────────────────────────
info "System-Updates..."
apt-get update -qq && apt-get upgrade -y -qq
apt-get install -y -qq curl git ufw ca-certificates gnupg lsb-release
log "System aktuell"

# ─── 2. Docker installieren ───────────────────────────────────────────────────
if ! command -v docker &> /dev/null; then
    info "Docker installieren..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    log "Docker installiert: $(docker --version)"
else
    log "Docker bereits vorhanden: $(docker --version)"
fi

# Docker Compose (als Plugin, modern)
if ! docker compose version &> /dev/null; then
    info "Docker Compose Plugin installieren..."
    apt-get install -y -qq docker-compose-plugin
fi
log "Docker Compose: $(docker compose version)"

# ─── 3. Firewall (UFW) ───────────────────────────────────────────────────────
info "Firewall einrichten..."
ufw --force reset > /dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh        # Port 22 – WICHTIG: immer offen lassen!
ufw allow 80/tcp     # HTTP  (Caddy: HTTPS-Redirect + Let's Encrypt Challenge)
ufw allow 443/tcp    # HTTPS (Caddy)
ufw --force enable
log "Firewall aktiv (SSH + HTTP + HTTPS erlaubt)"

# ─── 4. App-User und Verzeichnis ──────────────────────────────────────────────
info "App-Verzeichnis einrichten: $APP_DIR"
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /bin/bash -d "$APP_DIR" "$APP_USER"
fi
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"
usermod -aG docker "$APP_USER"
log "User '$APP_USER' und Verzeichnis $APP_DIR bereit"

# ─── 5. Code deployen ─────────────────────────────────────────────────────────
info "Code nach $APP_DIR kopieren..."
# Annahme: Script wird aus dem Projektverzeichnis gestartet
# ODER: Code ist bereits in /tmp/vollpension-agent vorhanden
if [ -f "$(dirname "$0")/webhook_server.py" ]; then
    # Script läuft aus dem Projektordner
    cp -r "$(dirname "$0")"/* "$APP_DIR/"
    log "Code aus aktuellem Verzeichnis kopiert"
elif [ -d "/tmp/vollpension-agent" ]; then
    cp -r /tmp/vollpension-agent/* "$APP_DIR/"
    log "Code aus /tmp/vollpension-agent kopiert"
else
    warn "Code konnte nicht automatisch kopiert werden."
    warn "Bitte manuell: scp -r ./Pipedrive_Sevdesk_Agent/* root@HETZNER-IP:$APP_DIR/"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ─── 6. .env Datei erstellen ──────────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    info "Erstelle .env Datei..."
    echo ""
    warn "Jetzt werden alle API-Keys und Konfigurationswerte abgefragt."
    warn "Die Werte findest du in .env.example mit Erklärungen."
    echo ""

    read -rp "  ANTHROPIC_API_KEY:          " ANTHROPIC_API_KEY
    read -rp "  PIPEDRIVE_API_TOKEN:        " PIPEDRIVE_API_TOKEN
    read -rp "  PIPEDRIVE_DOMAIN (z.B. vollpension): " PIPEDRIVE_DOMAIN
    read -rp "  PIPEDRIVE_PIPELINE_ID:      " PIPEDRIVE_PIPELINE_ID
    read -rp "  PIPEDRIVE_STAGE_ANFRAGEN:   " PIPEDRIVE_STAGE_ANFRAGEN
    read -rp "  PIPEDRIVE_STAGE_IN_BEARBEITUNG: " PIPEDRIVE_STAGE_IN_BEARBEITUNG
    read -rp "  PIPEDRIVE_STAGE_ANGEBOT_GELEGT: " PIPEDRIVE_STAGE_ANGEBOT_GELEGT
    read -rp "  SEVDESK_API_TOKEN:          " SEVDESK_API_TOKEN
    read -rp "  SLACK_BOT_TOKEN:            " SLACK_BOT_TOKEN
    read -rp "  SLACK_SIGNING_SECRET:       " SLACK_SIGNING_SECRET
    read -rp "  SLACK_APPROVAL_CHANNEL:     " SLACK_APPROVAL_CHANNEL
    read -rp "  SLACK_PERSON1_ID:           " SLACK_PERSON1_ID
    read -rp "  SLACK_PERSON2_ID:           " SLACK_PERSON2_ID

    # Sicherheits-Secrets automatisch generieren
    WEBHOOK_SECRET=$(openssl rand -hex 32)
    CRON_SECRET=$(openssl rand -hex 32)
    ADMIN_SECRET=$(openssl rand -hex 32)

    cat > "$APP_DIR/.env" << ENVEOF
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
PIPEDRIVE_API_TOKEN=$PIPEDRIVE_API_TOKEN
PIPEDRIVE_DOMAIN=$PIPEDRIVE_DOMAIN
PIPEDRIVE_PIPELINE_ID=$PIPEDRIVE_PIPELINE_ID
PIPEDRIVE_STAGE_ANFRAGEN=$PIPEDRIVE_STAGE_ANFRAGEN
PIPEDRIVE_STAGE_IN_BEARBEITUNG=$PIPEDRIVE_STAGE_IN_BEARBEITUNG
PIPEDRIVE_STAGE_ANGEBOT_GELEGT=$PIPEDRIVE_STAGE_ANGEBOT_GELEGT
SEVDESK_API_TOKEN=$SEVDESK_API_TOKEN
SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN
SLACK_SIGNING_SECRET=$SLACK_SIGNING_SECRET
SLACK_APPROVAL_CHANNEL=$SLACK_APPROVAL_CHANNEL
SLACK_PERSON1_ID=$SLACK_PERSON1_ID
SLACK_PERSON2_ID=$SLACK_PERSON2_ID
WEBHOOK_SECRET=$WEBHOOK_SECRET
CRON_SECRET=$CRON_SECRET
ADMIN_SECRET=$ADMIN_SECRET
ENVEOF

    chmod 600 "$APP_DIR/.env"
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    log ".env erstellt und gesichert (chmod 600)"

    echo ""
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  Generierte Secrets – bitte notieren!               │"
    echo "  │                                                       │"
    echo "  │  WEBHOOK_SECRET:  $WEBHOOK_SECRET  │"
    echo "  │  CRON_SECRET:     $CRON_SECRET  │"
    echo "  │  ADMIN_SECRET:    $ADMIN_SECRET  │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""
else
    log ".env bereits vorhanden – wird nicht überschrieben"
fi

# ─── 7. Domain in Caddyfile eintragen ────────────────────────────────────────
if [ -f "$APP_DIR/Caddyfile" ]; then
    echo ""
    read -rp "  Domain für HTTPS (z.B. agent.vollpension.wien): " AGENT_DOMAIN
    if [ -n "$AGENT_DOMAIN" ]; then
        sed -i "s/agent\.vollpension\.wien/$AGENT_DOMAIN/g" "$APP_DIR/Caddyfile"
        log "Caddyfile: Domain auf '$AGENT_DOMAIN' gesetzt"
    fi
fi

# ─── 8. Docker Container starten ──────────────────────────────────────────────
info "Docker Container bauen und starten..."
cd "$APP_DIR"
docker compose pull caddy 2>/dev/null || true
docker compose up -d --build
log "Container gestartet"

# Kurz warten und Health-Check
sleep 8
if docker compose ps | grep -q "healthy\|running"; then
    log "Agent läuft!"
else
    warn "Container-Status unklar – prüfe mit: docker compose logs agent"
fi

# ─── 9. Cron-Job für Mahnwesen ────────────────────────────────────────────────
info "Cron-Job für tägliches Mahnwesen einrichten..."
CRON_SECRET_VAL=$(grep CRON_SECRET "$APP_DIR/.env" | cut -d= -f2)
DOMAIN_VAL=$(grep -oP 'agent\.[a-z.]+' "$APP_DIR/Caddyfile" | head -1)
CRON_LINE="0 8 * * * curl -s -H \"X-Cron-Secret: ${CRON_SECRET_VAL}\" https://${DOMAIN_VAL}/cron/dunning-check > /dev/null 2>&1"

(crontab -u "$APP_USER" -l 2>/dev/null || true; echo "$CRON_LINE") | crontab -u "$APP_USER" -
log "Cron-Job eingerichtet (täglich 08:00 Uhr)"

# ─── 10. Zusammenfassung ──────────────────────────────────────────────────────
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "DEINE-IP")
echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║  ✅  Setup abgeschlossen!                                  ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "  Server-IP:    $SERVER_IP"
echo "  App-Pfad:     $APP_DIR"
echo ""
echo "  Nächste Schritte:"
echo ""
echo "  1. DNS: A-Record  ${AGENT_DOMAIN:-agent.vollpension.wien}  →  $SERVER_IP"
echo "     (bei deinem Domain-Anbieter oder Cloudflare)"
echo ""
echo "  2. Pipedrive Webhooks einrichten (beide auf gleiche URL):"
echo "     URL: https://${AGENT_DOMAIN:-agent.vollpension.wien}/webhook/pipedrive"
echo "     Webhook 1: Object=deal, Action=added"
echo "     Webhook 2: Object=deal, Action=updated"
echo "     Basic Auth: Password = WEBHOOK_SECRET aus .env"
echo ""
echo "  3. Slack App: Interactivity URL setzen auf:"
echo "     https://${AGENT_DOMAIN:-agent.vollpension.wien}/webhook/slack/interactive"
echo "     Events URL:"
echo "     https://${AGENT_DOMAIN:-agent.vollpension.wien}/webhook/slack/events"
echo ""
echo "  Nützliche Befehle:"
echo "    Logs:    cd $APP_DIR && docker compose logs -f agent"
echo "    Restart: cd $APP_DIR && docker compose restart agent"
echo "    Update:  cd $APP_DIR && docker compose up -d --build"
echo "    Status:  cd $APP_DIR && docker compose ps"
echo ""
