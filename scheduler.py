"""
scheduler.py — Run the agent every hour during trading hours.

Trading hours (UTC): 22:00 Sun – 21:00 Fri (gold market hours)
Agent runs at the start of each hour.

Run: python scheduler.py
Stop: Ctrl+C
"""

import time
import schedule
from datetime import datetime, timezone

from agent import run_agent
from telegram_client import TelegramClient


def is_market_open():
    now = datetime.now(timezone.utc)
    # Gold trades Sun 22:00 – Fri 21:00 UTC
    weekday = now.weekday()  # 0=Mon, 6=Sun
    hour    = now.hour
    if weekday == 5:   # Saturday — closed all day
        return False
    if weekday == 6 and hour < 22:   # Sunday before 22:00 UTC
        return False
    if weekday == 4 and hour >= 21:  # Friday after 21:00 UTC
        return False
    return True


def run_if_market_open():
    if is_market_open():
        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] Market open — running agent ...")
        run_agent()
    else:
        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] Market closed — skipping")


def main():
    tg = TelegramClient()
    tg.send_alert("Agent scheduler started. Running every hour during market hours.")
    print("Scheduler started. Agent will run every hour. Press Ctrl+C to stop.\n")

    schedule.every().hour.at(":00").do(run_if_market_open)

    # Run immediately on start
    run_if_market_open()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
