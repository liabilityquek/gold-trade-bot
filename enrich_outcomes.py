"""
enrich_outcomes.py — Verify XAUUSD signal outcomes against actual price data.
Fetches GC=F (gold futures) OHLCV at 1h resolution via yfinance.
For each signal: finds whether TP1/2/3 or SL was hit first within 48h.
Writes output/xau_signals_enriched.csv and rebuilds training_data.jsonl.
"""

import json
import csv
import os
import re
from datetime import datetime, timedelta, timezone

import yfinance as yf
import pandas as pd

INPUT_SIGNALS = "output/xau_signals.csv"
INPUT_MESSAGES = "output/messages.json"
OUTPUT_ENRICHED = "output/xau_signals_enriched.csv"
OUTPUT_TRAINING = "output/training_data_enriched.jsonl"
OUTPUT_REPORT   = "output/enriched_report.md"

SYSTEM_MSG = (
    "You are a gold (XAUUSD) trading signal analyst. "
    "Based on market commentary and context, generate the next XAUUSD trade signal "
    "in Uncle Lim's format: direction, entry zone, SL, and TP1/TP2/TP3 levels."
)

LOOKFORWARD_HOURS = 72


def load_signals():
    signals = []
    with open(INPUT_SIGNALS, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for col in ["entry_low", "entry_high", "sl", "tp1", "tp2", "tp3", "rr_ratio", "pip_risk"]:
                try:
                    row[col] = float(row[col]) if row[col] else None
                except (ValueError, TypeError):
                    row[col] = None
            signals.append(row)
    return signals


def fetch_price_data():
    """
    Fetch GC=F price data. yfinance limits 1h to the last 730 days, so we:
    1. Fetch daily data for the full 2-year range (always available).
    2. Fetch 1h data for the last 730 days and merge (overrides daily where available).
    Returns a DataFrame with Open/High/Low/Close indexed by UTC datetime.
    """
    def _normalise(df):
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index, utc=True)
        return df[["Open", "High", "Low", "Close"]].copy()

    print("Fetching GC=F daily OHLCV (full range) …")
    daily = yf.download("GC=F", start="2024-05-15", end="2026-05-17",
                        interval="1d", auto_adjust=True, progress=False)
    if daily.empty:
        raise RuntimeError("No daily price data returned from yfinance.")
    daily = _normalise(daily)
    print(f"  {len(daily):,} daily candles ({daily.index[0].date()} to {daily.index[-1].date()})")

    print("Fetching GC=F 1h OHLCV (last 730 days) …")
    try:
        hourly = yf.download("GC=F", start="2024-05-21", end="2026-05-17",
                             interval="1h", auto_adjust=True, progress=False)
        if not hourly.empty:
            hourly = _normalise(hourly)
            print(f"  {len(hourly):,} hourly candles ({hourly.index[0]} to {hourly.index[-1]})")
            # Combine: hourly takes precedence for its range; daily fills the gap at the start
            gap_daily = daily[daily.index < hourly.index[0]]
            combined = pd.concat([gap_daily, hourly]).sort_index()
            print(f"  Combined: {len(combined):,} candles")
            return combined
    except Exception as e:
        print(f"  1h fetch failed ({e}), falling back to daily only")

    return daily


