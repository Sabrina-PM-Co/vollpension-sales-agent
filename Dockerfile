# ─── Pipedrive → Sevdesk Agent ────────────────────────────────────────────────
# Python 3.12 slim – minimales Image
FROM python:3.12-slim

# Kein .pyc, kein gepufferter Output (wichtig für Logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies zuerst (Layer-Cache nutzen)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code kopieren
COPY . .

# Port für uvicorn
EXPOSE 8000

# Health-Check damit Docker/Compose den Status kennt
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Start
CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
