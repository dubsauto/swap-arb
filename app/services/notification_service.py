# app/services/notification_service.py

import os
import ssl
import smtplib
import asyncio
import httpx
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BOT_TOKEN      = os.getenv("telegram_bot_token", "")
EMAIL_HOST     = os.getenv("EMAIL_HOST", "")
EMAIL_PORT     = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER     = os.getenv("EMAIL_USER", "")
EMAIL_PASS     = os.getenv("EMAIL_PASS", "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "SWAPARB Alerts")


async def send_telegram(chat_id: str, text: str) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("telegram_bot_token not set in .env")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=12) as client:
        res = await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })
        res.raise_for_status()


async def send_email(to_email: str, subject: str, body: str) -> None:
    if not all([EMAIL_HOST, EMAIL_USER, EMAIL_PASS]):
        raise RuntimeError(
            "Email not configured — add EMAIL_HOST, EMAIL_USER, EMAIL_PASS to .env"
        )
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_sync, to_email, subject, body)


def _send_sync(to_email: str, subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{EMAIL_FROM_NAME} <{EMAIL_USER}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, to_email, msg.as_string())
