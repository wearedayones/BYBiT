"""Email delivery for reports — ports src/reports/emailer.ts.

SMTP is skipped when BYBIT_PROXY_URL is set (restricted cloud environment where
SMTP ports are blocked). Telegram handles delivery there instead. Reports are
always stored in report_history regardless of delivery outcome.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient

log = get_logger().bind(module="emailer")


async def send_report(subject: str, html: str, period: str, db: NeonHttpClient) -> None:
    to = os.environ.get("REPORT_EMAIL")
    password = os.environ.get("REPORT_EMAIL_APP_PASSWORD")
    proxy_url = os.environ.get("BYBIT_PROXY_URL")

    # SMTP only when not behind a proxy (restricted cloud env) and credentials exist.
    smtp_enabled = bool(to and password and not proxy_url)
    delivered_ok = False

    if smtp_enabled:
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "587"))
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = to
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(html, "html"))

            context = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as server:
                    server.login(to, password)
                    server.sendmail(to, [to], msg.as_string())
            else:
                with smtplib.SMTP(host, port, timeout=10) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.login(to, password)
                    server.sendmail(to, [to], msg.as_string())
            delivered_ok = True
            log.info("Report sent via email", subject=subject, period=period)
        except Exception as e:
            log.warning("SMTP send failed — report stored in DB only", subject=subject, error=str(e))

    try:
        await db.execute(
            """INSERT INTO report_history (period, subject, html_body, delivered_to, delivered_ok)
               VALUES ($1, $2, $3, $4, $5)""",
            period, subject, html, to, delivered_ok,
        )
    except Exception as e:
        log.error("Failed to save report to DB", error=str(e))
