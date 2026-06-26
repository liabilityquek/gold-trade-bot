"""Seed the ExperienceStore from historical Uncle Lim signals.

Reads output/xau_signals_enriched.csv (read-only) and loads each signal with a
verified WIN/LOSS outcome into data/experience_store.json as 'historical'
records. Idempotent: re-running clears prior historical records first and keeps
any live records.

Uses the Oanda-VERIFIED column `price_outcome` (not the channel-claimed
`outcome`, which is optimistically ~90% wins). Mapping:  'tp' -> WIN,
'sl' -> LOSS,  'unknown'/other -> skip. This reconciles to the documented
verified 54.8% win-rate (BUY 88.9% / SELL 4.7%).

Historical signals carry no Uncle Lim setup_type or indicator snapshot, so
setup_type is NONE; recall backoff (dir+hour / dir) still uses them.

    python -u seed_experience_store.py
"""

import csv
import os
import sys

from src.learning.experience_store import get_experience_store

CSV_PATH = os.path.join("output", "xau_signals_enriched.csv")


def _outcome(label: str):
    low = (label or "").lower()
    if "tp" in low:
        return "WIN"
    if "sl" in low:
        return "LOSS"
    return None


def build_rows(csv_path: str):
    rows = []
    skipped = 0
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            outcome = _outcome(r.get("price_outcome", ""))
            if outcome is None:
                skipped += 1
                continue
            direction = (r.get("direction") or "").upper().strip()
            if direction not in ("BUY", "SELL"):
                skipped += 1
                continue
            try:
                hour = int(r.get("utc_hour")) if r.get("utc_hour") else None
            except ValueError:
                hour = None
            try:
                rr = float(r.get("rr_ratio")) if r.get("rr_ratio") else None
            except ValueError:
                rr = None
            try:
                hold = float(r.get("hours_to_outcome")) if r.get("hours_to_outcome") else None
            except ValueError:
                hold = None
            rows.append({
                "trade_id": r.get("id"),
                "ts": r.get("date"),
                "direction": direction,
                "setup_type": "NONE",
                "hour": hour,
                "weekday": r.get("utc_weekday"),
                "rr": rr,
                "outcome": outcome,
                "hold_hours": hold,
            })
    return rows, skipped


def main():
    if not os.path.exists(CSV_PATH):
        print(f"ERROR: {CSV_PATH} not found")
        return 1

    rows, skipped = build_rows(CSV_PATH)
    store = get_experience_store()
    removed = store.clear_historical()
    added = store.bulk_add_closed(rows)

    s = store.stats()
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    print(f"Seeded experience store from {CSV_PATH}")
    print(f"  cleared prior historical: {removed}")
    print(f"  added: {added}  (wins {wins}, losses {added - wins})  skipped: {skipped}")
    print(f"  store totals: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
