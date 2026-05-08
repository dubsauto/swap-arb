# swaparb/notification_worker.py

import asyncio
import os
import time
import httpx

from app.database import SessionLocal
from app.model import (
    TradingAccount, UserSlot, User,
    UserNotificationPrefs, NotificationSettings, NotificationLog,
    DEFAULT_NOTIFICATION_TEMPLATE,
)
from app.services.notification_service import send_telegram, send_email
from app.services.account_management import account_manager

BOT_TOKEN = os.getenv("telegram_bot_token", "")

# Re-notify same account at most once per hour
NOTIFY_COOLDOWN = 3600


class NotificationWorker:
    def __init__(self):
        self._running          = False
        self._last_notified    = {}   # account_id (int) → unix timestamp
        self._telegram_offset  = 0

    # =====================================
    # START
    # =====================================
    async def start(self):
        if self._running:
            return
        self._running = True
        print("🔔 Notification Worker started")
        await asyncio.gather(
            self._check_loop(),
            self._telegram_bot_loop(),
        )

    # =====================================
    # LOAD SETTINGS FROM DB
    # =====================================
    def _get_settings(self) -> dict:
        db = SessionLocal()
        try:
            row = db.query(NotificationSettings).first()
            if not row:
                return {
                    "margin_threshold_pct":   50.0,
                    "check_interval_minutes": 15,
                    "message_template":       DEFAULT_NOTIFICATION_TEMPLATE,
                }
            return {
                "margin_threshold_pct":   row.margin_threshold_pct,
                "check_interval_minutes": row.check_interval_minutes,
                "message_template":       row.message_template or DEFAULT_NOTIFICATION_TEMPLATE,
            }
        finally:
            db.close()

    # =====================================
    # MAIN CHECK LOOP
    # =====================================
    async def _check_loop(self):
        while True:
            try:
                settings = self._get_settings()
                await self._check_all_accounts(settings)
                await asyncio.sleep(settings["check_interval_minutes"] * 60)
            except Exception as e:
                print(f"❌ Notification check error: {e}")
                await asyncio.sleep(60)

    async def _check_all_accounts(self, settings: dict):
        db = SessionLocal()
        try:
            slots = (
                db.query(UserSlot)
                .filter(
                    UserSlot.master_account_id.isnot(None),
                    UserSlot.status == "active",
                )
                .all()
            )
        finally:
            db.close()

        for slot in slots:
            await self._check_slot(slot, settings)

    async def _check_slot(self, slot, settings: dict):
        db = SessionLocal()
        try:
            master = db.query(TradingAccount).get(slot.master_account_id)
            if not master or not master.metaapi_account_id:
                return
            if not master.state or master.state.upper() != "DEPLOYED":
                return

            # Throttle
            last = self._last_notified.get(master.id, 0)
            if time.time() - last < NOTIFY_COOLDOWN:
                return

            # Get live metrics (reuses account_management cache)
            try:
                metrics = await asyncio.wait_for(
                    account_manager.get_account_metrics(master.metaapi_account_id),
                    timeout=12,
                )
            except Exception as e:
                print(f"⚠️ Metrics fetch failed for {master.login}: {e}")
                return

            if not metrics:
                return

            # Compute margin level
            margin_level = self._compute_margin_level(metrics)
            if margin_level is None:
                return

            threshold = settings["margin_threshold_pct"]
            if margin_level > threshold:
                return  # still healthy

            print(
                f"🚨 Margin alert: account={master.login} "
                f"margin_level={margin_level:.1f}% threshold={threshold}%"
            )

            user  = db.query(User).get(slot.user_id)
            prefs = db.query(UserNotificationPrefs).filter_by(user_id=slot.user_id).first()

            msg = self._build_message(settings["message_template"], master, margin_level, metrics)

            channels_sent = []

            # ── Telegram ──────────────────────────────
            if prefs and prefs.telegram_chat_id and getattr(prefs, "notify_telegram", True):
                try:
                    await send_telegram(prefs.telegram_chat_id, msg)
                    channels_sent.append("telegram")
                    print(f"📩 Telegram sent → {user.username if user else slot.user_id}")
                except Exception as e:
                    print(f"⚠️ Telegram failed for user {slot.user_id}: {e}")

            # ── Email ─────────────────────────────────
            if user and user.email and (not prefs or getattr(prefs, "notify_email", True)):
                # Strip HTML tags for plain-text email
                plain = (msg
                         .replace("<b>", "").replace("</b>", "")
                         .replace("<i>", "").replace("</i>", ""))
                try:
                    await send_email(
                        user.email,
                        f"⚠️ Margin Alert — Account {master.login}",
                        plain,
                    )
                    channels_sent.append("email")
                    print(f"📧 Email sent → {user.email}")
                except Exception as e:
                    print(f"⚠️ Email failed for {user.email}: {e}")

            if channels_sent:
                self._last_notified[master.id] = time.time()
                log_db = SessionLocal()
                try:
                    log_db.add(NotificationLog(
                        user_id=slot.user_id,
                        account_id=master.id,
                        channel="+".join(channels_sent),
                        message=msg,
                        margin_level=margin_level,
                    ))
                    log_db.commit()
                finally:
                    log_db.close()

        finally:
            db.close()

    # =====================================
    # HELPERS
    # =====================================
    def _compute_margin_level(self, metrics: dict):
        level = metrics.get("margin_level")
        if level is not None:
            return float(level)
        equity = metrics.get("equity") or 0
        margin = metrics.get("margin") or 0
        if margin > 0:
            return (equity / margin) * 100
        return None

    def _build_message(self, template: str, account, margin_level: float, metrics: dict) -> str:
        try:
            return template.format(
                account_number=account.login,
                broker=account.server,
                margin_level=f"{margin_level:.1f}",
                balance=f"{metrics.get('balance') or 0:.2f}",
                equity=f"{metrics.get('equity') or 0:.2f}",
                margin=f"{metrics.get('margin') or 0:.2f}",
                free_margin=f"{metrics.get('free_margin') or 0:.2f}",
            )
        except Exception as e:
            print(f"⚠️ Template format error: {e}")
            return (
                f"⚠️ Margin Alert — Account {account.login} at {account.server}\n"
                f"Margin Level: {margin_level:.1f}% (below threshold)\n"
                "Please rebalance: withdraw from master, deposit into slave."
            )

    # =====================================
    # TELEGRAM BOT POLLING
    # Handles /start and /link TOKEN commands
    # =====================================
    async def _telegram_bot_loop(self):
        if not BOT_TOKEN:
            print("⚠️ telegram_bot_token not set — Telegram bot polling disabled")
            return

        print("🤖 Telegram bot polling started")
        while True:
            try:
                await self._poll_updates()
                await asyncio.sleep(2)
            except Exception as e:
                print(f"❌ Telegram poll error: {e}")
                await asyncio.sleep(15)

    async def _poll_updates(self):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.get(url, params={
                "offset": self._telegram_offset,
                "timeout": 5,
                "allowed_updates": ["message"],
            })
            data = res.json()

        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._telegram_offset = update["update_id"] + 1
            msg  = update.get("message", {})
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not chat_id:
                continue

            if text.lower().startswith("/link "):
                token = text[6:].strip().upper()
                await self._handle_link(chat_id, token)
            elif text.lower() in ("/start", "/help"):
                await self._bot_send(chat_id,
                    "👋 Welcome to <b>SWAPARB Alerts</b>!\n\n"
                    "To receive margin alerts here, link your account:\n"
                    "1. Open your SWAPARB dashboard\n"
                    "2. Find your <b>Telegram Link Token</b> in your profile\n"
                    "3. Send: <code>/link YOUR_TOKEN</code>"
                )

    async def _handle_link(self, chat_id: str, token: str):
        db = SessionLocal()
        try:
            prefs = (
                db.query(UserNotificationPrefs)
                .filter_by(telegram_link_token=token)
                .first()
            )
            if not prefs:
                await self._bot_send(chat_id,
                    "❌ Invalid token. Check your SWAPARB dashboard for the correct 8-character token."
                )
                return

            if prefs.telegram_chat_id == chat_id:
                await self._bot_send(chat_id, "✅ Your Telegram is already linked!")
                return

            prefs.telegram_chat_id = chat_id
            db.commit()

            user = db.query(User).get(prefs.user_id)
            name = user.username if user else "there"
            await self._bot_send(chat_id,
                f"✅ Linked successfully! Hi <b>{name}</b> 👋\n\n"
                "You'll now receive margin alerts here when action is needed."
            )
            print(f"🔗 Telegram linked: user={prefs.user_id} chat_id={chat_id}")
        except Exception as e:
            print(f"❌ Link error: {e}")
        finally:
            db.close()

    async def _bot_send(self, chat_id: str, text: str):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })


notification_worker = NotificationWorker()
