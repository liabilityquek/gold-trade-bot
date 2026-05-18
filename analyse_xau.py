"""
analyse_xau.py — Uncle Lim XAUUSD Strategy Research + LLM Training Data Generator
Reads output/messages.json (read-only), writes to output/
"""

import json
import re
import csv
import os
from collections import defaultdict, Counter
from datetime import datetime, timezone

INPUT_FILE = "output/messages.json"
OUTPUT_DIR = "output"

# ── Signal regex ──────────────────────────────────────────────────────────────
SIGNAL_RE = re.compile(
    r"⚜️\s*XAUUSD\s+(BUY|SELL)\s+(NOW|LIMIT|STOP)\s*⚜️",
    re.IGNORECASE,
)

PRICE_RE = re.compile(r"(\d{3,5}(?:\.\d{1,2})?)")
SL_RE    = re.compile(r"SL\s*:?\s*(?:~~[\d.]+~~\s*)?(\d{3,5}(?:\.\d{1,2})?)", re.IGNORECASE)
TP_RE    = re.compile(r"TP(\d)\s*:?\s*(?:~~[\d.]+~~\s*)?(\d{3,5}(?:\.\d{1,2})?)", re.IGNORECASE)

# Entry zone: two prices on a line like "2391.50-2389.50"
ENTRY_RE = re.compile(r"(\d{3,5}(?:\.\d{1,2})?)\s*[-–]\s*(\d{3,5}(?:\.\d{1,2})?)")

# ── Strategy keyword sets ─────────────────────────────────────────────────────
INDICATOR_KEYWORDS = [
    "EMA", "MA ", "SMA", "RSI", "MACD", "Bollinger", "stochastic",
    "Fibonacci", "Fib ", "fib ", "support", "resistance", "trendline",
    "trend line", "channel", "pivot", "ATH", "ATL", "DXY", "dollar index",
    "supply", "demand", "order block", "OB", "fair value gap", "FVG",
    "imbalance", "liquidity",
]
PATTERN_KEYWORDS = [
    "head and shoulder", "H&S", "double top", "double bottom",
    "wedge", "flag", "pennant", "triangle", "consolidat", "breakout",
    "break out", "rejection", "reversal", "retracement",
]
SESSION_KEYWORDS = [
    "London", "New York", "NY ", "Asian", "Asia ", "kill zone",
    "killzone", "session open", "market open",
]
FUNDAMENTAL_KEYWORDS = [
    "NFP", "CPI", "Fed", "FOMC", "rate", "inflation", "war",
    "geopolit", "tariff", "sanction", "China", "dollar", "US data",
    "employment", "GDP",
]
OUTCOME_TP_RE  = re.compile(r"TP\s*(\d)", re.IGNORECASE)
OUTCOME_WIN_RE = re.compile(r"(✅|closed|profit|hit|take profit|tp\s*\d)", re.IGNORECASE)
OUTCOME_SL_RE  = re.compile(r"(❌|stopped|stop loss|SL hit|loss)", re.IGNORECASE)


def strip_strikethrough(text):
    """Remove ~~old~~ struck-through text, keep the replacement that follows."""
    return re.sub(r"~~[^~]+~~\s*", "", text)


def parse_signal(msg):
    text = msg.get("text", "") or ""
    m = SIGNAL_RE.search(text)
    if not m:
        return None

    direction  = m.group(1).upper()
    order_type = m.group(2).upper()
    clean      = strip_strikethrough(text)

    # Entry zone — look for "AAAA.BB-CCCC.DD" pattern
    entry_low = entry_high = None
    entry_m = ENTRY_RE.search(clean[m.end():m.end() + 120])
    if entry_m:
        a, b = float(entry_m.group(1)), float(entry_m.group(2))
        entry_low, entry_high = min(a, b), max(a, b)

    # SL
    sl_m = SL_RE.search(clean)
    sl = float(sl_m.group(1)) if sl_m else None

    # TPs
    tps = {}
    for tp_m in TP_RE.finditer(clean):
        tps[int(tp_m.group(1))] = float(tp_m.group(2))

    # R:R ratio (entry_mid → TP1) / (entry_mid → SL)
    rr = None
    pip_risk = None
    if entry_low and entry_high and sl:
        entry_mid = (entry_low + entry_high) / 2
        pip_risk  = abs(entry_mid - sl)
        if 1 in tps and pip_risk > 0:
            rr = round(abs(tps[1] - entry_mid) / pip_risk, 2)

    dt = datetime.fromisoformat(msg["date"].replace("Z", "+00:00"))

    return {
        "id":          msg["id"],
        "date":        msg["date"],
        "utc_hour":    dt.hour,
        "utc_weekday": dt.strftime("%A"),
        "direction":   direction,
        "order_type":  order_type,
        "entry_low":   entry_low,
        "entry_high":  entry_high,
        "sl":          sl,
        "tp1":         tps.get(1),
        "tp2":         tps.get(2),
        "tp3":         tps.get(3),
        "rr_ratio":    rr,
        "pip_risk":    round(pip_risk, 2) if pip_risk else None,
        "has_image":   bool(msg.get("image_path")),
        "image_path":  msg.get("image_path"),
        "raw_text":    text,
        "context_before": "",   # filled later
        "context_after":  "",   # filled later
        "outcome":        "unknown",
    }


