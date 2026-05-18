"""
extract_goldmapping.py — Extract Uncle Lim's gold market analysis posts.
Captures #goldmapping, #analysisbyuncle (gold), GOLD TARGET, and
pre/post-session gold commentary. Saves as goldmapping_corpus.jsonl
and goldmapping_corpus.txt for human review.
"""

import json
import re
import os
from datetime import datetime

INPUT_FILE = "output/messages.json"
OUTPUT_JSONL = "output/goldmapping_corpus.jsonl"
OUTPUT_TXT   = "output/goldmapping_corpus.txt"
OUTPUT_STATS = "output/goldmapping_stats.md"

# Tags and patterns that mark a gold analysis post
GOLD_MAP_RE = re.compile(
    r"(#goldmapping|GOLD TARGET|gold.*mapping|mapping.*gold|XAUUSD.*mapping|mapping.*XAUUSD)",
    re.IGNORECASE,
)

# Uncle's analysis posts (broader — check if GOLD appears in text)
ANALYSIS_RE = re.compile(r"#analysisbyuncle", re.IGNORECASE)
GOLD_RE      = re.compile(r"\bGOLD\b|\bXAUUSD\b", re.IGNORECASE)

# Signals to exclude (we already have those)
SIGNAL_RE = re.compile(r"⚜️\s*XAUUSD\s+(BUY|SELL)", re.IGNORECASE)

# Strategy concept keywords for scoring
CONCEPTS = [
    "support", "resistance", "supply", "demand", "SND", "trendline", "trend line",
    "breakout", "breakout zone", "RTB", "LCT", "secret pattern", "retracement",
    "uptrend", "downtrend", "H1", "H4", "M15", "M30", "D1",
    "rejection", "reversal", "consolidat", "liquidity", "order block",
    "Fibonacci", "fib", "channel", "wedge", "flag", "pivot",
    "kill zone", "London", "New York", "NY", "Asian", "session",
    "DXY", "dollar", "NFP", "CPI", "Fed", "FOMC", "rate",
    "engulfing", "confirmation", "bias", "bearish", "bullish",
]


def concept_score(text):
    lower = text.lower()
    return sum(1 for c in CONCEPTS if c.lower() in lower)


def is_gold_analysis(text):
    if not text:
        return False
    if SIGNAL_RE.search(text):
        return False
    if GOLD_MAP_RE.search(text):
        return True
    if ANALYSIS_RE.search(text) and GOLD_RE.search(text):
        return True
    return False


