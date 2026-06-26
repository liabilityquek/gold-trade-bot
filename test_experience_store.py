"""Assert-based self-check for the shadow-mode ExperienceStore.

Run: python -u test_experience_store.py
No framework — fails loud on the first broken invariant.
"""

import os
import tempfile

from src.learning.experience_store import ExperienceStore


def _fresh_store():
    tmp = os.path.join(tempfile.gettempdir(), "exp_store_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    return ExperienceStore(path=tmp), tmp


def test_backoff_levels():
    st, tmp = _fresh_store()
    rows = []
    for i in range(10):
        rows.append({"trade_id": f"w{i}", "direction": "BUY", "setup_type": "SND_ZONE",
                     "hour": 5, "outcome": "WIN"})
    for i in range(10):
        rows.append({"trade_id": f"l{i}", "direction": "BUY", "setup_type": "SND_ZONE",
                     "hour": 5, "outcome": "LOSS"})
    assert st.bulk_add_closed(rows) == 20

    # most-specific level matches
    p = st.recall_prior("BUY", "SND_ZONE", 5)
    assert p["level"] == "dir+setup+hour" and p["n"] == 20
    assert abs(p["win_rate"] - 0.5) < 1e-9

    # far hour (circular dist 11 > window) backs off below the hour level
    p2 = st.recall_prior("BUY", "SND_ZONE", 18)
    assert p2["level"] in ("dir+setup", "dir"), p2

    # unknown direction -> nothing
    assert st.recall_prior("SELL", "SND_ZONE", 5)["n"] == 0
    os.remove(tmp)


def test_entry_outcome_roundtrip():
    st, tmp = _fresh_store()
    st.record_entry("T1", "SELL", "LCT", indicators={"adx": 22.0, "rsi": 61.0},
                    confidence=0.7, rr=1.8)
    # OPEN record should not count as closed
    assert st.stats()["open"] == 1 and st.stats()["closed"] == 0

    st.record_outcome("T1", pnl=-3.2, close_reason="stop_loss", hold_hours=4.0)
    rec = next(r for r in st._closed() if r["trade_id"] == "T1")
    assert rec["outcome"] == "LOSS" and rec["verdict"] == "STOP_HIT"
    assert rec["confidence"] == 0.7 and rec["adx"] == 22.0

    # take_profit win classifies as TARGET_HIT
    st.record_entry("T2", "BUY", "SND_ZONE")
    st.record_outcome("T2", pnl=12.5, close_reason="take_profit", hold_hours=2.0)
    rec2 = next(r for r in st._closed() if r["trade_id"] == "T2")
    assert rec2["outcome"] == "WIN" and rec2["verdict"] == "TARGET_HIT", rec2

    # outcome with no prior entry -> minimal closed record appended
    st.record_outcome("ORPHAN", pnl=5.0, close_reason="user")
    assert any(r["trade_id"] == "ORPHAN" for r in st._closed())
    os.remove(tmp)


def test_reflection_gating():
    st, tmp = _fresh_store()
    # below LEARNING_REFLECTION_MIN_TRADES (default 20) -> no rules
    st.bulk_add_closed([{"trade_id": f"x{i}", "direction": "BUY",
                         "setup_type": "SND_ZONE", "hour": 5,
                         "outcome": "WIN" if i % 2 else "LOSS"} for i in range(10)])
    assert st.reflection_rules() == []

    # cross the threshold -> a rule appears for the group with enough samples
    st.bulk_add_closed([{"trade_id": f"y{i}", "direction": "BUY",
                         "setup_type": "SND_ZONE", "hour": 5,
                         "outcome": "WIN"} for i in range(15)])
    rules = st.reflection_rules()
    assert any("BUY/SND_ZONE" in r for r in rules), rules
    os.remove(tmp)


def test_disabled_prompt_block(monkeypatch_enabled=None):
    st, tmp = _fresh_store()
    st.bulk_add_closed([{"trade_id": f"z{i}", "direction": "BUY",
                         "setup_type": "SND_ZONE", "hour": 5,
                         "outcome": "WIN"} for i in range(25)])
    block = st.prompt_block("BUY", "SND_ZONE", 5)
    assert "observational only" in block and "BUY/SND_ZONE" in block, block
    os.remove(tmp)


if __name__ == "__main__":
    test_backoff_levels()
    test_entry_outcome_roundtrip()
    test_reflection_gating()
    test_disabled_prompt_block()
    print("ALL experience_store tests passed")
