"""
agent.py — Uncle Lim XAUUSD LLM Trading Agent.

Each run:
  1. Fetches multi-timeframe candles from Oanda (H4, H1, M30, M15)
  2. Asks Claude to analyse market structure using Uncle Lim's methodology
  3. If a valid setup exists, Claude calls place_order() tool
  4. Risk manager sizes the position (1% NAV)
  5. Order is executed on Oanda practice account
  6. Telegram notification sent immediately after

Run manually:   python agent.py
Run on schedule: use Windows Task Scheduler or cron (see README)
"""

import json
import os
import sys
import traceback
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

from oanda_client import OandaClient
from risk_manager import calculate_units, format_risk_summary, within_risk_limits
from telegram_client import TelegramClient

load_dotenv()

MAX_CONCURRENT_TRADES = 2
MODEL                 = "claude-sonnet-4-6"  # latest capable model
LOG_FILE              = "output/agent_log.jsonl"

# ── System prompt: Uncle Lim's strategy framework ────────────────────────────
SYSTEM_PROMPT = """You are a XAUUSD (Gold) trading analyst trained on Uncle Lim's multi-timeframe confluence strategy.

## Uncle Lim's Strategy Framework

**Top-down analysis — work from higher to lower timeframe:**
1. H4: Identify primary trend (uptrend / downtrend) and major Support/Resistance levels
2. H1: Identify trendline, breakout zones, and SND (Supply & Demand) areas
3. M30/M15: Confirm entry zone with LCT (pullback to breakout level) or Bearish/Bullish Engulfing
4. M5/M1: Final confirmation — Secret Pattern or SND zone at exact entry

**Entry checklist (must have at least 3 confirmations across timeframes):**
- H4 structure: Support/Resistance or trend context
- H1 trigger: Trendline Breakout, SND zone, or RTB (Return to Breakout)
- M30 or M15: LCT, Engulfing candle, or SND
- M5 or M1: Secret Pattern or final SND zone

**Signal format when entering:**
- Provide entry zone (low-high, e.g. 3285.50-3283.50)
- SL: at least 2 points below the zone for BUY, above for SELL
- TP1: first resistance/support level
- TP2: second target
- TP3: extended target

**Risk rules:**
- Maximum 2 concurrent trades
- Only enter if confirmations align across at least 3 timeframes
- Avoid trading within 30 min of major news events
- BUY bias in uptrend; SELL bias only with strong H4 confirmation

**Current market bias guidance:**
- Gold is in a long-term bull market (2024-2026). Prefer BUY setups unless H4 shows clear breakdown.
- Uncle Lim's SELL signals have a historically low win rate in this trend.
- Do not force a trade. If the setup is not clear, use no_trade().

## Your Job
Analyse the provided candle data across timeframes. Identify whether a valid Uncle Lim setup exists.
If yes, call place_order() with your reasoning. If not, call no_trade() explaining why.
Be concise and precise. State which confirmations you found and on which timeframe.
"""

# ── Tool definitions ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_candles",
        "description": "Fetch recent OHLCV candles for XAU_USD at a given timeframe. Call this for H4, H1, M30, M15 to build your analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "granularity": {
                    "type": "string",
                    "enum": ["H4", "H1", "M30", "M15", "M5"],
                    "description": "Timeframe to fetch",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of candles to fetch (default 50)",
                    "default": 50,
                },
            },
            "required": ["granularity"],
        },
    },
    {
        "name": "get_account_info",
        "description": "Get current account balance, NAV, and number of open trades.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "place_order",
        "description": "Execute a XAUUSD market order with SL and TP levels. Only call this when you have high-confidence multi-timeframe confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                    "description": "Trade direction",
                },
                "entry_zone_low":  {"type": "number", "description": "Lower bound of entry zone"},
                "entry_zone_high": {"type": "number", "description": "Upper bound of entry zone"},
                "sl":  {"type": "number", "description": "Stop loss price"},
                "tp1": {"type": "number", "description": "Take profit 1 (closest)"},
                "tp2": {"type": "number", "description": "Take profit 2 (optional)"},
                "tp3": {"type": "number", "description": "Take profit 3 (extended, optional)"},
                "reasoning": {
                    "type": "string",
                    "description": "Concise explanation: timeframe confirmations found, why entry is valid",
                },
                "confirmations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of confirmations found, e.g. ['Support H4', 'SND H1', 'LCT M30', 'Bullish Engulfing M15']",
                },
            },
            "required": ["direction", "entry_zone_low", "entry_zone_high", "sl", "tp1", "reasoning"],
        },
    },
    {
        "name": "no_trade",
        "description": "Explicitly decide not to trade this cycle. Call this when there is no valid setup or when risk limits are reached.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why no trade is being taken",
                },
            },
            "required": ["reason"],
        },
    },
]


# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_get_candles(oanda: OandaClient, granularity: str, count: int = 50) -> str:
    try:
        candles = oanda.get_candles(granularity=granularity, count=count)
        if not candles:
            return f"No candles returned for {granularity}"
        # Format compactly: time | O | H | L | C
        lines = [f"{granularity} candles (latest {len(candles)}):"]
        for c in candles[-30:]:  # last 30 to keep context manageable
            t = c["time"][:16].replace("T", " ")
            lines.append(f"{t} O:{c['open']:.2f} H:{c['high']:.2f} L:{c['low']:.2f} C:{c['close']:.2f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching {granularity} candles: {e}"


def handle_get_account_info(oanda: OandaClient, open_trades: list) -> str:
    try:
        acct = oanda.get_account()
        return (
            f"Balance: ${float(acct['balance']):,.2f}\n"
            f"NAV: ${float(acct['NAV']):,.2f}\n"
            f"Open trades: {len(open_trades)}/{MAX_CONCURRENT_TRADES}\n"
            f"Unrealized P&L: ${float(acct.get('unrealizedPL', 0)):+.2f}"
        )
    except Exception as e:
        return f"Error fetching account info: {e}"


def handle_place_order(oanda: OandaClient, tg: TelegramClient,
                       tool_input: dict, open_trades: list) -> str:
    direction      = tool_input["direction"]
    entry_zone_low = tool_input["entry_zone_low"]
    entry_zone_high= tool_input["entry_zone_high"]
    sl             = tool_input["sl"]
    tp1            = tool_input["tp1"]
    tp2            = tool_input.get("tp2")
    tp3            = tool_input.get("tp3")
    reasoning      = tool_input.get("reasoning", "")
    confirmations  = tool_input.get("confirmations", [])

    # Risk limits
    ok, msg = within_risk_limits(open_trades, daily_loss_pct=0.0)
    if not ok:
        return f"ORDER REJECTED: {msg}"

    nav        = oanda.get_nav()
    entry_mid  = (entry_zone_low + entry_zone_high) / 2
    sl_dist    = abs(entry_mid - sl)

    if sl_dist < 0.5:
        return f"ORDER REJECTED: SL distance too tight ({sl_dist:.2f} pts) — minimum 0.5"

    try:
        units    = calculate_units(nav, entry_mid, sl, risk_pct=0.01)
        risk_usd = units * sl_dist
    except ValueError as e:
        return f"ORDER REJECTED: {e}"

    try:
        result = oanda.place_market_order(
            direction=direction,
            units=units,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            tp3_price=tp3,
            comment=f"agent | {'; '.join(confirmations[:3])}",
        )
    except Exception as e:
        err = str(e)
        tg.send_alert(f"Order execution failed: {err}")
        return f"ORDER FAILED: {err}"

    fill = result.get("fill_price") or entry_mid
    summary = format_risk_summary(nav, fill, sl, units, direction, tp1, tp2, tp3)

    tg.send_trade_entry(
        direction=direction,
        entry=fill,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        units=units,
        risk_usd=risk_usd,
        reasoning=f"{'; '.join(confirmations)} | {reasoning}",
    )

    return (
        f"ORDER PLACED\n"
        f"Trade ID: {result.get('trade_id')}\n"
        f"Fill price: {fill:.2f}\n"
        f"{summary}"
    )


# ── Main agent loop ───────────────────────────────────────────────────────────

def run_agent():
    oanda = OandaClient()
    tg    = TelegramClient()
    ai    = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[{now}] Agent cycle starting ...")

    open_trades = oanda.get_open_trades()
    current_price = oanda.get_current_price()
    print(f"  Open trades: {len(open_trades)} | XAU/USD: {current_price}")

    if len(open_trades) >= MAX_CONCURRENT_TRADES:
        print(f"  Max trades reached ({MAX_CONCURRENT_TRADES}), skipping cycle")
        return

    # Initial user message to trigger analysis
    messages = [
        {
            "role": "user",
            "content": (
                f"Current time: {now}\n"
                f"Current XAU/USD price: {current_price}\n"
                f"Open trades: {len(open_trades)}/{MAX_CONCURRENT_TRADES}\n\n"
                "Please analyse the current XAUUSD market using Uncle Lim's methodology. "
                "Start by fetching H4 candles to determine the primary trend, then H1 for structure, "
                "then M30 and M15 for entry confirmation. "
                "If a valid setup exists, place an order. If not, call no_trade() with your reasoning."
            ),
        }
    ]

    trade_placed = False
    no_trade_reason = None

    # Agentic loop — Claude calls tools until it places an order or calls no_trade
    for iteration in range(10):  # safety cap
        response = ai.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant response
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name  = block.name
            tool_input = block.input
            tool_id    = block.id

            print(f"  Tool: {tool_name} {json.dumps(tool_input)[:100]}")

            if tool_name == "get_candles":
                result_text = handle_get_candles(
                    oanda,
                    granularity=tool_input.get("granularity", "H1"),
                    count=tool_input.get("count", 50),
                )

            elif tool_name == "get_account_info":
                result_text = handle_get_account_info(oanda, open_trades)

            elif tool_name == "place_order":
                result_text = handle_place_order(oanda, tg, tool_input, open_trades)
                trade_placed = True
                print(f"  {result_text[:120]}")

            elif tool_name == "no_trade":
                no_trade_reason = tool_input.get("reason", "No reason given")
                result_text = f"Acknowledged: no trade this cycle. Reason: {no_trade_reason}"
                print(f"  No trade: {no_trade_reason[:100]}")

            else:
                result_text = f"Unknown tool: {tool_name}"

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tool_id,
                "content":     result_text,
            })

        messages.append({"role": "user", "content": tool_results})

        # Stop if final decision made
        if trade_placed or no_trade_reason:
            break

    # ── Log this cycle ────────────────────────────────────────────────────────
    os.makedirs("output", exist_ok=True)
    log_entry = {
        "timestamp":       now,
        "price":           current_price,
        "open_trades":     len(open_trades),
        "trade_placed":    trade_placed,
        "no_trade_reason": no_trade_reason,
        "iterations":      iteration + 1,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    status = "TRADE PLACED" if trade_placed else f"NO TRADE: {no_trade_reason}"
    print(f"  Cycle complete: {status}")


def main():
    try:
        run_agent()
    except KeyboardInterrupt:
        print("\nAgent stopped by user")
    except Exception as e:
        tb = traceback.format_exc()
        print(f"AGENT ERROR: {e}\n{tb}")
        try:
            tg = TelegramClient()
            tg.send_alert(f"Agent crashed: {e}\n\n{tb[:500]}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
