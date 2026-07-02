"""Self-check for the hardened Uncle Lim detectors + confluence dedup.

Run: python test_uncle_lim_detectors.py   (no pytest, no fixtures)

The 12 UNCLE_LIM_* env vars have NO code default, so they MUST be set before
config.settings is imported — hence the os.environ block at the very top.
"""

import os
import sys

# --- required env (no code defaults) — set BEFORE importing settings/agent ---
os.environ.setdefault('UNCLE_LIM_SND_ATR_MULT', '1.0')
os.environ.setdefault('UNCLE_LIM_BREAKOUT_ATR_MULT', '0.75')
os.environ.setdefault('UNCLE_LIM_LCT_ATR_MULT', '1.25')
os.environ.setdefault('UNCLE_LIM_DEDUP_ATR_MULT', '1.0')
os.environ.setdefault('UNCLE_LIM_SND_PCT_FLOOR', '0.003')
os.environ.setdefault('UNCLE_LIM_BREAKOUT_PCT_FLOOR', '0.002')
os.environ.setdefault('UNCLE_LIM_LCT_PCT_FLOOR', '0.004')
os.environ.setdefault('UNCLE_LIM_H4_MIN_ADX', '20.0')
os.environ.setdefault('UNCLE_LIM_DOJI_BODY_PCT', '0.0005')
os.environ.setdefault('UNCLE_LIM_PIN_BODY_RATIO', '0.4')
os.environ.setdefault('UNCLE_LIM_PIN_WICK_MULT', '2.0')
os.environ.setdefault('UNCLE_LIM_REACTION_WICK_MULT', '1.0')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.agents.base import Signal                    # noqa: E402
from src.agents.uncle_lim_agent import UncLeLimAgent  # noqa: E402


def c(o, h, l, cl):
    return {'open': o, 'high': h, 'low': l, 'close': cl}


def test_tf_tolerance():
    ag = UncLeLimAgent()
    price = 3300.0
    pct = 0.003
    floor = pct * price  # 9.9
    # ATR ~30 across 20 candles -> ATR path dominates the floor.
    big = [c(3300, 3315, 3285, 3300) for _ in range(20)]
    tol = ag._tf_tolerance(big, price, pct, 1.0)
    assert tol > floor + 1, f"ATR path should exceed floor: tol={tol} floor={floor}"
    # <19 candles -> ATR None -> exactly the pct floor.
    thin = [c(3300, 3301, 3299, 3300) for _ in range(10)]
    tol2 = ag._tf_tolerance(thin, price, pct, 1.0)
    assert abs(tol2 - floor) < 1e-6, f"thin-TF tol should equal floor: {tol2} vs {floor}"


def test_snd_reaction():
    ag = UncLeLimAgent()
    price = 3291.0
    tol = 5.0
    # History carries a swing low at 3290 (indices 0..5, excludes last 2).
    hist = [
        c(3298, 3300, 3290, 3299),  # swing low 3290
        c(3299, 3301, 3296, 3300),
        c(3300, 3302, 3297, 3301),
        c(3301, 3303, 3298, 3302),
        c(3300, 3302, 3297, 3301),
        c(3299, 3301, 3296, 3300),
    ]
    # No bounce: last candle closes DOWN with no lower wick at the level.
    no_bounce = hist + [
        c(3300, 3301, 3296, 3300),                 # penultimate (ignored by reaction)
        c(3293.0, 3293.2, 3290.4, 3290.6),         # bearish, tiny lower wick
    ]
    ok, lvl = ag._detect_snd_zone(no_bounce, price, True, tol=tol)
    assert ok is False, f"near level but no rejection should be False (lvl={lvl})"
    # Bounce: last candle is a hammer rejecting off 3290 and closing up.
    bounce = hist + [
        c(3300, 3301, 3296, 3300),
        c(3291.5, 3292.0, 3290.2, 3291.8),         # long lower wick, close up
    ]
    ok2, lvl2 = ag._detect_snd_zone(bounce, price, True, tol=tol)
    assert ok2 is True and abs(lvl2 - 3290) < 1.0, f"rejection wick should match SND: ok={ok2} lvl={lvl2}"


