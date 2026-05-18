"""Stop-loss and take-profit calculator for XAU_USD gold trading.

Gold specifics:
- Prices in USD/oz (e.g. 3285.42)
- 1 point = $1/oz = 1 pip for XAU_USD
- ATR is directly in USD/oz (no pip conversion needed)
- Three TP targets: TP1 (nearest structure), TP2 (extended), TP3 (breakout)
"""

import logging
from typing import Optional, Tuple
from dataclasses import dataclass

import pandas as pd

from config.settings import settings


@dataclass
class GoldSLTPLevels:
    """Calculated SL/TP levels for XAU_USD."""
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    sl_distance: float      # USD/oz
    tp1_distance: float     # USD/oz
    rr_ratio_tp1: float
    rr_ratio_tp2: float
    rr_ratio_tp3: float
    method: str


class StopLossTakeProfitCalculator:
    """Calculate stop-loss and take-profit levels for gold."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('sl_tp_calculator')
        self.default_rr_ratio = settings.MIN_RR_RATIO  # 1.5

    def calculate_atr_based(
        self,
        entry_price: float,
        is_long: bool,
        historical_data: pd.DataFrame,
        atr_multiplier: float = 2.0,
        adaptive: bool = True,
    ) -> GoldSLTPLevels:
        """Calculate SL/TP using ATR for XAU_USD.

        TP1 = 1.5× SL distance (min RR)
        TP2 = 2.0× SL distance
        TP3 = 3.0× SL distance (breakout target)
        """
        atr_val = self._calculate_atr(historical_data)

        if atr_val is None or atr_val == 0:
            self.logger.warning(f"Could not calculate ATR, using default {settings.DEFAULT_ATR_POINTS} points")
            atr_val = settings.DEFAULT_ATR_POINTS

        if adaptive:
            atr_multiplier = self._get_adaptive_multiplier(atr_val, historical_data)

        sl_distance = atr_val * atr_multiplier

        if is_long:
            stop_loss = entry_price - sl_distance
            tp1 = entry_price + sl_distance * settings.TP1_MULTIPLIER
            tp2 = entry_price + sl_distance * settings.TP2_MULTIPLIER
            tp3 = entry_price + sl_distance * settings.TP3_MULTIPLIER
        else:
            stop_loss = entry_price + sl_distance
            tp1 = entry_price - sl_distance * settings.TP1_MULTIPLIER
            tp2 = entry_price - sl_distance * settings.TP2_MULTIPLIER
            tp3 = entry_price - sl_distance * settings.TP3_MULTIPLIER

        tp1_distance = abs(tp1 - entry_price)

        return GoldSLTPLevels(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            sl_distance=sl_distance,
            tp1_distance=tp1_distance,
            rr_ratio_tp1=tp1_distance / sl_distance if sl_distance > 0 else 0,
            rr_ratio_tp2=abs(tp2 - entry_price) / sl_distance if sl_distance > 0 else 0,
            rr_ratio_tp3=abs(tp3 - entry_price) / sl_distance if sl_distance > 0 else 0,
            method=f"atr_{atr_multiplier:.1f}x",
        )

    def calculate_structural(
        self,
        entry_price: float,
        is_long: bool,
        sl_level: float,
        tp1_level: float,
        tp2_level: Optional[float] = None,
        tp3_level: Optional[float] = None,
    ) -> GoldSLTPLevels:
        """Build SL/TP from Uncle Lim structural levels (zone-based)."""
        sl_distance = abs(entry_price - sl_level)
        tp1_distance = abs(entry_price - tp1_level)

        # If TP2/TP3 not provided, extrapolate from TP1 distance
        if tp2_level is None:
            if is_long:
                tp2_level = entry_price + tp1_distance * (4.0 / 3.0)
            else:
                tp2_level = entry_price - tp1_distance * (4.0 / 3.0)

        if tp3_level is None:
            if is_long:
                tp3_level = entry_price + tp1_distance * 2.0
            else:
                tp3_level = entry_price - tp1_distance * 2.0

        return GoldSLTPLevels(
            entry_price=entry_price,
            stop_loss=sl_level,
            take_profit_1=tp1_level,
            take_profit_2=tp2_level,
            take_profit_3=tp3_level,
            sl_distance=sl_distance,
            tp1_distance=tp1_distance,
            rr_ratio_tp1=tp1_distance / sl_distance if sl_distance > 0 else 0,
            rr_ratio_tp2=abs(tp2_level - entry_price) / sl_distance if sl_distance > 0 else 0,
            rr_ratio_tp3=abs(tp3_level - entry_price) / sl_distance if sl_distance > 0 else 0,
            method="structural",
        )

    def _get_adaptive_multiplier(self, atr_value: float, data: pd.DataFrame, period: int = 50) -> float:
        """Return 1.5/2.0/3.0 based on current ATR vs rolling average."""
        if len(data) < period + 15:
            return 2.0
        high, low, close = data['high'], data['low'], data['close']
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_series = tr.rolling(window=14).mean()
        atr_avg = atr_series.iloc[-period:].mean()
        if pd.isna(atr_avg) or atr_avg == 0:
            return 2.0
        ratio = atr_value / atr_avg
        if ratio > settings.ATR_ADAPTIVE_RATIO_HIGH:
            return settings.TP3_MULTIPLIER
        if ratio < settings.ATR_ADAPTIVE_RATIO_LOW:
            return settings.TP1_MULTIPLIER
        return settings.TP2_MULTIPLIER

    def _calculate_atr(self, data: pd.DataFrame, period: int = 14) -> Optional[float]:
        if len(data) < period + 1:
            return None

        high_low = data['high'] - data['low']
        high_close = abs(data['high'] - data['close'].shift())
        low_close = abs(data['low'] - data['close'].shift())

        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean().iloc[-1]

        return float(atr) if pd.notna(atr) else None

    def validate_levels(self, levels: GoldSLTPLevels, is_long: bool) -> Tuple[bool, str]:
        """Validate SL/TP levels are logically consistent."""
        if is_long:
            if levels.stop_loss >= levels.entry_price:
                return False, "SL must be below entry for long"
            if levels.take_profit_1 <= levels.entry_price:
                return False, "TP1 must be above entry for long"
        else:
            if levels.stop_loss <= levels.entry_price:
                return False, "SL must be above entry for short"
            if levels.take_profit_1 >= levels.entry_price:
                return False, "TP1 must be below entry for short"

        if levels.sl_distance < 2.0:
            return False, f"SL too tight: {levels.sl_distance:.1f} pts (min 2 pts)"

        if levels.rr_ratio_tp1 < 1.0:
            return False, f"RR ratio too low: {levels.rr_ratio_tp1:.2f} (min 1.0)"

        return True, ""
