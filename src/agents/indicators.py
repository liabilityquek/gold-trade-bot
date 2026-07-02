"""Shared indicator functions for the multi-agent system.

All functions operate on a pd.DataFrame with columns: open, high, low, close, volume.
Each returns Optional[float] (or tuple). Returns None when there is insufficient data
(len(df) < period + 5) so agents can exclude the component from scoring gracefully.
"""

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Data conversion
# ---------------------------------------------------------------------------

def to_dataframe(candles: List[Dict]) -> pd.DataFrame:
    """Convert candle dicts to a DataFrame with OHLCV float columns.

    Accepts two formats:
    - Flat broker format: {'open': ..., 'high': ..., 'low': ..., 'close': ..., 'volume': ...}
    - OANDA raw API format: {'mid': {'o': ..., 'h': ..., 'l': ..., 'c': ...}, 'volume': ...}
    """
    rows = []
    for c in candles:
        if 'mid' in c:
            mid = c['mid']
            rows.append({
                'open':   float(mid.get('o', 0)),
                'high':   float(mid.get('h', 0)),
                'low':    float(mid.get('l', 0)),
                'close':  float(mid.get('c', 0)),
                'volume': float(c.get('volume', 0)),
            })
        else:
            rows.append({
                'open':   float(c.get('open', 0)),
                'high':   float(c.get('high', 0)),
                'low':    float(c.get('low', 0)),
                'close':  float(c.get('close', 0)),
                'volume': float(c.get('volume', 0)),
            })
    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# RSI — Wilder's smoothing
# ---------------------------------------------------------------------------

def rsi(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """RSI using Wilder's smoothing (ewm alpha=1/period)."""
    if len(df) < period + 5:
        return None

    close = df['close']
    delta = close.diff().dropna()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)

    avg_gain = gains.ewm(com=period - 1, adjust=False).mean().iloc[-1]
    avg_loss = losses.ewm(com=period - 1, adjust=False).mean().iloc[-1]

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Optional[Tuple[float, float, float]]:
    """Return (macd_line, signal_line, histogram) or None."""
    min_bars = slow + signal_period + 5
    if len(df) < min_bars:
        return None

    close = df['close']
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    num_std: float = 2.0,
) -> Optional[Tuple[float, float, float]]:
    """Return (upper, middle, lower) or None."""
    if len(df) < period + 5:
        return None

    close = df['close']
    middle = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std().iloc[-1]

    if pd.isna(middle) or pd.isna(std) or std == 0.0:
        return None

    upper = middle + num_std * std
    lower = middle - num_std * std
    return float(upper), float(middle), float(lower)


# ---------------------------------------------------------------------------
# EMA — single current value
# ---------------------------------------------------------------------------

def ema(df: pd.DataFrame, period: int) -> Optional[float]:
    """Current EMA value."""
    if len(df) < period + 5:
        return None
    val = df['close'].ewm(span=period, adjust=False).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


# ---------------------------------------------------------------------------
# ADX — Wilder smoothing on TR/+DM/-DM
# ---------------------------------------------------------------------------

def adx_di(df: pd.DataFrame, period: int = 14) -> Optional[Tuple[float, float, float]]:
    """Return (ADX, +DI, -DI) using Wilder's smoothing, or None.

    +DI/-DI come from directional movement (high/low range expansion), so they
    carry different information from a close-price EMA — usable as an independent
    direction check, not just a strength gate.
    """
    if len(df) < period * 2 + 5:
        return None

    high = df['high']
    low = df['low']
    close = df['close']

    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    plus_dm = high - prev_high
    minus_dm = prev_low - low

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_s = tr.ewm(com=period - 1, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(com=period - 1, adjust=False).mean() / atr_s)
    minus_di = 100 * (minus_dm.ewm(com=period - 1, adjust=False).mean() / atr_s)

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    adx_val = dx.ewm(com=period - 1, adjust=False).mean().iloc[-1]

    pdi, mdi = plus_di.iloc[-1], minus_di.iloc[-1]
    if pd.isna(adx_val) or pd.isna(pdi) or pd.isna(mdi):
        return None
    return float(adx_val), float(pdi), float(mdi)