def test_breakout_requires_close():
    ag = UncLeLimAgent()
    tol = 2.0
    # pivot_high = max(highs[:-3]) = 3300 (indices 0..3).
    base = [
        c(3295, 3300, 3294, 3298),  # pivot high 3300
        c(3296, 3299, 3295, 3297),
        c(3296, 3298, 3295, 3297),
        c(3296, 3298, 3295, 3297),
        c(3296, 3299, 3295, 3298),
        c(3297, 3300, 3296, 3299),
    ]
    # Live price above pivot, but last CLOSE below -> no breakout.
    below = base + [c(3299, 3306, 3298, 3299.0)]
    ok, lvl = ag._detect_trendline_breakout(below, price=3305.0, is_long=True, tol=tol)
    assert ok is False, f"wick/live-price spike must not count as breakout (lvl={lvl})"
    # Last close above pivot + tol -> breakout confirmed.
    above = base + [c(3300, 3307, 3299, 3305.0)]
    ok2, lvl2 = ag._detect_trendline_breakout(above, price=3305.0, is_long=True, tol=tol)
    assert ok2 is True and abs(lvl2 - 3300) < 1e-6, f"close above pivot should confirm: ok={ok2} lvl={lvl2}"


def test_doji_fails_closed():
    ag = UncLeLimAgent()
    # prev is a doji (body 0.5 < 3300*0.0005 = 1.65) -> reject even if "engulfed".
    doji_prev = c(3300.0, 3300.6, 3299.4, 3300.5)
    curr = c(3298.0, 3307.0, 3297.0, 3306.0)
    assert ag._detect_engulfing([doji_prev, curr], True, ref_price=3300.0) is False, \
        "doji previous body must fail closed"
    # Real engulfing (prev body 7) -> True.
    real_prev = c(3305.0, 3305.5, 3297.5, 3298.0)   # bearish body ~7
    real_curr = c(3297.0, 3306.5, 3296.5, 3306.0)   # bullish engulf
    assert ag._detect_engulfing([real_prev, real_curr], True, ref_price=3300.0) is True, \
        "valid engulfing should pass"


def test_dedup():
    ag = UncLeLimAgent()
    tol = 5.0
    near = ag._dedup_confirmations([("a", 3300.0), ("b", 3300.5)], tol)
    assert len(near) == 1, f"near-identical levels collapse to 1: {near}"
    far = ag._dedup_confirmations([("a", 3300.0), ("b", 3330.0)], tol)
    assert len(far) == 2, f"far levels stay separate: {far}"
    mixed = ag._dedup_confirmations([("h4", None), ("a", 3300.0), ("b", 3300.2)], tol)
    assert mixed == ["h4", "a"], f"None kept, near-dup dropped: {mixed}"


def _series(builder, n):
    return [builder(i) for i in range(n)]


def test_h4_quality():
    ag = UncLeLimAgent()
    # STRONG: monotonic uptrend -> ema20>ema50, high ADX.
    def strong(i):
        cl = 3000.0 + i * 2.0
        return c(cl - 2.0, cl + 1.0, cl - 1.0, cl)
    bias, is_strong = ag._get_h4_bias(_series(strong, 60))
    assert bias == "bullish" and is_strong is True, f"strong trend -> counted: {bias},{is_strong}"

    # WEAK: closes drift up slowly (ema20>ema50) but wicks whipsaw -> low ADX.
    def weak(i):
        cl = 3000.0 + i * 0.5
        if i % 2 == 0:
            return c(cl - 0.2, cl + 40.0, cl - 1.0, cl)
        return c(cl - 0.2, cl + 1.0, cl - 40.0, cl)
    bias_w, is_strong_w = ag._get_h4_bias(_series(weak, 60))
    assert bias_w == "bullish", f"weak series should still set bullish bias: {bias_w}"
    assert is_strong_w is False, "choppy/low-ADX trend must NOT count as strong"


def test_smoke_garbage():
    ag = UncLeLimAgent()
    assert ag.analyze("XAU_USD", [], {}, 0.0).signal == Signal.HOLD
    junk_candles = [{'garbage': 1} for _ in range(30)]
    junk_htf = {'H4': [{'x': 0}], 'M30': None, 'M15': [1, 2], 'M5': 'nope'}
    vote = ag.analyze("XAU_USD", junk_candles, junk_htf, 0.0)
    assert vote.signal == Signal.HOLD, f"garbage input must yield HOLD, got {vote.signal}"


def main():
    tests = [
        test_tf_tolerance,
        test_snd_reaction,
        test_breakout_requires_close,
        test_doji_fails_closed,
        test_dedup,
        test_h4_quality,
        test_smoke_garbage,
    ]
    for t in tests:
        t()
        print(f"PASS  {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    main()
