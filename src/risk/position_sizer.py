"""Position sizing calculator for XAU_USD gold trading.

Gold specifics:
- 1 unit = 1 troy ounce
- pip_value = $1.00 per unit (1 point = $1/oz at any price)
- No currency conversion needed (quote is USD)
- Formula: units = (NAV * risk_pct) / sl_distance_in_usd
"""

import logging
from enum import Enum
from typing import Optional, Tuple
from dataclasses import dataclass

from config.settings import settings


class PositionSizingMethod(Enum):
    PERCENT_RISK = "percent_risk"
    KELLY = "kelly"


@dataclass
class PositionSizeResult:
    units: int
    risk_amount: float
    risk_percent: float
    method: PositionSizingMethod
    leverage_used: float
    pip_value: float
    notes: str = ""


# Gold: pip value is $1 per unit regardless of price
_GOLD_PIP_VALUE = 1.0
_GOLD_MIN_UNITS = 1


class PositionSizer:
    """Calculate position sizes based on risk parameters for XAU_USD."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('position_sizer')
        self.max_leverage = settings.MAX_LEVERAGE
        self.max_risk_per_trade = settings.MAX_RISK_PER_TRADE

    def calculate(
        self,
        account_balance: float,
        stop_loss_points: float,
        risk_percent: Optional[float] = None,
        method: PositionSizingMethod = PositionSizingMethod.PERCENT_RISK,
        kelly_win_rate: Optional[float] = None,
        kelly_avg_win: Optional[float] = None,
        kelly_avg_loss: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> Optional[PositionSizeResult]:
        """Calculate position size for XAU_USD.

        Args:
            account_balance: Current account balance in USD
            stop_loss_points: SL distance in USD/oz points (e.g. 15.0 = $15/oz)
            risk_percent: Risk as fraction (default: from settings, 0.01 = 1%)
            method: Sizing method
            kelly_win_rate: Win rate for Kelly criterion
            kelly_avg_win: Average win amount (USD)
            kelly_avg_loss: Average loss amount (USD, positive)
            current_price: Current XAU/USD price (used for leverage calc only)

        Returns:
            PositionSizeResult or None if calculation fails
        """
        if account_balance <= 0:
            self.logger.error(f"Invalid account balance: ${account_balance}")
            return None

        if stop_loss_points <= 0:
            self.logger.error(f"Invalid stop loss points: {stop_loss_points}")
            return None

        if risk_percent is None:
            risk_percent = self.max_risk_per_trade

        if risk_percent > self.max_risk_per_trade:
            self.logger.warning(
                f"Risk {risk_percent*100:.1f}% exceeds max {self.max_risk_per_trade*100:.1f}%, capping"
            )
            risk_percent = self.max_risk_per_trade

        if method == PositionSizingMethod.PERCENT_RISK:
            return self._calculate_percent_risk(
                account_balance, stop_loss_points, risk_percent, current_price
            )
        elif method == PositionSizingMethod.KELLY:
            return self._calculate_kelly(
                account_balance, stop_loss_points,
                kelly_win_rate, kelly_avg_win, kelly_avg_loss, current_price
            )
        else:
            self.logger.error(f"Unknown sizing method: {method}")
            return None

    def _calculate_percent_risk(
        self,
        account_balance: float,
        stop_loss_points: float,
        risk_percent: float,
        current_price: Optional[float] = None,
    ) -> PositionSizeResult:
        """units = (balance * risk_pct) / (sl_points * pip_value_per_unit)"""
        risk_amount = account_balance * risk_percent

        # For XAU_USD: pip_value_per_unit = $1.00/oz — no conversion needed
        pip_value_per_unit = _GOLD_PIP_VALUE

        position_size = risk_amount / (stop_loss_points * pip_value_per_unit)
        units = max(_GOLD_MIN_UNITS, int(position_size))

        pip_value = pip_value_per_unit * units
        actual_risk_amount = stop_loss_points * pip_value
        actual_risk_percent = actual_risk_amount / account_balance

        notes = ""
        if current_price and current_price > 0:
            notional_value = units * current_price
            leverage_used = notional_value / account_balance
            if leverage_used > self.max_leverage:
                max_units = int(account_balance * self.max_leverage / current_price)
                if max_units < units:
                    units = max(max_units, _GOLD_MIN_UNITS)
                    pip_value = pip_value_per_unit * units
                    actual_risk_amount = stop_loss_points * pip_value
                    actual_risk_percent = actual_risk_amount / account_balance
                    notes = f"Position capped by max leverage {self.max_leverage}:1"
                    self.logger.warning(notes)
        else:
            self.logger.warning("No live price available — leverage cap check skipped")

        return PositionSizeResult(
            units=units,
            risk_amount=actual_risk_amount,
            risk_percent=actual_risk_percent,
            method=PositionSizingMethod.PERCENT_RISK,
            leverage_used=leverage_used,
            pip_value=pip_value,
            notes=notes,
        )

    def _calculate_kelly(
        self,
        account_balance: float,
        stop_loss_points: float,
        win_rate: Optional[float],
        avg_win: Optional[float],
        avg_loss: Optional[float],
        current_price: Optional[float] = None,
    ) -> Optional[PositionSizeResult]:
        """Kelly criterion with 0.25 fractional Kelly."""
        if win_rate is None or avg_win is None or avg_loss is None:
            self.logger.error("Kelly requires win_rate, avg_win, avg_loss")
            return None

        if not (0 < win_rate < 1) or avg_win <= 0 or avg_loss <= 0:
            self.logger.error("Invalid Kelly parameters")
            return None

        loss_rate = 1 - win_rate
        kelly_percent = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
        adjusted_kelly = kelly_percent * 0.25

        adjusted_kelly = min(adjusted_kelly, self.max_risk_per_trade)
        if adjusted_kelly <= 0:
            adjusted_kelly = 0.01

        notes = f"Full Kelly: {kelly_percent*100:.1f}%, Fractional 25%: {adjusted_kelly*100:.2f}%"

        result = self._calculate_percent_risk(
            account_balance, stop_loss_points, adjusted_kelly, current_price
        )

        if result:
            result.method = PositionSizingMethod.KELLY
            result.notes = notes + (f" | {result.notes}" if result.notes else "")

        return result

    def get_max_position_size(self, account_balance: float, current_price: float) -> int:
        """Maximum position size based on leverage limit."""
        max_notional = account_balance * self.max_leverage
        return max(_GOLD_MIN_UNITS, int(max_notional / current_price))

    def validate_position_size(
        self,
        units: int,
        account_balance: float,
        current_price: float,
    ) -> Tuple[bool, str]:
        if units < _GOLD_MIN_UNITS:
            return False, f"Position size {units} below minimum {_GOLD_MIN_UNITS}"

        notional_value = units * current_price
        leverage = notional_value / account_balance

        if leverage > self.max_leverage:
            max_units = int(account_balance * self.max_leverage / current_price)
            return False, (
                f"Leverage {leverage:.1f}:1 exceeds max {self.max_leverage}:1 "
                f"(max units: {max_units})"
            )

        return True, "Position size valid"
