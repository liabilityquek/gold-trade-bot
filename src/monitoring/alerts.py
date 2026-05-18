"""Alert management for Telegram notifications — Gold Trading Bot.

Commands:
  /stop      — activate kill switch (halt all trading)
  /resume    — deactivate kill switch (resume trading)
  /status    — current bot status, balance, open trades
  /calendar  — upcoming gold-relevant economic events
  /logs      — today's bot log (last 30 lines)
  /credits   — LLM provider status
  /analyst   — last analyst decision
  /reviewer  — last reviewer verdict
  /help      — command list
"""

import logging
import os
import threading
import time
import requests
from typing import Optional, Callable
from datetime import datetime, date

from config.settings import settings


class AlertManager:
    """Manage Telegram alerts for gold trading notifications."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('alerts')
        self.enabled = settings.ALERT_ENABLED
        self.bot_token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '') or ''
        self.chat_id = getattr(settings, 'TELEGRAM_CHAT_ID', '') or ''

        if self.enabled:
            if not self.bot_token or not self.chat_id:
                self.logger.warning("Telegram credentials missing. Alerts disabled.")
                self.enabled = False
            else:
                self.logger.info("Telegram alerts initialized")

        # Poller attributes — initialized here to prevent AttributeError
        self._kill_switch_ref = None
        self._get_status_fn = None
        self._get_calendar_fn = None
        self._get_credits_fn = None
        self._get_analyst_fn = None
        self._get_reviewer_fn = None
        self._poll_interval = 10
        self._poll_failures = 0
        self._last_update_id = 0

    def _send_telegram(self, text: str, parse_mode: str = 'Markdown') -> bool:
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': parse_mode,
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            safe_error = str(e)
            if self.bot_token:
                safe_error = safe_error.replace(self.bot_token, '[REDACTED]')
            self.logger.error(f"Failed to send Telegram alert: {safe_error}")
            return False

    def send_alert(self, message: str, priority: str = 'INFO') -> bool:
        if not self.enabled:
            self.logger.debug(f"Alert (disabled): {message}")
            return False

        priority_emoji = {
            'INFO': 'i',
            'WARNING': '!',
            'ERROR': 'X',
            'CRITICAL': '!!',
        }.get(priority, 'i')

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted_message = (
            f"*Gold Bot Alert*\n\n"
            f"[{priority_emoji}] *{priority}*\n"
            f"{message}\n\n"
            f"_{timestamp}_"
        )

        success = self._send_telegram(formatted_message)
        if success:
            self.logger.info(f"Telegram alert sent: {message[:60]}")
        return success

    def alert_trade_opened(
        self,
        side: str,
        units: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        tp2: Optional[float] = None,
        tp3: Optional[float] = None,
        setup_type: str = "",
    ):
        direction_emoji = "BUY" if side.upper() == 'BUY' else "SELL"
        tp2_str = f"\n*TP2:* `{tp2:.2f}`" if tp2 else ""
        tp3_str = f"\n*TP3:* `{tp3:.2f}`" if tp3 else ""
        setup_str = f"\n*Setup:* {setup_type}" if setup_type else ""

        message = (
            f"*Trade Opened — XAU/USD {direction_emoji}*\n\n"
            f"*Size:* {units} oz\n"
            f"*Entry:* `{entry_price:.2f}`\n"
            f"*SL:* `{stop_loss:.2f}`\n"
            f"*TP1:* `{take_profit:.2f}`"
            f"{tp2_str}{tp3_str}{setup_str}"
        )
        self.send_alert(message, 'INFO')

    def alert_trade_closed(
        self,
        trade_id: str,
        pnl: float,
        reason: str = "Unknown",
        close_price: float = 0.0,
        entry_price: float = 0.0,
        points: float = 0.0,
    ):
        emoji = "WIN" if pnl >= 0 else "LOSS"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pts_str = f"+{points:.1f}" if points >= 0 else f"{points:.1f}"

        message = (
            f"*Trade Closed — XAU/USD [{emoji}]*\n\n"
            f"*ID:* {trade_id}\n"
            f"*Reason:* {reason}\n"
            f"*Entry:* `{entry_price:.2f}`\n"
            f"*Close:* `{close_price:.2f}` ({pts_str} pts)\n"
            f"*P/L:* {pnl_str}"
        )
        priority = 'INFO' if pnl >= 0 else 'WARNING'
        self.send_alert(message, priority)

    def alert_error(self, error_message: str):
        message = f"*Error*\n\n`{error_message}`"
        self.send_alert(message, 'ERROR')

    def alert_llm_credits_exhausted(self):
        message = (
            "*LLM Credits Exhausted*\n\n"
            "Both Groq and Anthropic API credits are depleted.\n"
            "The LLM analyst is offline — trading halted this cycle.\n\n"
            "Top up credits to restore functionality."
        )
        self.send_alert(message, 'CRITICAL')

    def alert_reviewer_unavailable(self, reason: str):
        message = (
            f"*Reviewer Unavailable*\n\n"
            f"*Pair:* XAU_USD\n"
            f"*Reason:* {reason}\n"
            f"Trade blocked — reviewer could not run this cycle."
        )
        self.send_alert(message, 'WARNING')

    def alert_system_start(self):
        mode = "PAPER" if settings.PAPER_TRADING_MODE else "LIVE"
        message = f"*Gold Bot Started*\n\n*Mode:* {mode}\n*Instrument:* XAU/USD"
        self.send_alert(message, 'INFO')

    def alert_system_stop(self):
        message = "*Gold Bot Stopped*\n\nTrading bot has been shut down."
        self.send_alert(message, 'INFO')

    def alert_news_suspend(self, event_name: str):
        message = (
            f"*Trading Suspended — Gold News*\n\n"
            f"*Event:* {event_name}\n"
            f"*Window:* {settings.NEWS_SUSPEND_BEFORE_MINUTES}min before "
            f"→ {settings.NEWS_RESUME_AFTER_MINUTES}min after"
        )
        self.send_alert(message, 'WARNING')

    def alert_emergency_stop(self, reason: str):
        message = (
            f"*EMERGENCY STOP — XAU/USD*\n\n"
            f"*Reason:* {reason}\n"
            f"All positions closed. Trading halted."
        )
        self.send_alert(message, 'CRITICAL')

    def test_connection(self) -> bool:
        if not self.bot_token or not self.chat_id:
            self.logger.error("Telegram credentials not configured")
            return False
        return self._send_telegram("*Test Alert*\n\nGold Bot Telegram alerts working!")

    # ------------------------------------------------------------------
    # Telegram command poller
    # ------------------------------------------------------------------

    def start_command_poller(
        self,
        kill_switch=None,
        get_status_fn: Optional[Callable[[], str]] = None,
        get_calendar_fn: Optional[Callable[[], str]] = None,
        get_credits_fn: Optional[Callable[[], str]] = None,
        get_analyst_fn: Optional[Callable[[], str]] = None,
        get_reviewer_fn: Optional[Callable[[], str]] = None,
        poll_interval_seconds: int = 10,
    ) -> None:
        if not self.enabled:
            self.logger.debug("Telegram alerts disabled — command poller not started")
            return

        self._kill_switch_ref = kill_switch
        self._get_status_fn = get_status_fn
        self._get_calendar_fn = get_calendar_fn
        self._get_credits_fn = get_credits_fn
        self._get_analyst_fn = get_analyst_fn
        self._get_reviewer_fn = get_reviewer_fn
        self._poll_interval = poll_interval_seconds
        self._poll_failures = 0
        self._last_update_id = self._fetch_latest_update_id()

        thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="TelegramCommandPoller",
        )
        thread.start()
        self.logger.info(
            f"Telegram command poller started (interval: {poll_interval_seconds}s)"
        )

    def _fetch_latest_update_id(self) -> int:
        if not self.bot_token:
            return 0
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getUpdates",
                params={"offset": -1, "timeout": 0},
                timeout=8,
            )
            resp.raise_for_status()
            results = resp.json().get("result", [])
            if results:
                return results[-1]["update_id"]
        except Exception:
            pass
        return 0

    def _poll_loop(self) -> None:
        while True:
            try:
                self._check_commands()
            except Exception as exc:
                self._poll_failures += 1
                if self._poll_failures >= 3:
                    safe = str(exc)
                    if self.bot_token:
                        safe = safe.replace(self.bot_token, "[REDACTED]")
                    self.logger.warning(f"Telegram poll error (x{self._poll_failures}): {safe}")
            time.sleep(self._poll_interval)

    def _check_commands(self) -> None:
        if not self.bot_token:
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {
            "offset": self._last_update_id + 1,
            "timeout": 0,
            "allowed_updates": ["message"],
        }

        try:
            resp = requests.get(url, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            self._poll_failures = 0
        except requests.RequestException as exc:
            self._poll_failures += 1
            safe = str(exc)
            if self.bot_token:
                safe = safe.replace(self.bot_token, "[REDACTED]")
            if self._poll_failures >= 3:
                self.logger.warning(f"getUpdates failed (x{self._poll_failures}): {safe}")
            return

        for update in data.get("result", []):
            self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip().lower()
            from_chat = str(msg.get("chat", {}).get("id", ""))

            if from_chat != str(self.chat_id):
                self.logger.debug(f"Ignoring message from chat {from_chat}")
                continue

            self.logger.debug(f"Telegram command received: {text!r}")
            try:
                if text in ("/stop", "/kill"):
                    self._handle_stop()
                elif text in ("/resume", "/start"):
                    self._handle_resume()
                elif text == "/status":
                    self._handle_status()
                elif text == "/calendar":
                    self._handle_calendar()
                elif text == "/logs":
                    self._handle_logs()
                elif text == "/credits":
                    self._handle_credits()
                elif text == "/analyst":
                    self._handle_analyst()
                elif text == "/reviewer":
                    self._handle_reviewer()
                elif text == "/help":
                    self._send_telegram(
                        "*Gold Bot Commands*\n\n"
                        "/stop — activate kill switch (halt trading, positions stay open)\n"
                        "/resume — deactivate kill switch (resume trading)\n"
                        "/status — balance, NAV, open trades, unrealized P/L\n"
                        "/calendar — next 24h gold-relevant economic events\n"
                        "/logs — today's bot log (last 50 lines)\n"
                        "/credits — LLM provider status (Groq + Anthropic)\n"
                        "/analyst — last analyst decision + confidence\n"
                        "/reviewer — last reviewer verdict + reason\n"
                        "/help — show this message"
                    )
            except Exception as exc:
                self.logger.error(f"Error handling command {text!r}: {exc}")

    def _handle_stop(self) -> None:
        ks = getattr(self, '_kill_switch_ref', None)
        if ks:
            ks.activate("Telegram /stop command")
        self._send_telegram(
            "*Kill Switch Activated*\n\n"
            "All trading halted. Open positions remain live at broker with existing SL/TP.\n"
            "Send /resume to reactivate trading."
        )
        self.logger.warning("Kill switch activated via Telegram /stop")

    def _handle_resume(self) -> None:
        ks = getattr(self, '_kill_switch_ref', None)
        if ks:
            ks.deactivate()
        self._send_telegram(
            "*Kill Switch Deactivated*\n\n"
            "Trading will resume on the next cycle."
        )
        self.logger.info("Kill switch deactivated via Telegram /resume")

    def _handle_status(self) -> None:
        ks = getattr(self, '_kill_switch_ref', None)
        ks_status = "HALTED" if (ks and ks.is_active()) else "ACTIVE"

        fn = getattr(self, '_get_status_fn', None)
        extra = fn() if fn else ""

        msg = (
            f"Gold Bot Status\n\n"
            f"Trading: {ks_status}\n"
            f"Mode: {'PAPER' if settings.PAPER_TRADING_MODE else 'LIVE'}\n"
            f"Instrument: XAU/USD\n"
        )
        if extra:
            msg += f"\n{extra}"

        self._send_telegram(msg, parse_mode="")

    def _handle_calendar(self) -> None:
        fn = getattr(self, '_get_calendar_fn', None)
        if not fn:
            self._send_telegram("Calendar not configured.", parse_mode="")
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode="")
        except Exception as exc:
            self._send_telegram(f"Calendar fetch failed: {exc}", parse_mode="")

    def _handle_logs(self) -> None:
        log_path = settings.LOG_FILE_PATH
        today_str = date.today().strftime("%Y-%m-%d")

        if not os.path.exists(log_path):
            self._send_telegram(f"*Logs*\n\nLog file not found: `{log_path}`.")
            return

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            today_lines = [ln.rstrip() for ln in lines if today_str in ln]

            if not today_lines:
                self._send_telegram(f"*Logs*\n\nNo entries for today ({today_str}).")
                return

            tail = today_lines[-50:]
            text = "\n".join(tail)
            if len(text) > 3800:
                text = "...\n" + text[-3800:]

            self._send_telegram(
                f"*Logs — {today_str}*\n\n```\n{text}\n```",
                parse_mode="Markdown",
            )
        except Exception as exc:
            self._send_telegram(f"*Logs*\n\nFailed to read log: {exc}")

    def _handle_credits(self) -> None:
        fn = getattr(self, '_get_credits_fn', None)
        if not fn:
            self._send_telegram("*LLM Credits*\n\nCredit status not available.")
            return
        try:
            status_text = fn()
            self._send_telegram(f"*LLM Provider Status*\n\n`{status_text}`")
        except Exception as exc:
            self._send_telegram(f"*LLM Credits*\n\nFailed to fetch status: {exc}")

    def _handle_analyst(self) -> None:
        fn = getattr(self, '_get_analyst_fn', None)
        if not fn:
            self._send_telegram("Analyst history not available.", parse_mode="")
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode="")
        except Exception as exc:
            self._send_telegram(f"Analyst history fetch failed: {exc}", parse_mode="")

    def _handle_reviewer(self) -> None:
        fn = getattr(self, '_get_reviewer_fn', None)
        if not fn:
            self._send_telegram("Reviewer history not available.", parse_mode="")
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode="")
        except Exception as exc:
            self._send_telegram(f"Reviewer history fetch failed: {exc}", parse_mode="")
