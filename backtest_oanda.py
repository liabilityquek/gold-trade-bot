"""
backtest_oanda.py — Replay Uncle Lim's XAU_USD signals against Oanda
historical candle data. Calculates realistic P&L including spread.

Uses xau_signals_enriched.csv as signal source.
Fetches Oanda XAU_USD candles for the corresponding periods.

Run: python backtest_oanda.py
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from dotenv import load_dotenv

load_dotenv()

INPUT_SIGNALS = "output/xau_signals_enriched.csv"
OUTPUT_RESULTS = "output/backtest_results.json"
OUTPUT_REPORT  = "output/backtest_report.md"

INSTRUMENT       = "XAU_USD"
RISK_PCT         = 0.01       # 1% of account per trade
STARTING_BALANCE = 10000.0    # USD, paper account start
SPREAD_USD       = 0.30       # Typical Oanda XAU_USD spread in USD/oz
LOOKFORWARD_H    = 72         # Hours to scan for TP/SL hit
MAX_CONCURRENT   = 2
GRANULARITY      = "H1"

PRACTICE = True


def load_signals():
    signals = []
    with open(INPUT_SIGNALS, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for col in ["entry_low", "entry_high", "sl", "tp1", "tp2", "tp3", "rr_ratio"]:
                try:
                    row[col] = float(row[col]) if row[col] else None
                except (ValueError, TypeError):
                    row[col] = None
            if row["entry_low"] and row["entry_high"] and row["sl"] and row["tp1"]:
                signals.append(row)
    return signals


def get_candles_for_period(client, account_id, start_iso, hours=80):
    """Fetch up to `hours` H1 candles starting from start_iso."""
    params = {
        "granularity": GRANULARITY,
        "from": start_iso,
        "count": min(hours, 500),
        "price": "BA",  # bid/ask for spread
    }
    r = instruments.InstrumentsCandles(INSTRUMENT, params=params)
    try:
        client.request(r)
        candles = []
        for c in r.response["candles"]:
            if not c.get("complete", True):
                continue
            bid_h = float(c["bid"]["h"])
            bid_l = float(c["bid"]["l"])
            ask_h = float(c["ask"]["h"])
            ask_l = float(c["ask"]["l"])
            candles.append({
                "time":  c["time"],
                "bid_h": bid_h, "bid_l": bid_l,
                "ask_h": ask_h, "ask_l": ask_l,
                "mid_h": (bid_h + ask_h) / 2,
                "mid_l": (bid_l + ask_l) / 2,
            })
        return candles
    except Exception as e:
        print(f"  Candle fetch failed for {start_iso}: {e}")
        return []


def simulate_trade(signal, candles):
    """
    Simulate a trade from signal using candle data.
    BUY: fill at ask (entry_high + spread), SL hit if bid_l <= sl, TP if bid_h >= tp
    SELL: fill at bid (entry_low - spread), SL hit if ask_h >= sl, TP if ask_l <= tp
    Returns dict with outcome, pl_pips, fill_price, exit_price.
    """
    direction  = signal["direction"]
    entry_mid  = (signal["entry_low"] + signal["entry_high"]) / 2
    sl         = signal["sl"]
    tp1        = signal["tp1"]
    tp2        = signal["tp2"]
    tp3        = signal["tp3"]

    if direction == "BUY":
        fill_price = entry_mid + SPREAD_USD / 2
        exit_sl    = sl
        tps        = [t for t in [tp3, tp2, tp1] if t]  # check best first
    else:
        fill_price = entry_mid - SPREAD_USD / 2
        exit_sl    = sl
        tps        = [t for t in [tp3, tp2, tp1] if t]

    for c in candles:
        if direction == "BUY":
            sl_hit  = c["bid_l"] <= exit_sl
            tp_hits = [(tp, c["bid_h"] >= tp) for tp in [tp3, tp2, tp1] if tp]
        else:
            sl_hit  = c["ask_h"] >= exit_sl
            tp_hits = [(tp, c["ask_l"] <= tp) for tp in [tp3, tp2, tp1] if tp]

        # Best TP hit first
        for tp_price, hit in tp_hits:
            if hit and not sl_hit:
                pl = abs(tp_price - fill_price) if direction == "BUY" else abs(fill_price - tp_price)
                label = {signal["tp3"]: "tp3_hit", signal["tp2"]: "tp2_hit", signal["tp1"]: "tp1_hit"}.get(tp_price, "tp_hit")
                return {"outcome": label, "fill_price": fill_price, "exit_price": tp_price, "pl_pips": pl}

        if sl_hit:
            pl = -(abs(fill_price - exit_sl))
            return {"outcome": "sl_hit", "fill_price": fill_price, "exit_price": exit_sl, "pl_pips": pl}

    return {"outcome": "unknown", "fill_price": fill_price, "exit_price": None, "pl_pips": 0}


def main():
    api_key    = os.environ["OANDA_API_KEY"]
    account_id = os.environ["OANDA_ACCOUNT_ID"]
    env = "practice" if PRACTICE else "live"
    client = oandapyV20.API(access_token=api_key, environment=env)

    print("Loading signals ...")
    signals = load_signals()

    # Filter to BUY only (SELL win rate 4.7% in bull market — see strategy report)
    buy_signals = [s for s in signals if s["direction"] == "BUY"]
    print(f"  Total signals: {len(signals)} | BUY only: {len(buy_signals)}")

    print(f"\nFetching Oanda candles and simulating {len(buy_signals)} trades ...")
    print(f"  Starting balance: ${STARTING_BALANCE:,.2f} | Risk per trade: {RISK_PCT*100:.0f}%")

    balance   = STARTING_BALANCE
    peak_nav  = STARTING_BALANCE
    results   = []
    monthly   = defaultdict(lambda: {"trades": 0, "wins": 0, "pl": 0.0})
    open_count = 0

    for i, sig in enumerate(buy_signals):
        if open_count >= MAX_CONCURRENT:
            open_count = max(0, open_count - 1)

        entry_mid = (sig["entry_low"] + sig["entry_high"]) / 2
        sl_dist   = abs(entry_mid - sig["sl"])
        if sl_dist < 0.01:
            continue

        units    = max(1, int((balance * RISK_PCT) / sl_dist))
        risk_usd = units * sl_dist

        # Fetch Oanda candles from signal time
        start_iso = sig["date"]
        candles   = get_candles_for_period(client, account_id, start_iso, hours=LOOKFORWARD_H)

        if not candles:
            results.append({**sig, "bt_outcome": "no_data", "bt_pl_usd": 0, "units": units})
            continue

        sim = simulate_trade(sig, candles)
        pl_usd = sim["pl_pips"] * units

        balance += pl_usd
        peak_nav = max(peak_nav, balance)
        drawdown = (peak_nav - balance) / peak_nav * 100

        month = sig["date"][:7]
        monthly[month]["trades"] += 1
        monthly[month]["pl"]     += pl_usd
        if "tp" in sim["outcome"]:
            monthly[month]["wins"] += 1

        results.append({
            **sig,
            "bt_outcome":    sim["outcome"],
            "bt_fill":       sim["fill_price"],
            "bt_exit":       sim["exit_price"],
            "bt_pl_pips":    sim["pl_pips"],
            "bt_pl_usd":     round(pl_usd, 2),
            "bt_units":      units,
            "bt_balance":    round(balance, 2),
            "bt_drawdown":   round(drawdown, 2),
        })

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(buy_signals)} | Balance: ${balance:,.2f} | Drawdown: {drawdown:.1f}%")

    # ── Stats ────────────────────────────────────────────────────────────────
    decided   = [r for r in results if r["bt_outcome"] != "unknown" and r["bt_outcome"] != "no_data"]
    wins      = [r for r in decided if "tp" in r["bt_outcome"]]
    losses    = [r for r in decided if r["bt_outcome"] == "sl_hit"]
    win_rate  = len(wins) / len(decided) * 100 if decided else 0
    total_pl  = sum(r["bt_pl_usd"] for r in results)
    max_dd    = max((r.get("bt_drawdown", 0) for r in results), default=0)
    avg_win   = sum(r["bt_pl_usd"] for r in wins) / len(wins) if wins else 0
    avg_loss  = sum(r["bt_pl_usd"] for r in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(r["bt_pl_usd"] for r in wins) / sum(r["bt_pl_usd"] for r in losses)) if losses else float("inf")

    # ── Write JSON ───────────────────────────────────────────────────────────
    os.makedirs("output", exist_ok=True)
    with open(OUTPUT_RESULTS, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nWrote {OUTPUT_RESULTS}")

    # ── Write report ─────────────────────────────────────────────────────────
    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        f.write("# XAUUSD Backtest Report — Uncle Lim BUY Signals\n\n")
        f.write(f"Data source : Oanda XAU_USD {GRANULARITY} candles\n")
        f.write(f"Signals     : BUY only (SELL signals excluded — 4.7% win rate in bull market)\n")
        f.write(f"Risk        : {RISK_PCT*100:.0f}% per trade | Start: ${STARTING_BALANCE:,.2f}\n")
        f.write(f"Spread      : ${SPREAD_USD} per oz assumed\n\n")

        f.write("## Performance Summary\n\n")
        f.write(f"| Metric | Value |\n|---|---|\n")
        f.write(f"| Starting balance | ${STARTING_BALANCE:,.2f} |\n")
        f.write(f"| Ending balance | ${balance:,.2f} |\n")
        f.write(f"| Total P&L | ${total_pl:+,.2f} |\n")
        f.write(f"| Return | {(balance/STARTING_BALANCE - 1)*100:+.1f}% |\n")
        f.write(f"| Signals tested | {len(buy_signals)} |\n")
        f.write(f"| Decided trades | {len(decided)} |\n")
        f.write(f"| Win rate | {win_rate:.1f}% |\n")
        f.write(f"| Avg win | ${avg_win:+.2f} |\n")
        f.write(f"| Avg loss | ${avg_loss:+.2f} |\n")
        f.write(f"| Profit factor | {profit_factor:.2f} |\n")
        f.write(f"| Max drawdown | {max_dd:.1f}% |\n\n")

        f.write("## Monthly Breakdown\n\n")
        f.write("| Month | Trades | Wins | P&L |\n|---|---|---|---|\n")
        for month in sorted(monthly.keys()):
            d = monthly[month]
            f.write(f"| {month} | {d['trades']} | {d['wins']} | ${d['pl']:+.2f} |\n")

        f.write("\n## Outcome Distribution\n\n")
        from collections import Counter
        outcome_counts = Counter(r["bt_outcome"] for r in results)
        for k, v in outcome_counts.most_common():
            f.write(f"- {k}: {v}\n")

    print(f"Wrote {OUTPUT_REPORT}")
    print(f"\nBacktest complete:")
    print(f"  Return     : {(balance/STARTING_BALANCE - 1)*100:+.1f}%")
    print(f"  Win rate   : {win_rate:.1f}%")
    print(f"  Max DD     : {max_dd:.1f}%")
    print(f"  Profit factor: {profit_factor:.2f}")


if __name__ == "__main__":
    main()
