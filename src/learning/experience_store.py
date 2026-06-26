"""ExperienceStore — shadow-mode trade memory for the gold bot.

Stores every trade entry + outcome in a JSON file and answers two questions
for the analyst prompt (observational only):

  1. recall_prior(direction, setup, hour) -> historical win-rate for a similar
     setup, via backoff: dir+setup+hour-window -> dir+setup -> dir+hour-window
     -> dir -> none.
  2. reflection_rules() -> win-rate lines grouped by (direction, setup).

Design notes:
  - stdlib only (no embeddings / chromadb) — this runs inside a live trading
    loop, heavy deps are not worth it. ponytail: JSON + backoff, swap for a
    vector store only if recall quality measurably falls short.
  - thread-safe: the main loop records entries while the monitoring thread
    records outcomes. One in-memory list guarded by a lock; atomic file writes.
  - never raises from public methods called by the live loop — log + return a
    safe default instead.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config.settings import settings

logger = logging.getLogger("ExperienceStore")

_DEFAULT_PATH = os.path.join("data", "experience_store.json")


def _norm_dir(direction) -> str:
    """Normalize a direction (Signal enum or str) to 'BUY'/'SELL'/'HOLD'."""
    val = getattr(direction, "value", direction)
    return str(val).upper().strip()


def _norm_setup(setup_type) -> str:
    if not setup_type:
        return "NONE"
    return str(setup_type).upper().strip()


def _hour_dist(a: int, b: int) -> int:
    """Circular distance between two hours-of-day."""
    d = abs(int(a) - int(b))
    return min(d, 24 - d)


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class ExperienceStore:
    def __init__(self, path: str = _DEFAULT_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._records: List[Dict] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> List[Dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
            logger.warning("experience store not a list; starting empty")
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"could not load experience store ({exc}); starting empty")
        return []

    def _save_locked(self) -> None:
        """Atomic write. Caller must hold self._lock."""
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._records, fh, ensure_ascii=True, indent=0)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning(f"could not save experience store: {exc}")

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_entry(
        self,
        trade_id,
        direction,
        setup_type,
        indicators: Optional[Dict] = None,
        confidence: Optional[float] = None,
        rr: Optional[float] = None,
        ts: Optional[datetime] = None,
    ) -> None:
        """Append an OPEN record at trade entry. Never raises."""
        try:
            ind = indicators or {}
            now = ts or datetime.now(timezone.utc)
            rec = {
                "trade_id": str(trade_id),
                "ts": now.isoformat(),
                "source": "live",
                "direction": _norm_dir(direction),
                "setup_type": _norm_setup(setup_type),
                "hour": now.hour,
                "weekday": now.strftime("%A"),
                "rr": _safe_float(rr),
                "confidence": _safe_float(confidence),
                "adx": _safe_float(ind.get("adx")),
                "rsi": _safe_float(ind.get("rsi")),
                "h4_bias": ind.get("uncle_lim_h4_bias"),
                "confluences": ind.get("uncle_lim_confluences"),
                "outcome": "OPEN",
                "pnl": None,
                "close_reason": None,
                "hold_hours": None,
                "verdict": None,
                "lesson": None,
            }
            with self._lock:
                self._records.append(rec)
                self._save_locked()
        except Exception as exc:  # never break the live loop
            logger.warning(f"record_entry failed: {exc}")

    def record_outcome(
        self,
        trade_id,
        pnl: float,
        close_reason: str = "",
        hold_hours: Optional[float] = None,
    ) -> None:
        """Fill the matching OPEN record with its outcome. Never raises."""
        try:
            tid = str(trade_id)
            pnl_f = _safe_float(pnl, 0.0) or 0.0
            outcome = "WIN" if pnl_f > 0 else "LOSS"
            verdict = self._classify_verdict(pnl_f, close_reason)
            with self._lock:
                rec = next(
                    (r for r in reversed(self._records)
                     if r.get("trade_id") == tid and r.get("outcome") == "OPEN"),
                    None,
                )
                if rec is None:
                    # no entry record (e.g. restart) — append minimal closed row
                    rec = {
                        "trade_id": tid,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "source": "live",
                        "direction": "",
                        "setup_type": "NONE",
                        "hour": datetime.now(timezone.utc).hour,
                        "weekday": datetime.now(timezone.utc).strftime("%A"),
                    }
                    self._records.append(rec)
                rec["outcome"] = outcome
                rec["pnl"] = round(pnl_f, 4)
                rec["close_reason"] = close_reason or ""
                rec["hold_hours"] = _safe_float(hold_hours)
                rec["verdict"] = verdict
                rec["lesson"] = self._lesson(rec, verdict)
                self._save_locked()
        except Exception as exc:
            logger.warning(f"record_outcome failed: {exc}")

    def bulk_add_closed(self, rows: List[Dict]) -> int:
        """Seed historical CLOSED records in one save. Returns count added."""
        added = 0
        with self._lock:
            for row in rows:
                try:
                    direction = _norm_dir(row.get("direction", ""))
                    outcome = str(row.get("outcome", "")).upper()
                    if outcome not in ("WIN", "LOSS"):
                        continue
                    self._records.append({
                        "trade_id": row.get("trade_id"),
                        "ts": row.get("ts"),
                        "source": "historical",
                        "direction": direction,
                        "setup_type": _norm_setup(row.get("setup_type")),
                        "hour": int(row.get("hour")) if row.get("hour") is not None else None,
                        "weekday": row.get("weekday"),
                        "rr": _safe_float(row.get("rr")),
                        "confidence": None,
                        "adx": None,
                        "rsi": None,
                        "h4_bias": None,
                        "confluences": None,
                        "outcome": outcome,
                        "pnl": None,
                        "close_reason": "historical",
                        "hold_hours": _safe_float(row.get("hold_hours")),
                        "verdict": outcome,
                        "lesson": None,
                    })
                    added += 1
                except Exception:
                    continue
            self._save_locked()
        return added

    def clear_historical(self) -> int:
        """Drop seeded historical records (keeps live ones). For re-seeding."""
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r.get("source") != "historical"]
            removed = before - len(self._records)
            self._save_locked()
        return removed

    # ------------------------------------------------------------------
    # Classification (deterministic)
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_verdict(pnl: float, close_reason: str) -> str:
        reason = (close_reason or "").lower()
        if pnl > 0:
            if "tp" in reason or "target" in reason or "profit" in reason:
                return "TARGET_HIT"
            return "WIN_OTHER"
        if "sl" in reason or "stop" in reason:
            return "STOP_HIT"
        if "news" in reason:
            return "NEWS_CUT"
        return "LOSS_OTHER"

    @staticmethod
    def _lesson(rec: Dict, verdict: str) -> str:
        hold = rec.get("hold_hours")
        hold_str = f"{hold:.1f}h" if isinstance(hold, (int, float)) else "?h"
        pnl = rec.get("pnl")
        pnl_str = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "?"
        return (
            f"{rec.get('direction', '?')} {rec.get('setup_type', 'NONE')} "
            f"-> {verdict.lower()} after {hold_str} (pnl {pnl_str})"
        )

    # ------------------------------------------------------------------
    # Recall + reflection
    # ------------------------------------------------------------------

    def _closed(self) -> List[Dict]:
        return [r for r in self._records if r.get("outcome") in ("WIN", "LOSS")]

    @staticmethod
    def _winrate(rows: List[Dict]) -> float:
        if not rows:
            return 0.0
        wins = sum(1 for r in rows if r.get("outcome") == "WIN")
        return wins / len(rows)

    def recall_prior(self, direction, setup_type, hour: int) -> Dict:
        """Backoff recall over CLOSED records. Never raises."""
        empty = {"n": 0, "win_rate": 0.0, "level": "none", "text": ""}
        try:
            d = _norm_dir(direction)
            s = _norm_setup(setup_type)
            h = int(hour)
            window = settings.LEARNING_RECALL_HOUR_WINDOW
            min_n = settings.LEARNING_MIN_SAMPLE
            closed = self._closed()
            if not closed:
                return empty

            def in_window(r):
                rh = r.get("hour")
                return rh is not None and _hour_dist(rh, h) <= window

            levels = [
                ("dir+setup+hour",
                 lambda r: r.get("direction") == d and r.get("setup_type") == s and in_window(r)),
                ("dir+setup",
                 lambda r: r.get("direction") == d and r.get("setup_type") == s),
                ("dir+hour",
                 lambda r: r.get("direction") == d and in_window(r)),
                ("dir",
                 lambda r: r.get("direction") == d),
            ]
            for level, pred in levels:
                rows = [r for r in closed if pred(r)]
                if len(rows) >= min_n:
                    wr = self._winrate(rows)
                    text = (
                        f"Historical prior for {d}/{s} near {h:02d}:00 UTC: "
                        f"{len(rows)} closed, {wr*100:.0f}% win-rate "
                        f"(basis: {level})."
                    )
                    return {"n": len(rows), "win_rate": round(wr, 4),
                            "level": level, "text": text}
            return empty
        except Exception as exc:
            logger.debug(f"recall_prior failed: {exc}")
            return empty

    def reflection_rules(self) -> List[str]:
        """Win-rate rule lines by (direction, setup). Never raises."""
        try:
            closed = self._closed()
            if len(closed) < settings.LEARNING_REFLECTION_MIN_TRADES:
                return []
            groups: Dict[tuple, List[Dict]] = {}
            for r in closed:
                key = (r.get("direction", "?"), r.get("setup_type", "NONE"))
                groups.setdefault(key, []).append(r)

            scored = []
            for (d, s), rows in groups.items():
                if len(rows) < settings.LEARNING_MIN_SAMPLE:
                    continue
                wr = self._winrate(rows)
                scored.append((abs(wr - 0.5), d, s, wr, len(rows)))
            scored.sort(reverse=True)  # strongest edge first

            rules = [
                f"{d}/{s}: {wr*100:.0f}% win-rate over {n} trades"
                for _, d, s, wr, n in scored[: settings.LEARNING_REFLECTION_MAX_RULES]
            ]
            return rules
        except Exception as exc:
            logger.debug(f"reflection_rules failed: {exc}")
            return []

    def prompt_block(self, direction, setup_type, hour: int) -> str:
        """Observational learning block for the analyst prompt. '' if nothing."""
        if not settings.LEARNING_ENABLED:
            return ""
        try:
            prior = self.recall_prior(direction, setup_type, hour)
            rules = self.reflection_rules()
            if prior["n"] == 0 and not rules:
                return ""
            lines = [
                "[LEARNING - observational only; do NOT let this override your "
                "live analysis]",
            ]
            if prior["n"] > 0:
                lines.append(prior["text"])
            if rules:
                lines.append("Reflection rules (win-rate by direction/setup):")
                lines.extend(f"  - {r}" for r in rules)
            return "\n".join(lines)
        except Exception as exc:
            logger.debug(f"prompt_block failed: {exc}")
            return ""

    def stats(self) -> Dict:
        with self._lock:
            total = len(self._records)
            closed = self._closed()
        wins = sum(1 for r in closed if r.get("outcome") == "WIN")
        return {
            "total": total,
            "closed": len(closed),
            "open": total - len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(self._winrate(closed), 4),
        }


# ----------------------------------------------------------------------
# Module-level singleton (mirrors goldmapping cache pattern in llm_agent)
# ----------------------------------------------------------------------

_store: Optional[ExperienceStore] = None
_store_lock = threading.Lock()


def get_experience_store() -> ExperienceStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ExperienceStore()
    return _store


if __name__ == "__main__":
    # ponytail: self-check on a temp file, asserts the backoff + reflection math.
    import tempfile

    tmp = os.path.join(tempfile.gettempdir(), "exp_store_selfcheck.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    st = ExperienceStore(path=tmp)

    # seed 10 BUY/SND wins + 10 BUY/SND losses at hour 5 -> 50% win-rate
    rows = []
    for i in range(10):
        rows.append({"trade_id": f"w{i}", "direction": "BUY", "setup_type": "SND_ZONE",
                     "hour": 5, "outcome": "WIN", "rr": 1.5})
    for i in range(10):
        rows.append({"trade_id": f"l{i}", "direction": "BUY", "setup_type": "SND_ZONE",
                     "hour": 5, "outcome": "LOSS", "rr": 1.5})
    added = st.bulk_add_closed(rows)
    assert added == 20, added

    prior = st.recall_prior("BUY", "SND_ZONE", 5)
    assert prior["n"] == 20, prior
    assert abs(prior["win_rate"] - 0.5) < 1e-9, prior
    assert prior["level"] == "dir+setup+hour", prior

    # hour far away (5 vs 18 -> circular dist 11 > window) should drop to dir+setup
    prior2 = st.recall_prior("BUY", "SND_ZONE", 18)
    assert prior2["level"] in ("dir+setup", "dir"), prior2

    # live entry then outcome round-trip
    st.record_entry("T1", "SELL", "LCT", indicators={"adx": 22.0, "rsi": 61.0})
    st.record_outcome("T1", pnl=-3.2, close_reason="sl_hit", hold_hours=4.0)
    closed = st._closed()
    t1 = next(r for r in closed if r["trade_id"] == "T1")
    assert t1["outcome"] == "LOSS" and t1["verdict"] == "STOP_HIT", t1

    rules = st.reflection_rules()
    assert any("BUY/SND_ZONE" in r for r in rules), rules

    s = st.stats()
    assert s["closed"] == 21 and s["wins"] == 10, s

    os.remove(tmp)
    print("experience_store self-check OK:", s)