def check_outcome(signal, df):
    """
    Returns (price_outcome, hours_to_outcome) where price_outcome is one of:
      tp1_hit, tp2_hit, tp3_hit, sl_hit, unknown
    """
    direction = signal["direction"]
    entry_low  = signal["entry_low"]
    entry_high = signal["entry_high"]
    sl   = signal["sl"]
    tp1  = signal["tp1"]
    tp2  = signal["tp2"]
    tp3  = signal["tp3"]

    if not entry_low or not sl or not tp1:
        return "unknown", None

    try:
        sig_time = datetime.fromisoformat(signal["date"].replace("Z", "+00:00"))
    except Exception:
        return "unknown", None

    end_time = sig_time + timedelta(hours=LOOKFORWARD_HOURS)

    # Slice price data: candles after signal time up to 48h
    window = df[(df.index >= sig_time) & (df.index <= end_time)]
    if window.empty:
        return "unknown", None

    is_buy = direction == "BUY"

    for i, (ts, row) in enumerate(window.iterrows()):
        high = row["High"]
        low  = row["Low"]
        hours = i  # approximate hours elapsed

        if is_buy:
            sl_hit  = low <= sl
            tp3_hit = tp3 and high >= tp3
            tp2_hit = tp2 and high >= tp2
            tp1_hit = high >= tp1
        else:
            sl_hit  = high >= sl
            tp3_hit = tp3 and low <= tp3
            tp2_hit = tp2 and low <= tp2
            tp1_hit = low <= tp1

        # Check highest TP first (best case), then SL
        if tp3_hit and not sl_hit:
            return "tp3_hit", hours
        if tp2_hit and not sl_hit:
            return "tp2_hit", hours
        if tp1_hit and not sl_hit:
            return "tp1_hit", hours
        if sl_hit and not tp1_hit:
            return "sl_hit", hours
        # If both triggered in same candle, unclear — call it sl_hit (conservative)
        if sl_hit and tp1_hit:
            return "sl_hit", hours

    return "unknown", None


