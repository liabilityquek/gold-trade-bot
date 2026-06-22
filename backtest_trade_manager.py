"""
backtest_trade_manager.py -- Replay XAU_USD BUY signals through the REAL
TradeManager (break-even / partial-TP / trailing-stop) to A/B old-vs-new
break-even config.

Why this exists: backtest_oanda.py only checks raw SL/TP against candles -- it
never exercises trade_manager's break-even lock, the very thing we changed.
This harness drives the live TradeManager via a candle-backed SimBroker so we
can measure how often trades get shaken out at the break-even lock, old vs new.

  OLD : break-even fixed (arm +10 / lock entry+5), PARTIAL_TP_RR_TARGET=1.0
        (the pre-change behavior that produced the recurring "+5 lock then
         stopped" shake-outs)
  NEW : break-even ATR-scaled (arm 1.5xATR / lock 0.5xATR), PARTIAL_TP_RR_TARGET=1.5
        (shipped behavior)

Both runs use IDENTICAL cached candles and identical position sizes, so the only
moving parts are the break-even numbers and the partial-TP RR target.

Fidelity notes / assumptions (same convention as backtest_oanda.py so results are
comparable):
  - Mid-price OHLC for SL/TP hit detection; spread applied once at entry fill.
  - M5 candles for intrabar path; SL/TP active during a bar are the ones set as
    of the PRIOR bar close (no look-ahead -- you cannot move a stop on a price you
    have not seen yet).
  - Both-hit-in-one-bar resolved pessimistically (SL before TP).
  - ATR(14) computed from H1 candles preceding entry, matching engine._calc_atr.
  - Trades simulated independently (no portfolio concurrency cap); net R is the
    sum of per-trade R, which is position-size independent.

Run: python backtest_trade_manager.py
Requires OANDA_API_KEY + OANDA_ACCOUNT_ID in the environment (.env).
"""

import csv
import hashlib
import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from dotenv import load_dotenv

from config.settings import settings
from src.broker.base import BaseBroker, Trade, OrderSide, TradeCloseResult
from src.execution.trade_manager import TradeManager

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
INPUT_SIGNALS = "output/xau_signals_enriched.csv"
CACHE_DIR     = Path("output/tm_backtest_cache")
OUTPUT_REPORT = "output/backtest_tm_report.md"

INSTRUMENT       = "XAU_USD"
SPREAD_USD       = 0.30      # entry-fill spread, USD/oz (Oanda practice typical)
RISK_PCT         = 0.01      # 1% sizing off a FIXED notional (no compounding,
STARTING_BALANCE = 10000.0   # so both configs get identical unit sizes per trade)
LOOKFORWARD_H    = 72        # hours of forward M5 candles to replay
ATR_LOOKBACK_H   = 40        # hours of H1 candles before entry for ATR(14)
REPLAY_GRAN      = "M5"
ATR_GRAN         = "H1"
PRACTICE         = True

# How many BUY signals to test (caching makes re-runs free; 0 = all).
MAX_SIGNALS = int(os.getenv("TM_BT_MAX", "150"))

# OLD break-even numbers = the .env values that caused the shake-outs.
OLD_BE_ACTIVATION = 10.0
OLD_BE_BUFFER     = 5.0

# Quiet logger so TradeManager's INFO spam doesn't drown the report.
_QUIET = logging.getLogger("tm_backtest")
_QUIET.addHandler(logging.NullHandler())
_QUIET.setLevel(logging.CRITICAL)


# ── SimBroker: candle-backed BaseBroker the real TradeManager drives ──────────
class _SimTrade:
    def __init__(self, trade_id, pair, side, units, entry_price, stop_loss, take_profit, open_time):
        self.trade_id = trade_id
        self.pair = pair
        self.side = side
        self.units0 = units            # initial size (for risk/R)
        self.units = units             # current remaining size
        self.entry_price = entry_price
        self.current_price = entry_price
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.open_time = open_time
        self.open = True
        self.be_sl = None              # first profit-side SL lock (break-even)
        self.partial_done = False      # a partial close happened (break-even armed)
        self.close_sl = None           # SL value at the moment of a stop exit
        self.fills = []                # list of (units, price, kind)

    @property
    def is_long(self):
        return self.side == OrderSide.BUY