def adx(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """ADX magnitude only (Wilder). Thin wrapper over adx_di()."""
    r = adx_di(df, period)
    return r[0] if r else None


# ---------------------------------------------------------------------------
# ATR — simple rolling mean
# ---------------------------------------------------------------------------

def atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """ATR using simple rolling mean over True Range."""
    if len(df) < period + 5:
        return None

    high = df['high']
    low = df['low']
    close = df['close']

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


# ---------------------------------------------------------------------------
# Fisher Transform
# ---------------------------------------------------------------------------

def fisher_transform(
    df: pd.DataFrame,
    period: int = 10,
) -> Optional[Tuple[float, float, float, float]]:
    """Return (fisher_now, signal_now, fisher_prev, signal_prev) or None."""
    if len(df) < period + 5:
        return None

    high = df['high']
    low = df['low']

    hh = high.rolling(period).max()
    ll = low.rolling(period).min()

    hl_range = (hh - ll).replace(0, np.nan)
    value = ((high + low) / 2 - ll) / hl_range
    value = value.clip(0.001, 0.999)

    fisher_series = 0.5 * np.log((1 + value) / (1 - value))
    signal_series = fisher_series.shift(1)

    valid = fisher_series.dropna()
    if len(valid) < 2:
        return None

    fisher_now  = float(fisher_series.iloc[-1])
    signal_now  = float(signal_series.iloc[-1])
    fisher_prev = float(fisher_series.iloc[-2])
    signal_prev = float(signal_series.iloc[-2])

    if any(math.isnan(v) for v in [fisher_now, signal_now, fisher_prev, signal_prev]):
        return None

    return fisher_now, signal_now, fisher_prev, signal_prev


# ---------------------------------------------------------------------------
# Market Structure — swing high/low classification
# ---------------------------------------------------------------------------

def market_structure(
    df: pd.DataFrame,
    lookback: int = 50,
    swing_window: int = 5,
) -> Optional[Tuple[str, float, float]]:
    """Classify recent market structure and identify key S/R levels.

    Returns:
        (structure_label, nearest_resistance, nearest_support) or None.
        structure_label: 'bullish_structure' | 'bearish_structure' | 'ranging'
    """
    min_bars = lookback + swing_window * 2
    if len(df) < min_bars:
        return None

    sub = df.tail(lookback).reset_index(drop=True)
    highs = sub['high']
    lows = sub['low']
    closes = sub['close']
    current_price = float(closes.iloc[-1])

    swing_highs: List[float] = []
    swing_lows: List[float] = []

    for i in range(swing_window, len(sub) - swing_window):
        window_high = highs.iloc[i - swing_window: i + swing_window + 1]
        if highs.iloc[i] == window_high.max():
            swing_highs.append(float(highs.iloc[i]))

        window_low = lows.iloc[i - swing_window: i + swing_window + 1]
        if lows.iloc[i] == window_low.min():
            swing_lows.append(float(lows.iloc[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    sh1, sh2 = swing_highs[-2], swing_highs[-1]
    sl1, sl2 = swing_lows[-2], swing_lows[-1]

    hh = sh2 > sh1
    lh = sh2 < sh1
    hl = sl2 > sl1
    ll = sl2 < sl1

    if hh and hl:
        label = 'bullish_structure'
    elif lh and ll:
        label = 'bearish_structure'
    else:
        label = 'ranging'

    above = [h for h in swing_highs if h > current_price]
    resistance = min(above) if above else swing_highs[-1]

    below = [l for l in swing_lows if l < current_price]
    support = max(below) if below else swing_lows[-1]

    return label, resistance, support
