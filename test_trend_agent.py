"""Self-check for the H1 trend-following agent.

Run: python test_trend_agent.py   (no pytest, no fixtures)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import settings              # noqa: E402
from src.agents.base import Signal                 # noqa: E402
from src.agents.trend_agent import TrendAgent      # noqa: E402


def c(o, h, l, cl):
    return {'open': o, 'high': h, 'low': l, 'close': cl}


def _ramp(step, n=60, base=3000.0):
    """Linear series: rising if step>0, falling if step<0."""
    out = []
    for i in range(n):
        cl = base + i * step
        out.append(c(cl - step, cl + 1.0, cl - 1.0, cl))
    return out


def test_uptrend_buys():
    ag = TrendAgent()
    v = ag.analyze("XAU_USD", _ramp(2.0), {}, 3120.0)
    assert v.signal == Signal.BUY, f"strong uptrend should BUY, got {v.signal}"
    assert v.setup_type == "TREND"
    assert len(v.meta['confirmations']) == 3


def test_downtrend_sells():
    ag = TrendAgent()
    v = ag.analyze("XAU_USD", _ramp(-2.0), {}, 2880.0)
    assert v.signal == Signal.SELL, f"strong downtrend should SELL, got {v.signal}"


def test_adx_gate_holds():
    ag = TrendAgent()
    saved = settings.TREND_ADX_MIN
    settings.TREND_ADX_MIN = 999.0   # no real ADX clears this
    try:
        v = ag.analyze("XAU_USD", _ramp(2.0), {}, 3120.0)
        assert v.signal == Signal.HOLD, f"ADX gate must block, got {v.signal}"
    finally:
        settings.TREND_ADX_MIN = saved


def test_flat_holds():
    ag = TrendAgent()
    flat = [c(3000.0, 3000.5, 2999.5, 3000.0) for _ in range(60)]
    assert ag.analyze("XAU_USD", flat, {}, 3000.0).signal == Signal.HOLD


def test_thin_holds():
    ag = TrendAgent()
    v = ag.analyze("XAU_USD", _ramp(2.0, n=30), {}, 3060.0)
    assert v.signal == Signal.HOLD, "under ~55 bars must HOLD (EMA50 unavailable)"


def test_garbage_never_raises():
    ag = TrendAgent()
    assert ag.analyze("XAU_USD", [], {}, 0.0).signal == Signal.HOLD
    junk = [{'garbage': 1} for _ in range(60)]
    assert ag.analyze("XAU_USD", junk, None, 0.0).signal == Signal.HOLD
    # get_indicators must also survive junk
    ag.get_indicators("XAU_USD", junk, None, 0.0)


def main():
    tests = [
        test_uptrend_buys,
        test_downtrend_sells,
        test_adx_gate_holds,
        test_flat_holds,
        test_thin_holds,
        test_garbage_never_raises,
    ]
    for t in tests:
        t()
        print(f"PASS  {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    main()