def main():
    os.makedirs("output", exist_ok=True)

    signals = load_signals()
    print(f"Loaded {len(signals):,} signals from {INPUT_SIGNALS}")

    df = fetch_price_data()

    print("Verifying outcomes against price data …")
    for i, sig in enumerate(signals):
        outcome, hours = check_outcome(sig, df)
        sig["price_outcome"]       = outcome
        sig["hours_to_outcome"]    = hours
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(signals)} processed …")

    # ── Stats ────────────────────────────────────────────────────────────────
    from collections import Counter
    outcome_counts = Counter(s["price_outcome"] for s in signals)
    decided = [s for s in signals if s["price_outcome"] != "unknown"]
    wins    = [s for s in decided if "tp" in s["price_outcome"]]
    sl_hits = [s for s in decided if s["price_outcome"] == "sl_hit"]
    win_rate = len(wins) / len(decided) * 100 if decided else 0

    # TP distribution among wins
    tp_counts = Counter(s["price_outcome"] for s in wins)

    # Avg hours to outcome
    hours_list = [s["hours_to_outcome"] for s in decided if s["hours_to_outcome"] is not None]
    avg_hours = sum(hours_list) / len(hours_list) if hours_list else 0

    # R:R stats for winners vs losers
    def avg_rr(lst):
        vals = [float(s["rr_ratio"]) for s in lst if s.get("rr_ratio")]
        return sum(vals) / len(vals) if vals else 0

    buy_decided  = [s for s in decided if s["direction"] == "BUY"]
    sell_decided = [s for s in decided if s["direction"] == "SELL"]
    buy_wins     = [s for s in buy_decided if "tp" in s["price_outcome"]]
    sell_wins    = [s for s in sell_decided if "tp" in s["price_outcome"]]

    # Monthly breakdown
    from collections import defaultdict
    monthly = defaultdict(lambda: {"total": 0, "wins": 0, "sl": 0})
    for s in signals:
        month = s["date"][:7]
        monthly[month]["total"] += 1
        if "tp" in s["price_outcome"]:
            monthly[month]["wins"] += 1
        elif s["price_outcome"] == "sl_hit":
            monthly[month]["sl"] += 1

    # ── Write enriched CSV ───────────────────────────────────────────────────
    all_keys = list(signals[0].keys()) + ["price_outcome", "hours_to_outcome"]
    # deduplicate keys keeping order
    seen = set()
    fieldnames = []
    for k in all_keys:
        if k not in seen:
            seen.add(k)
            fieldnames.append(k)

    with open(OUTPUT_ENRICHED, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(signals)
    print(f"Wrote {OUTPUT_ENRICHED} ({len(signals):,} rows)")

    # ── Write enriched training JSONL (verified outcomes only) ──────────────
    quality_signals = [
        s for s in signals
        if s["price_outcome"] != "unknown"
        and s.get("context_before", "").strip()
    ]
    with open(OUTPUT_TRAINING, "w", encoding="utf-8") as f:
        for sig in quality_signals:
            record = {
                "messages": [
                    {"role": "system",    "content": SYSTEM_MSG},
                    {"role": "user",      "content": sig["context_before"] + "\n\nGenerate the next XAUUSD trade signal."},
                    {"role": "assistant", "content": sig["raw_text"]},
                ],
                "metadata": {
                    "id":            sig["id"],
                    "date":          sig["date"],
                    "direction":     sig["direction"],
                    "entry_low":     sig["entry_low"],
                    "entry_high":    sig["entry_high"],
                    "sl":            sig["sl"],
                    "tp1":           sig["tp1"],
                    "rr_ratio":      sig["rr_ratio"],
                    "price_outcome": sig["price_outcome"],
                    "hours_to_outcome": sig["hours_to_outcome"],
                },
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Wrote {OUTPUT_TRAINING} ({len(quality_signals):,} quality pairs)")

    # ── Write enriched report ────────────────────────────────────────────────
    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        f.write("# Uncle Lim XAUUSD — Price-Verified Outcome Report\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Price source: GC=F (gold futures) 1h OHLCV via yfinance\n")
        f.write(f"Lookforward window: {LOOKFORWARD_HOURS}h per signal\n\n")

        f.write("## Verified Outcome Summary\n\n")
        f.write(f"- Total XAU signals: {len(signals):,}\n")
        f.write(f"- Signals with verified outcome: {len(decided):,} ({len(decided)/len(signals)*100:.1f}%)\n")
        f.write(f"- Signals unresolved within {LOOKFORWARD_HOURS}h: {outcome_counts['unknown']:,}\n\n")
        f.write(f"- **Verified win rate:** {win_rate:.1f}%\n")
        f.write(f"- Wins: {len(wins):,} | SL hits: {len(sl_hits):,}\n")
        f.write(f"- Avg hours to outcome: {avg_hours:.1f}h\n\n")

        f.write("## TP Distribution (among wins)\n\n")
        for tp, n in sorted(tp_counts.items()):
            pct = n / len(wins) * 100 if wins else 0
            f.write(f"- {tp}: {n} ({pct:.1f}%)\n")
        f.write("\n")

        f.write("## Direction Breakdown\n\n")
        f.write(f"| Direction | Decided | Wins | Win Rate |\n")
        f.write(f"|---|---|---|---|\n")
        bwr = len(buy_wins)/len(buy_decided)*100 if buy_decided else 0
        swr = len(sell_wins)/len(sell_decided)*100 if sell_decided else 0
        f.write(f"| BUY  | {len(buy_decided)}  | {len(buy_wins)}  | {bwr:.1f}% |\n")
        f.write(f"| SELL | {len(sell_decided)} | {len(sell_wins)} | {swr:.1f}% |\n\n")

        f.write("## Monthly Verified Win Rate\n\n")
        f.write("| Month | Signals | Wins | SL Hits | Win Rate |\n")
        f.write("|---|---|---|---|---|\n")
        for month in sorted(monthly.keys()):
            d = monthly[month]
            dec = d["wins"] + d["sl"]
            wr = d["wins"] / dec * 100 if dec else 0
            f.write(f"| {month} | {d['total']} | {d['wins']} | {d['sl']} | {wr:.0f}% |\n")
        f.write("\n")

        f.write("## Outcome Breakdown\n\n")
        for k, v in outcome_counts.most_common():
            f.write(f"- {k}: {v}\n")
        f.write("\n")

        f.write("## Training Data Quality\n\n")
        f.write(f"- Quality training pairs (verified outcome + context): {len(quality_signals):,}\n")
        f.write(f"- Recommendation: filter to `rr_ratio >= 1.0` winners only for highest-quality fine-tuning signal.\n")

    print(f"Wrote {OUTPUT_REPORT}")

    print("\nSummary:")
    print(f"  Verified win rate:    {win_rate:.1f}%")
    print(f"  Decided outcomes:     {len(decided):,} / {len(signals):,}")
    print(f"  Avg hours to result:  {avg_hours:.1f}h")
    print(f"  Quality training pairs: {len(quality_signals):,}")
    print(f"  Outcome counts:       {dict(outcome_counts)}")


if __name__ == "__main__":
    main()
