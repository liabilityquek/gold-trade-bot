"""
telegram_client.py — Send trade notifications via Telegram Bot API.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()


class TelegramClient:
    def __init__(self):
        self.token   = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = os.environ["TELEGRAM_CHAT_ID"]
        self.base    = f"https://api.telegram.org/bot{self.token}"

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        url  = f"{self.base}/sendMessage"
        data = {
            "chat_id":    self.chat_id,
            "text":       message,
            "parse_mode": parse_mode,
        }
        try:
            resp = requests.post(url, data=data, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[Telegram] send failed: {e}")
            return False

    def send_trade_entry(self, direction, entry, sl, tp1, tp2, tp3, units, risk_usd, reasoning):
        tp2_line = f"TP2: <b>{tp2:.2f}</b>\n" if tp2 else ""
        tp3_line = f"TP3: <b>{tp3:.2f}</b>\n" if tp3 else ""
        msg = (
            f"<b>XAUUSD {direction} EXECUTED</b>\n"
            f"{'=' * 28}\n"
            f"Entry : <b>{entry:.2f}</b>\n"
            f"SL    : <b>{sl:.2f}</b>\n"
            f"TP1   : <b>{tp1:.2f}</b>\n"
            f"{tp2_line}"
            f"{tp3_line}"
            f"Units : {units} oz\n"
            f"Risk  : ${risk_usd:.2f}\n\n"
            f"<i>Reasoning: {reasoning[:300]}</i>"
        )
        return self.send(msg)

    def send_trade_exit(self, direction, entry, exit_price, pl_usd, outcome):
        emoji = "✅" if pl_usd >= 0 else "❌"
        msg = (
            f"{emoji} <b>XAUUSD {direction} CLOSED</b>\n"
            f"Entry  : {entry:.2f}\n"
            f"Exit   : {exit_price:.2f}\n"
            f"P&L    : <b>${pl_usd:+.2f}</b>\n"
            f"Outcome: {outcome}"
        )
        return self.send(msg)

    def send_alert(self, message: str):
        return self.send(f"<b>ALERT</b>\n{message}")

    def send_daily_summary(self, trades_taken, wins, losses, total_pl, account_nav):
        win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        msg = (
            f"<b>Daily Summary</b>\n"
            f"Trades : {trades_taken} | Wins: {wins} | Losses: {losses}\n"
            f"Win rate : {win_rate:.0f}%\n"
            f"Total P&L : <b>${total_pl:+.2f}</b>\n"
            f"Account NAV : ${account_nav:,.2f}"
        )
        return self.send(msg)
