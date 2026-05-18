"""
test_connections.py — Verify Oanda, Telegram, and Anthropic API connections.
Run this after adding credentials to .env before starting the agent.

python test_connections.py
"""

import os
from dotenv import load_dotenv

load_dotenv()

PASS = "[PASS]"
FAIL = "[FAIL]"


def test_oanda():
    try:
        from oanda_client import OandaClient
        c      = OandaClient()
        balance = c.get_balance()
        price   = c.get_current_price()
        candles = c.get_candles(granularity="H1", count=5)
        print(f"{PASS} Oanda  | Balance: ${balance:,.2f} | XAU/USD: {price:.2f} | Candles: {len(candles)}")
        return True
    except Exception as e:
        print(f"{FAIL} Oanda  | {e}")
        return False


def test_telegram():
    try:
        from telegram_client import TelegramClient
        tg = TelegramClient()
        ok = tg.send("Test message from Gold Trading Agent — connections verified.")
        if ok:
            print(f"{PASS} Telegram | Message sent to chat_id {os.environ.get('TELEGRAM_CHAT_ID', '???')}")
        else:
            print(f"{FAIL} Telegram | send() returned False — check token and chat_id")
        return ok
    except Exception as e:
        print(f"{FAIL} Telegram | {e}")
        return False


def test_anthropic():
    try:
        import anthropic
        client   = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": "Reply with: OK"}],
        )
        text = response.content[0].text.strip()
        print(f"{PASS} Anthropic | Model response: {text}")
        return True
    except Exception as e:
        print(f"{FAIL} Anthropic | {e}")
        return False


def main():
    print("Testing connections ...\n")
    results = {
        "Oanda":     test_oanda(),
        "Telegram":  test_telegram(),
        "Anthropic": test_anthropic(),
    }
    print(f"\n{'='*40}")
    all_ok = all(results.values())
    if all_ok:
        print("All connections OK. Ready to run agent.py or backtest_oanda.py")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"Failed: {', '.join(failed)}")
        print("Fix the .env entries and re-run test_connections.py")


if __name__ == "__main__":
    main()