class SimBroker(BaseBroker):
    """Holds open trades, mimics Oanda server-side SL/TP, lets TradeManager move stops."""

    def __init__(self, spread=SPREAD_USD):
        self.spread = spread
        self._trades = {}
        self._counter = 0

    # -- harness helpers (not part of BaseBroker) --
    def open_trade(self, pair, side, units, entry_price, stop_loss, take_profit, open_time):
        self._counter += 1
        tid = f"sim{self._counter}"
        self._trades[tid] = _SimTrade(
            tid, pair, side, units, entry_price, stop_loss, take_profit, open_time
        )
        return tid

    def as_base_trade(self, tid):
        t = self._trades[tid]
        return Trade(
            trade_id=t.trade_id, pair=t.pair, side=t.side, units=t.units,
            entry_price=t.entry_price, current_price=t.current_price,
            stop_loss=t.stop_loss, take_profit=t.take_profit, open_time=t.open_time,
        )

    def is_open(self, tid):
        return self._trades[tid].open

    def set_price(self, tid, price):
        self._trades[tid].current_price = price

    def process_bar(self, bar):
        """Mimic server-side stop/TP fills using SL/TP as they stand right now.
        Pessimistic: if both SL and TP fall inside the bar, assume SL first."""
        for t in self._trades.values():
            if not t.open:
                continue
            hi, lo = bar["h"], bar["l"]
            if t.is_long:
                if t.stop_loss is not None and lo <= t.stop_loss:
                    self._close_remaining(t, t.stop_loss, "sl")
                elif t.take_profit is not None and hi >= t.take_profit:
                    self._close_remaining(t, t.take_profit, "tp")
            else:
                if t.stop_loss is not None and hi >= t.stop_loss:
                    self._close_remaining(t, t.stop_loss, "sl")
                elif t.take_profit is not None and lo <= t.take_profit:
                    self._close_remaining(t, t.take_profit, "tp")

    def close_at(self, tid, price, kind):
        t = self._trades[tid]
        if t.open:
            self._close_remaining(t, price, kind)

    def _close_remaining(self, t, price, kind):
        if t.units > 0:
            t.fills.append((t.units, price, kind))
        if kind == "sl":
            t.close_sl = t.stop_loss
        t.units = 0
        t.open = False

    # -- BaseBroker surface actually exercised by TradeManager.update_all_trades --
    def get_open_trades(self):
        return [self.as_base_trade(tid) for tid, t in self._trades.items() if t.open]

    def modify_trade(self, trade_id, pair, stop_loss=None, take_profit=None):
        t = self._trades.get(trade_id)
        if not t or not t.open:
            return False
        if stop_loss is not None:
            profit_side = (stop_loss > t.entry_price) if t.is_long else (stop_loss < t.entry_price)
            if profit_side and t.be_sl is None:
                t.be_sl = stop_loss
            t.stop_loss = stop_loss
        if take_profit is not None:
            t.take_profit = take_profit
        return True

    def partial_close_trade(self, trade_id, units):
        t = self._trades.get(trade_id)
        if not t or not t.open or units < 1:
            return False
        units = min(units, t.units)
        if units < 1:
            return False
        t.fills.append((units, t.current_price, "partial"))
        t.units -= units
        t.partial_done = True
        if t.units <= 0:
            t.open = False
        return True

    def close_trade(self, trade_id):
        t = self._trades.get(trade_id)
        if not t or not t.open:
            return TradeCloseResult(success=False)
        price = t.current_price
        self._close_remaining(t, price, "manual")
        pnl = self._realized(t)
        return TradeCloseResult(success=True, realized_pnl=pnl, close_price=price)

    # -- stubs for the rest of the abstract surface (unused by the replay) --
    def connect(self):
        return True

    def get_account_info(self):
        return None

    def get_current_price(self, pair):
        return None

    def get_positions(self):
        return []

    def get_position(self, pair):
        return None

    def place_market_order(self, pair, side, units, stop_loss=None, take_profit=None):
        return None

    def get_closed_trade_info(self, trade_id):
        return {}

    def close_position(self, pair):
        return True

    # -- result extraction --
    def _realized(self, t):
        pnl = 0.0
        for units, price, _ in t.fills:
            move = (price - t.entry_price) if t.is_long else (t.entry_price - price)
            pnl += move * units
        return pnl

    def summarize(self, tid, sl_distance):
        t = self._trades[tid]
        realized = self._realized(t)
        risk = t.units0 * sl_distance
        R = realized / risk if risk > 0 else 0.0
        final_kind = t.fills[-1][2] if t.fills else "none"

        if final_kind == "tp":
            cls = "tp_win"
        elif final_kind == "sl":
            close_sl = t.close_sl
            profit_side = close_sl is not None and (
                (close_sl >= t.entry_price - 1e-9) if t.is_long
                else (close_sl <= t.entry_price + 1e-9)
            )
            if not profit_side:
                cls = "sl_loss"
            elif t.be_sl is not None and abs(close_sl - t.be_sl) < 1e-6:
                cls = "be_shakeout"      # stopped exactly at the break-even lock
            else:
                cls = "trail_stop"        # trailing had improved the lock
        elif final_kind == "manual":
            cls = "manual"
        else:
            cls = "timeout"

        return {
            "R": R,
            "pnl_usd": realized,
            "cls": cls,
            "partial_done": t.partial_done,
            "be_armed": t.be_sl is not None,
        }