def detect_outcome(after_texts):
    for t in after_texts:
        if not t:
            continue
        if OUTCOME_SL_RE.search(t):
            return "sl_hit"
        win_m = OUTCOME_WIN_RE.search(t)
        if win_m:
            tp_m = OUTCOME_TP_RE.search(t)
            if tp_m:
                return f"tp{tp_m.group(1)}_hit"
            return "tp_hit"
    return "unknown"


def keyword_count(texts, keywords):
    combined = " ".join(texts).lower()
    return Counter(
        kw for kw in keywords if kw.lower() in combined
    )


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading messages.json …")
    with open(INPUT_FILE, encoding="utf-8") as f:
        messages = json.load(f)
    print(f"  {len(messages):,} messages loaded")

    # Index by position for context lookup
    idx_by_pos = {i: m for i, m in enumerate(messages)}

    # ── Phase 1: Parse XAU signals ───────────────────────────────────────────
    print("Parsing XAUUSD signals …")
    signals = []
    signal_positions = {}   # msg id → list position

    for pos, msg in enumerate(messages):
        sig = parse_signal(msg)
        if sig:
            signal_positions[msg["id"]] = pos
            signals.append((pos, sig))

    print(f"  {len(signals):,} XAU signals found")

    # ── Phase 2: Context & outcome ───────────────────────────────────────────
    print("Extracting context and outcomes …")
    final_signals = []
    commentary_blocks = []

    for pos, sig in signals:
        # Context before: 3 preceding non-signal messages
        before_texts = []
        for p in range(max(0, pos - 6), pos):
            t = messages[p].get("text", "") or ""
            if t and not SIGNAL_RE.search(t):
                before_texts.append(t)
        before_texts = before_texts[-3:]

        # Context after: 5 following messages
        after_texts = []
        for p in range(pos + 1, min(len(messages), pos + 21)):
            t = messages[p].get("text", "") or ""
            after_texts.append(t)

        sig["context_before"] = "\n".join(before_texts)
        sig["context_after"]  = "\n".join(after_texts[:5])
        sig["outcome"]        = detect_outcome(after_texts[:20])

        final_signals.append(sig)
        if before_texts:
            commentary_blocks.append({
                "signal_id": sig["id"],
                "date":      sig["date"],
                "context":   sig["context_before"],
            })

    # ── Phase 3: Write xau_signals.csv ───────────────────────────────────────
    csv_path = os.path.join(OUTPUT_DIR, "xau_signals.csv")
    fieldnames = [
        "id", "date", "utc_hour", "utc_weekday", "direction", "order_type",
        "entry_low", "entry_high", "sl", "tp1", "tp2", "tp3",
        "rr_ratio", "pip_risk", "has_image", "image_path",
        "outcome", "context_before", "context_after", "raw_text",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(final_signals)
    print(f"  Wrote {csv_path} ({len(final_signals):,} rows)")

    # ── Phase 4: Commentary mining ───────────────────────────────────────────
    print("Mining strategy keywords from commentary …")
    all_context = [b["context"] for b in commentary_blocks]
    indicator_counts  = keyword_count(all_context, INDICATOR_KEYWORDS)
    pattern_counts    = keyword_count(all_context, PATTERN_KEYWORDS)
    session_counts    = keyword_count(all_context, SESSION_KEYWORDS)
    fundamental_counts = keyword_count(all_context, FUNDAMENTAL_KEYWORDS)

    # ── Phase 5: Outcome stats ────────────────────────────────────────────────
    outcome_counts   = Counter(s["outcome"] for s in final_signals)
    direction_counts = Counter(s["direction"] for s in final_signals)
    order_type_counts = Counter(s["order_type"] for s in final_signals)
    rr_values        = [s["rr_ratio"] for s in final_signals if s["rr_ratio"]]
    pip_values       = [s["pip_risk"] for s in final_signals if s["pip_risk"]]
    hour_counts      = Counter(s["utc_hour"] for s in final_signals)
    weekday_counts   = Counter(s["utc_weekday"] for s in final_signals)

    # Monthly breakdown
    monthly = defaultdict(lambda: {"total": 0, "wins": 0, "sl": 0})
    for s in final_signals:
        month = s["date"][:7]
        monthly[month]["total"] += 1
        if "tp" in s["outcome"]:
            monthly[month]["wins"] += 1
        elif s["outcome"] == "sl_hit":
            monthly[month]["sl"] += 1

    # Win rate (excluding unknown)
    decided = [s for s in final_signals if s["outcome"] != "unknown"]
    wins    = [s for s in decided if "tp" in s["outcome"]]
    win_rate = len(wins) / len(decided) * 100 if decided else 0

    # Top R:R
    avg_rr      = sum(rr_values) / len(rr_values) if rr_values else 0
    avg_pip_risk = sum(pip_values) / len(pip_values) if pip_values else 0

    # Richest commentary (most indicator keywords)
    all_non_signal = [
        (msg.get("text", "") or "")
        for msg in messages
        if msg.get("text") and not SIGNAL_RE.search(msg.get("text", ""))
    ]
    all_keywords = INDICATOR_KEYWORDS + PATTERN_KEYWORDS + SESSION_KEYWORDS + FUNDAMENTAL_KEYWORDS
    rich_commentary = sorted(
        [(t, sum(1 for kw in all_keywords if kw.lower() in t.lower())) for t in all_non_signal],
        key=lambda x: x[1], reverse=True
    )[:10]

    # ── Phase 6: Strategy report ─────────────────────────────────────────────
    report_path = os.path.join(OUTPUT_DIR, "strategy_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# Uncle Lim XAUUSD Strategy Research Report\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")

        f.write("## Signal Overview\n\n")
        f.write(f"- **Total XAU signals:** {len(final_signals):,}\n")
        f.write(f"- **BUY signals:** {direction_counts['BUY']:,} ({direction_counts['BUY']/len(final_signals)*100:.1f}%)\n")
        f.write(f"- **SELL signals:** {direction_counts['SELL']:,} ({direction_counts['SELL']/len(final_signals)*100:.1f}%)\n")
        f.write(f"- **Order types:** {dict(order_type_counts)}\n")
        f.write(f"- **Signals with images:** {sum(1 for s in final_signals if s['has_image']):,}\n\n")

        f.write("## Risk Profile\n\n")
        f.write(f"- **Avg R:R ratio (entry→TP1 / entry→SL):** {avg_rr:.2f}\n")
        f.write(f"- **Avg pip risk (entry to SL):** {avg_pip_risk:.1f} pips\n")
        if rr_values:
            f.write(f"- **R:R range:** {min(rr_values):.2f} – {max(rr_values):.2f}\n")
        f.write("\n")

        f.write("## Estimated Win Rate (message-based, unverified)\n\n")
        f.write(f"> **Warning:** Outcome detection is based on keywords in follow-up messages, not actual price data.\n")
        f.write(f"> Use Phase 4 price enrichment for verified statistics.\n\n")
        f.write(f"- Signals with detectable outcome: {len(decided):,} / {len(final_signals):,} ({len(decided)/len(final_signals)*100:.1f}%)\n")
        f.write(f"- **Estimated win rate:** {win_rate:.1f}%\n")
        f.write(f"- Outcome breakdown: {dict(outcome_counts)}\n\n")

        f.write("## Monthly Breakdown\n\n")
        f.write("| Month | Signals | Wins | SL Hits | Win Rate |\n")
        f.write("|---|---|---|---|---|\n")
        for month in sorted(monthly.keys()):
            d = monthly[month]
            dec = d["wins"] + d["sl"]
            wr = d["wins"] / dec * 100 if dec else 0
            f.write(f"| {month} | {d['total']} | {d['wins']} | {d['sl']} | {wr:.0f}% |\n")
        f.write("\n")

        f.write("## Time-of-Day Patterns (UTC hours)\n\n")
        f.write("Top signal hours:\n")
        for hour, count in sorted(hour_counts.items(), key=lambda x: -x[1])[:8]:
            bar = "█" * (count // max(1, max(hour_counts.values()) // 20))
            f.write(f"  {hour:02d}:00  {bar} {count}\n")
        f.write("\nBy weekday:\n")
        for day, count in sorted(weekday_counts.items(), key=lambda x: -x[1]):
            f.write(f"  {day}: {count}\n")
        f.write("\n")

        f.write("## Strategy Keywords Found in Commentary\n\n")
        f.write("### Technical Indicators\n")
        for kw, n in indicator_counts.most_common():
            f.write(f"- `{kw}`: {n} contexts\n")
        f.write("\n### Chart Patterns\n")
        for kw, n in pattern_counts.most_common():
            f.write(f"- `{kw}`: {n} contexts\n")
        f.write("\n### Session References\n")
        for kw, n in session_counts.most_common():
            f.write(f"- `{kw}`: {n} contexts\n")
        f.write("\n### Fundamental Triggers\n")
        for kw, n in fundamental_counts.most_common():
            f.write(f"- `{kw}`: {n} contexts\n")
        f.write("\n")

        f.write("## Top 10 Richest Commentary Samples\n\n")
        for i, (text, score) in enumerate(rich_commentary, 1):
            f.write(f"### Sample {i} (keyword density: {score})\n\n")
            f.write(f"```\n{text[:500]}\n```\n\n")

        f.write("## Next Steps for LLM Training\n\n")
        f.write("1. **Phase 4 (price enrichment):** Fetch XAUUSD OHLCV via `yfinance` and verify each signal outcome against actual price movement within 48h. This replaces approximate message-based outcomes.\n")
        f.write("2. **Image captioning:** Use a vision model (Claude claude-sonnet-4-6) to caption each chart image in `output/images/` and attach the caption to the corresponding signal as structured context.\n")
        f.write("3. **Fine-tuning vs RAG:** With <2,000 signals, RAG (retrieval-augmented generation) over `training_data.jsonl` is faster to iterate than fine-tuning. Fine-tune only after price enrichment gives verified win-rate labels.\n")
        f.write("4. **Signal quality filter:** Filter training data to signals where `outcome != 'unknown'` and `rr_ratio >= 1.5` — these are the highest-confidence, well-structured examples.\n")
        f.write("5. **System prompt engineering:** Build a system prompt encoding Uncle Lim's strategy fingerprint (indicators, R:R profile, session bias) derived from this report.\n")

    print(f"  Wrote {report_path}")

    # ── Phase 7: LLM training data JSONL ────────────────────────────────────
    print("Writing LLM training data …")
    jsonl_path = os.path.join(OUTPUT_DIR, "training_data.jsonl")
    system_msg = (
        "You are a gold (XAUUSD) trading signal analyst. "
        "Based on market commentary and context, generate the next XAUUSD trade signal "
        "in Uncle Lim's format: direction, entry zone, SL, and TP1/TP2/TP3 levels."
    )
    training_count = 0
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for sig in final_signals:
            if not sig["context_before"].strip():
                continue
            record = {
                "messages": [
                    {"role": "system",    "content": system_msg},
                    {"role": "user",      "content": sig["context_before"] + "\n\nGenerate the next XAUUSD trade signal."},
                    {"role": "assistant", "content": sig["raw_text"]},
                ],
                "metadata": {
                    "id":        sig["id"],
                    "date":      sig["date"],
                    "direction": sig["direction"],
                    "entry_low": sig["entry_low"],
                    "entry_high":sig["entry_high"],
                    "sl":        sig["sl"],
                    "tp1":       sig["tp1"],
                    "rr_ratio":  sig["rr_ratio"],
                    "outcome":   sig["outcome"],
                },
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            training_count += 1

    print(f"  Wrote {jsonl_path} ({training_count:,} training pairs)")

    # ── Phase 8: Commentary text dump ────────────────────────────────────────
    commentary_path = os.path.join(OUTPUT_DIR, "xau_commentary.txt")
    with open(commentary_path, "w", encoding="utf-8") as f:
        for b in commentary_blocks:
            f.write(f"=== Signal {b['signal_id']} | {b['date']} ===\n")
            f.write(b["context"] + "\n\n")
    print(f"  Wrote {commentary_path}")

    print("\nDone. Summary:")
    print(f"  XAU signals:       {len(final_signals):,}")
    print(f"  Training pairs:    {training_count:,}")
    print(f"  Decided outcomes:  {len(decided):,} ({len(decided)/len(final_signals)*100:.1f}%)")
    print(f"  Est. win rate:     {win_rate:.1f}%")
    print(f"  Avg R:R:           {avg_rr:.2f}")
    print(f"  Avg pip risk:      {avg_pip_risk:.1f}")


if __name__ == "__main__":
    main()
