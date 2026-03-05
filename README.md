# Pipedrive → Sevdesk Agentic Workflow 🤖

Automatischer Workflow: Neuer Lead via Pipedrive-Webformular → Claude analysiert → Sevdesk-Angebot als Entwurf → E-Mail-Benachrichtigung zur Freigabe.

---

## Workflow-Übersicht

```
Pipedrive Webform
       │
       ▼ (neuer Deal angelegt)
Pipedrive Webhook ──► FastAPI Server (webhook_server.py)
                              │
                              ▼
                    Claude Agent (claude-opus-4-6)
                              │
                 ┌────────────┼────────────────┐
                 ▼            ▼                ▼
         Pipedrive API   Sevdesk API      E-Mail-Notify
         - Deal laden    - Kontakt suchen  - Draft-Link
         - Felder        - Kontakt anlegen - zur Freigabe
           anreichern    - Angebot Draft
                              │
                              ▼
                    Deal in Pipedrive aktualisiert
                    (Angebotsnummer als Custom Field)
```

---

## Installation

### 1. Python-Abhängigkeiten

```bash
cd pipedrive_sevdesk_agent
pip3 install -r requirements.txt
```

### 2. Umgebungsvariablen konfigurieren

```bash
cp .env.example .env
# .env öffnen und alle Werte eintragen:
open -e .env  # macOS
```

**API-Keys holen:**
- **Anthropic:** https://console.anthropic.com → API-Keys
- **Pipedrive:** Profil-Icon → Persönliche Einstellungen → API
- **Sevdesk:** Einstellungen → Benutzer → API-Token (ganz unten)
- **Gmail SMTP:** Google-Account → Sicherheit → App-Passwörter (2FA muss aktiv sein)

### 3. Angebotspositionen anpassen

In `config.py` die `DEFAULT_OFFER_POSITIONS` anpassen – das sind die Standard-Positionen, die Claude ins Angebot einbaut, wenn keine spezifischeren Infos im Deal vorliegen.

---

## Server starten

```bash
# Lokal (für Tests)
python webhook_server.py

# Produktiv
uvicorn webhook_server:app --host 0.0.0.0 --port 8000
```

Server läuft dann auf: http://localhost:8000

---

## Pipedrive Webhook einrichten

1. Pipedrive → **Einstellungen** → **Tools & Integrationen** → **Webhooks** → **+ Webhook**
2. Settings:
   - **Event action:** `Added`
   - **Event object:** `Deal`
   - **Endpoint URL:** `https://DEINE-DOMAIN.com/webhook/pipedrive`
   - **HTTP Basic Auth** (optional): Username = `webhook`, Password = dein `WEBHOOK_SECRET`
3. Speichern

> **Für lokale Tests:** [ngrok](https://ngrok.com) verwenden um `localhost:8000` öffentlich erreichbar zu machen:
> ```bash
> ngrok http 8000
> # gibt dir eine URL wie https://abc123.ngrok.io
> ```

---

## Hosting-Optionen

| Anbieter | Preis | Aufwand | Empfehlung |
|----------|-------|---------|------------|
| [Railway.app](https://railway.app) | ~5$/Monat | sehr einfach | ⭐ Empfohlen |
| [Render.com](https://render.com) | Free-Tier möglich | einfach | ✅ |
| VPS (Hetzner etc.) | ~4€/Monat | mittel | für Profis |
| ngrok | kostenlos | nur für Tests | 🧪 |

### Railway Deployment (empfohlen):

```bash
# Railway CLI installieren
npm i -g @railway/cli

# Einloggen und deployen
railway login
railway init
railway up

# Env-Variablen setzen
railway variables set ANTHROPIC_API_KEY=sk-ant-...
# (alle anderen Variablen aus .env.example ebenso)
```

---

## Sevdesk Custom Fields in Pipedrive

Empfehlung: Custom Field `sevdesk_angebot_nr` im Pipedrive-Deal anlegen, damit der Agent die Angebotsnummer automatisch einträgt.

**Anlegen:** Pipedrive → Einstellungen → Datenfelder → Deals → + Feld
→ Typ: Text, Name: `Sevdesk Angebotsnummer`
→ Den generierten Field-Key in `agent.py` eintragen (z.B. `abc1234de5fghij`)

---

## Manueller Test

```bash
# Agent direkt mit einer Deal-ID testen (ohne Webhook)
python -c "from agent import process_new_deal; print(process_new_deal(12345))"
```

---

## Dateien

| Datei | Beschreibung |
|-------|-------------|
| `webhook_server.py` | FastAPI-Server, empfängt Pipedrive-Webhooks |
| `agent.py`          | Claude-Agent, orchestriert den Workflow |
| `pipedrive_tools.py`| Pipedrive REST API Wrapper |
| `sevdesk_tools.py`  | Sevdesk REST API Wrapper |
| `notify.py`         | E-Mail-Benachrichtigung |
| `config.py`         | Konfiguration aus .env |
| `.env.example`      | Vorlage für Umgebungsvariablen |

---

## Wichtige Hinweise zu Sevdesk

Die Sevdesk API ändert sich gelegentlich. Falls die Angebotserstellung fehlschlägt, prüfe in `sevdesk_tools.py`:

- **Angebot-Endpunkt:** `POST /Order` mit `orderType: "AN"` (Angebot) ← Standard
- **Kontakt-Kategorie:** ID `3` = Kunde/Debitor (kann je nach Account abweichen)
- **Adress-Kategorie:** ID `48` = Hauptadresse

Aktuelle API-Docs: https://api.sevdesk.de/

---

## Sicherheit

- API-Keys **nie** in den Code schreiben – immer über `.env`
- `.env` in `.gitignore` aufnehmen
- Webhook-Secret für Pipedrive verwenden
- SMTP: App-Passwörter statt Haupt-Passwort
