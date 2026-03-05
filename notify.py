#!/usr/bin/env python3
"""
E-Mail-Benachrichtigung
========================
Sendet eine Benachrichtigung, wenn Claude ein Angebot als Entwurf angelegt hat.
Der Empfänger kann den Link öffnen, das Angebot prüfen und in Sevdesk freigeben.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from config import (
    NOTIFY_EMAIL_TO, NOTIFY_EMAIL_FROM,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
)


def send_offer_notification(
    deal_title: str,
    contact_name: str,
    contact_email: str,
    offer_number: str,
    sevdesk_link: str,
    pipedrive_deal_id: int,
    valid_until: str,
    positions_summary: list,
) -> bool:
    """
    Sendet eine E-Mail-Benachrichtigung mit dem Link zum Sevdesk-Angebotsentwurf.

    Args:
        deal_title:         Titel des Deals / Angebots.
        contact_name:       Name des Interessenten.
        contact_email:      E-Mail des Interessenten.
        offer_number:       Sevdesk-Angebotsnummer.
        sevdesk_link:       Direktlink zum Angebot in Sevdesk.
        pipedrive_deal_id:  ID des Pipedrive-Deals.
        valid_until:        Gültigkeitsdatum (ISO-Format YYYY-MM-DD).
        positions_summary:  Liste der Positionen für die E-Mail-Vorschau.

    Returns:
        True bei Erfolg, False bei Fehler (Fehler wird ins Log geschrieben, wirft nicht).
    """
    try:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")

        # Positionen als HTML-Tabelle
        positions_html = "".join(
            f"<tr><td>{p.get('name','')}</td>"
            f"<td>{p.get('quantity',1)} {p.get('unit','Stk.')}</td>"
            f"<td>€ {p.get('price', 0):,.2f}</td></tr>"
            for p in positions_summary
        )

        html_body = f"""
<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"></head>
<body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">

  <div style="background: #1a1a2e; color: white; padding: 20px 30px; border-radius: 8px 8px 0 0;">
    <h2 style="margin:0;">🤖 Neuer Angebotsentwurf bereit</h2>
    <p style="margin: 5px 0 0; color: #aaa; font-size: 13px;">Erstellt am {now}</p>
  </div>

  <div style="background: #f9f9f9; padding: 25px 30px; border: 1px solid #ddd;">

    <h3 style="color: #1a1a2e; margin-top: 0;">📋 Deal-Details</h3>
    <table style="width:100%; border-collapse: collapse; margin-bottom: 20px;">
      <tr>
        <td style="padding: 6px 0; color: #666; width: 40%;">Deal-Titel</td>
        <td style="padding: 6px 0;"><strong>{deal_title}</strong></td>
      </tr>
      <tr>
        <td style="padding: 6px 0; color: #666;">Interessent</td>
        <td style="padding: 6px 0;">{contact_name} &lt;{contact_email}&gt;</td>
      </tr>
      <tr>
        <td style="padding: 6px 0; color: #666;">Angebotsnummer</td>
        <td style="padding: 6px 0;">{offer_number}</td>
      </tr>
      <tr>
        <td style="padding: 6px 0; color: #666;">Gültig bis</td>
        <td style="padding: 6px 0;">{valid_until}</td>
      </tr>
      <tr>
        <td style="padding: 6px 0; color: #666;">Pipedrive Deal-ID</td>
        <td style="padding: 6px 0;">#{pipedrive_deal_id}</td>
      </tr>
    </table>

    <h3 style="color: #1a1a2e;">📦 Positionen (Vorschau)</h3>
    <table style="width:100%; border-collapse: collapse; font-size: 14px; margin-bottom: 20px;">
      <thead>
        <tr style="background: #e8e8e8;">
          <th style="padding: 8px; text-align: left; border-bottom: 1px solid #ccc;">Bezeichnung</th>
          <th style="padding: 8px; text-align: left; border-bottom: 1px solid #ccc;">Menge</th>
          <th style="padding: 8px; text-align: right; border-bottom: 1px solid #ccc;">Preis (netto)</th>
        </tr>
      </thead>
      <tbody>
        {positions_html if positions_html else '<tr><td colspan="3" style="padding:8px; color:#888;">Positionen in Sevdesk prüfen</td></tr>'}
      </tbody>
    </table>

    <div style="text-align: center; margin: 30px 0;">
      <a href="{sevdesk_link}"
         style="background: #16a34a; color: white; padding: 14px 30px;
                text-decoration: none; border-radius: 6px; font-size: 16px;
                font-weight: bold; display: inline-block;">
        ✅ Angebot in Sevdesk öffnen &amp; freigeben
      </a>
    </div>

    <p style="font-size: 12px; color: #999; text-align: center; margin-top: 20px;">
      Das Angebot ist als <strong>Entwurf</strong> gespeichert und wurde noch nicht versendet.<br>
      Bitte prüfen, anpassen und dann in Sevdesk freigeben / versenden.
    </p>
  </div>

  <div style="background: #e8e8e8; padding: 12px 30px; border-radius: 0 0 8px 8px;
              font-size: 11px; color: #666; text-align: center;">
    Automatisch erstellt von deinem Pipedrive → Sevdesk Agenten 🤖
  </div>

</body>
</html>
"""

        plain_body = (
            f"Neuer Angebotsentwurf bereit – {now}\n\n"
            f"Deal:          {deal_title}\n"
            f"Interessent:   {contact_name} <{contact_email}>\n"
            f"Angebotsnr.:   {offer_number}\n"
            f"Gültig bis:    {valid_until}\n\n"
            f"→ Sevdesk Link: {sevdesk_link}\n\n"
            f"Das Angebot ist als Entwurf gespeichert. Bitte prüfen und freigeben.\n"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📋 Neues Angebot bereit: {deal_title}"
        msg["From"]    = NOTIFY_EMAIL_FROM
        msg["To"]      = NOTIFY_EMAIL_TO
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body,  "html",  "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, msg.as_string())

        print(f"✉️  Benachrichtigung gesendet an {NOTIFY_EMAIL_TO}")
        return True

    except Exception as exc:
        print(f"⚠️  E-Mail-Benachrichtigung fehlgeschlagen: {exc}")
        return False