# ── Candle fetch + cache ──────────────────────────────────────────────────────
def _cache_path(gran, from_iso, to_iso):
    key = hashlib.md5(f"{INSTRUMENT}|{gran}|{from_iso}|{to_iso}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{gran}_{key}.json"


def fetch_candles(client, gran, from_iso, to_iso):
    cp = _cache_path(gran, from_iso, to_iso)
    if cp.exists():
        with open(cp, encoding="utf-8") as f:
            return json.load(f)

    params = {"granularity": gran, "from": from_iso, "to": to_iso, "price": "BA"}
    r = instruments.InstrumentsCandles(INSTRUMENT, params=params)
    out = []
    try:
        client.request(r)
        for c in r.response["candles"]:
            if not c.get("complete", True):
                continue
            b, a = c["bid"], c["ask"]
            out.append({
                "t": c["time"],
                "o": (float(b["o"]) + float(a["o"])) / 2,
                "h": (float(b["h"]) + float(a["h"])) / 2,
                "l": (float(b["l"]) + float(a["l"])) / 2,
                "c": (float(b["c"]) + float(a["c"])) / 2,
            })
    except Exception as e:
        print(f"  Candle fetch failed ({gran} {from_iso}): {e}")
        return []

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def calc_atr(candles, period=14):
    """ATR(14) on mid candles -- mirrors engine._calc_atr."""
    if not candles or len(candles) < period + 1:
        return None
    df = pd.DataFrame(candles)
    high, low, close = df["h"], df["l"], df["c"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


# ── Signal loading + preparation ──────────────────────────────────────────────
def load_buy_signals():
    signals = []
    with open(INPUT_SIGNALS, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("direction") != "BUY":
                continue
            for col in ["entry_low", "entry_high", "sl", "tp1", "tp2", "tp3"]:
                try:
                    row[col] = float(row[col]) if row[col] else None
                except (ValueError, TypeError):
                    row[col] = None
            if row["entry_low"] and row["entry_high"] and row["sl"] and row["tp1"]:
                signals.append(row)
    return signals


def prepare(client, signals):
    """Fetch+cache candles, compute fill/SL/TP/units/ATR. Returns list of prepared dicts."""
    prepared = []
    for i, sig in enumerate(signals):
        try:
            entry_dt = datetime.fromisoformat(sig["date"])
        except (ValueError, TypeError):
            continue
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)

        m5_from = entry_dt.isoformat()
        m5_to   = (entry_dt + timedelta(hours=LOOKFORWARD_H)).isoformat()
        h1_from = (entry_dt - timedelta(hours=ATR_LOOKBACK_H)).isoformat()
        h1_to   = entry_dt.isoformat()

        m5 = fetch_candles(client, REPLAY_GRAN, m5_from, m5_to)
        if not m5:
            continue
        h1 = fetch_candles(client, ATR_GRAN, h1_from, h1_to)
        atr = calc_atr(h1)

        entry_mid = (sig["entry_low"] + sig["entry_high"]) / 2
        fill = entry_mid + SPREAD_USD / 2          # BUY fills at ask
        sl_distance = fill - sig["sl"]
        if sl_distance <= 0.01:
            continue

        units = max(1, int((STARTING_BALANCE * RISK_PCT) / sl_distance))
        broker_tp = sig["tp1"] if units < 4 else (sig["tp3"] or sig["tp1"])

        prepared.append({
            "fill": fill, "sl": sig["sl"], "tp1": sig["tp1"],
            "tp2": sig["tp2"], "tp3": sig["tp3"], "broker_tp": broker_tp,
            "units": units, "sl_distance": sl_distance, "atr": atr,
            "open_time": entry_dt, "m5": m5,
        })

        if (i + 1) % 25 == 0:
            print(f"  prepared {i + 1}/{len(signals)} ...")
    return prepared


# ── Simulation ────────────────────────────────────────────────────────────────
def _neutralize_state(tm):
    """Stop the real TradeManager from reading/writing live managed_trades.json."""
    tm._persisted_state = {}
    tm.managed_trades = {}
    tm._save_state = lambda *a, **k: None


def make_tm(sim, mode):
    tm = TradeManager(sim, logger=_QUIET, alert_manager=None)
    _neutralize_state(tm)
    if mode == "OLD":
        tm._break_even_activation = lambda managed: OLD_BE_ACTIVATION
        tm._break_even_buffer = lambda managed: OLD_BE_BUFFER
    return tm


def simulate_one(prep, mode):
    sim = SimBroker(spread=SPREAD_USD)
    tm = make_tm(sim, mode)

    tid = sim.open_trade(
        pair=INSTRUMENT, side=OrderSide.BUY, units=prep["units"],
        entry_price=prep["fill"], stop_loss=prep["sl"],
        take_profit=prep["broker_tp"], open_time=prep["open_time"],
    )
    tm.register_trade(
        sim.as_base_trade(tid), strategy_name="bt", trailing_stop=True,
        trailing_distance=tm.trailing_stop_activation_points,
        tp2=prep["tp2"], tp3=prep["tp3"],
    )
    if prep["atr"]:
        tm.update_trade_atr(tid, prep["atr"])

    for bar in prep["m5"]:
        sim.process_bar(bar)            # stops as of prior-bar close act now
        if not sim.is_open(tid):
            break
        sim.set_price(tid, bar["c"])    # mark to this bar's close
        tm.update_all_trades()          # break-even / partial / trailing manage
        if not sim.is_open(tid):
            break

    if sim.is_open(tid):
        sim.close_at(tid, prep["m5"][-1]["c"], "timeout")

    return sim.summarize(tid, prep["sl_distance"])


def run_config(prepared, mode):
    if mode == "OLD":
        settings.PARTIAL_TP_RR_TARGET = 1.0
    else:
        settings.PARTIAL_TP_RR_TARGET = 1.5
        settings.BREAK_EVEN_ACTIVATION_ATR_MULT = 1.5
        settings.BREAK_EVEN_BUFFER_ATR_MULT = 0.5

    results = [simulate_one(p, mode) for p in prepared]

    n = len(results)
    net_R = sum(r["R"] for r in results)
    wins = [r for r in results if r["R"] > 0]
    losses = [r for r in results if r["R"] <= 0]
    win_rate = len(wins) / n * 100 if n else 0.0
    gross_win = sum(r["R"] for r in wins)
    gross_loss = abs(sum(r["R"] for r in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    cls_counts = Counter(r["cls"] for r in results)
    be_shakeouts = cls_counts.get("be_shakeout", 0)

    return {
        "mode": mode, "n": n, "net_R": net_R, "avg_R": net_R / n if n else 0.0,
        "win_rate": win_rate, "pf": pf, "be_shakeouts": be_shakeouts,
        "cls": cls_counts, "results": results,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────
CLASSES = ["tp_win", "be_shakeout", "trail_stop", "sl_loss", "timeout", "manual"]


def _row(label, old_v, new_v):
    return f"| {label} | {old_v} | {new_v} |"


def print_and_write(old, new):
    pct = lambda c, n: f"{(c / n * 100):.1f}%" if n else "0.0%"
    lines = []
    lines.append("# XAUUSD TradeManager Backtest -- Break-even A/B")
    lines.append("")
    lines.append(f"Signals replayed (BUY): {old['n']}  |  Granularity: {REPLAY_GRAN}  |  "
                 f"Forward window: {LOOKFORWARD_H}h")
    lines.append("")
    lines.append("OLD = fixed break-even (arm +10 / lock entry+5), partial RR 1.0")
    lines.append("NEW = ATR break-even (arm 1.5xATR / lock 0.5xATR), partial RR 1.5")
    lines.append("")
    lines.append("| Metric | OLD | NEW |")
    lines.append("|---|---|---|")
    lines.append(_row("Net R", f"{old['net_R']:+.1f}", f"{new['net_R']:+.1f}"))
    lines.append(_row("Avg R / trade", f"{old['avg_R']:+.3f}", f"{new['avg_R']:+.3f}"))
    lines.append(_row("Win rate", f"{old['win_rate']:.1f}%", f"{new['win_rate']:.1f}%"))
    pf_o = "inf" if old["pf"] == float("inf") else f"{old['pf']:.2f}"
    pf_n = "inf" if new["pf"] == float("inf") else f"{new['pf']:.2f}"
    lines.append(_row("Profit factor", pf_o, pf_n))
    lines.append(_row("Break-even shake-outs",
                      f"{old['be_shakeouts']} ({pct(old['be_shakeouts'], old['n'])})",
                      f"{new['be_shakeouts']} ({pct(new['be_shakeouts'], new['n'])})"))
    lines.append("")
    lines.append("## Exit breakdown")
    lines.append("")
    lines.append("| Outcome | OLD | NEW |")
    lines.append("|---|---|---|")
    for c in CLASSES:
        oc, nc = old["cls"].get(c, 0), new["cls"].get(c, 0)
        if oc or nc:
            lines.append(_row(c, oc, nc))

    report = "\n".join(lines)
    print("\n" + report + "\n")

    os.makedirs("output", exist_ok=True)
    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"Wrote {OUTPUT_REPORT}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("OANDA_API_KEY")
    if not api_key:
        print("ERROR: OANDA_API_KEY not in environment (.env). Cannot fetch candles.")
        return
    env = "practice" if PRACTICE else "live"
    client = oandapyV20.API(access_token=api_key, environment=env)

    print("Loading BUY signals ...")
    signals = load_buy_signals()
    if MAX_SIGNALS:
        signals = signals[:MAX_SIGNALS]
    print(f"  {len(signals)} BUY signals to test (set TM_BT_MAX to change)")

    print("\nFetching/caching candles + computing ATR ...")
    prepared = prepare(client, signals)
    print(f"  {len(prepared)} signals with usable candle data")
    if not prepared:
        print("No usable data -- aborting.")
        return

    print("\nReplaying through real TradeManager (OLD config) ...")
    old = run_config(prepared, "OLD")
    print("Replaying through real TradeManager (NEW config) ...")
    new = run_config(prepared, "NEW")

    print_and_write(old, new)


if __name__ == "__main__":
    main()