def main():
    os.makedirs("output", exist_ok=True)

    print("Loading messages.json …")
    with open(INPUT_FILE, encoding="utf-8") as f:
        messages = json.load(f)
    print(f"  {len(messages):,} messages loaded")

    # Pass 1: identify gold analysis posts
    gold_posts = []
    for msg in messages:
        text = msg.get("text", "") or ""
        if is_gold_analysis(text):
            dt = datetime.fromisoformat(msg["date"].replace("Z", "+00:00"))
            gold_posts.append({
                "id":          msg["id"],
                "date":        msg["date"],
                "utc_hour":    dt.hour,
                "utc_weekday": dt.strftime("%A"),
                "month":       msg["date"][:7],
                "text":        text,
                "has_image":   bool(msg.get("image_path")),
                "image_path":  msg.get("image_path"),
                "concept_score": concept_score(text),
                "is_goldmapping": bool(GOLD_MAP_RE.search(text)),
                "char_count":  len(text),
            })

    print(f"  {len(gold_posts):,} gold analysis posts found")

    # Pass 2: for each post, attach the next gold signal (if any within 10 messages)
    msg_by_id = {m["id"]: (i, m) for i, m in enumerate(messages)}

    for post in gold_posts:
        pos, _ = msg_by_id[post["id"]]
        linked_signal = None
        for j in range(pos + 1, min(len(messages), pos + 15)):
            t = messages[j].get("text", "") or ""
            if SIGNAL_RE.search(t):
                linked_signal = messages[j]["id"]
                break
        post["linked_signal_id"] = linked_signal

    # ── Write JSONL ───────────────────────────────────────────────────────────
    with open(OUTPUT_JSONL, "w", encoding="utf-8") as f:
        for post in gold_posts:
            f.write(json.dumps(post, ensure_ascii=False) + "\n")
    print(f"Wrote {OUTPUT_JSONL}")

    # ── Write human-readable TXT ─────────────────────────────────────────────
    sorted_posts = sorted(gold_posts, key=lambda x: -x["concept_score"])
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("UNCLE LIM — GOLD ANALYSIS POSTS\n")
        f.write(f"Total: {len(gold_posts)} posts | Sorted by concept density\n")
        f.write("=" * 80 + "\n\n")
        for p in sorted_posts:
            f.write(f"[{p['date'][:16]}] ID:{p['id']} | Score:{p['concept_score']} | {'#goldmapping' if p['is_goldmapping'] else 'analysis'}\n")
            f.write(p["text"] + "\n")
            if p["linked_signal_id"]:
                f.write(f"  → Followed by signal ID: {p['linked_signal_id']}\n")
            f.write("-" * 60 + "\n\n")
    print(f"Wrote {OUTPUT_TXT}")

    # ── Stats ─────────────────────────────────────────────────────────────────
    from collections import Counter, defaultdict
    month_counts = Counter(p["month"] for p in gold_posts)
    hour_counts  = Counter(p["utc_hour"] for p in gold_posts)
    linked_count = sum(1 for p in gold_posts if p["linked_signal_id"])
    avg_score    = sum(p["concept_score"] for p in gold_posts) / len(gold_posts) if gold_posts else 0
    top_concepts = Counter()
    for p in gold_posts:
        text = p["text"].lower()
        for c in CONCEPTS:
            if c.lower() in text:
                top_concepts[c] += 1

    # Sample 5 richest posts
    richest = sorted(gold_posts, key=lambda x: -x["concept_score"])[:5]

    with open(OUTPUT_STATS, "w", encoding="utf-8") as f:
        f.write("# Uncle Lim — Gold Analysis Post Stats\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")

        f.write("## Overview\n\n")
        f.write(f"- Total gold analysis posts: {len(gold_posts):,}\n")
        f.write(f"- #goldmapping tagged: {sum(1 for p in gold_posts if p['is_goldmapping']):,}\n")
        f.write(f"- Posts with image: {sum(1 for p in gold_posts if p['has_image']):,}\n")
        f.write(f"- Posts linked to a signal (within 15 msgs): {linked_count:,} ({linked_count/len(gold_posts)*100:.1f}%)\n")
        f.write(f"- Avg concept density score: {avg_score:.1f}\n\n")

        f.write("## Monthly Distribution\n\n")
        f.write("| Month | Posts |\n|---|---|\n")
        for month in sorted(month_counts.keys()):
            f.write(f"| {month} | {month_counts[month]} |\n")
        f.write("\n")

        f.write("## Top Signal Hours (UTC)\n\n")
        for hour, n in sorted(hour_counts.items(), key=lambda x: -x[1])[:6]:
            f.write(f"- {hour:02d}:00 UTC — {n} posts\n")
        f.write("\n")

        f.write("## Top Strategy Concepts Mentioned\n\n")
        for concept, n in top_concepts.most_common(20):
            f.write(f"- `{concept}`: {n} posts\n")
        f.write("\n")

        f.write("## Top 5 Richest Analysis Posts\n\n")
        for p in richest:
            f.write(f"### [{p['date'][:16]}] Score: {p['concept_score']}\n\n")
            f.write(f"```\n{p['text']}\n```\n\n")

        f.write("## Training Data Recommendation\n\n")
        f.write(
            "Use `goldmapping_corpus.jsonl` as a **pre-signal context source** "
            "for training pairs where `linked_signal_id` is not null.\n"
            "Structure: (goldmapping analysis text) → (signal that followed).\n"
            "This gives the LLM the reasoning chain: *market structure read* → *entry decision*.\n\n"
            "Prioritise posts with `concept_score >= 4` for highest-quality context.\n"
        )

    print(f"Wrote {OUTPUT_STATS}")
    print("\nSummary:")
    print(f"  Gold analysis posts:     {len(gold_posts):,}")
    print(f"  Linked to a signal:      {linked_count:,} ({linked_count/len(gold_posts)*100:.1f}%)")
    print(f"  Avg concept score:       {avg_score:.1f}")
    print(f"  Top concept:             {top_concepts.most_common(1)[0] if top_concepts else 'none'}")


if __name__ == "__main__":
    main()
