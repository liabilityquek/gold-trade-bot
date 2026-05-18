"""Exposure tracker for XAU_USD gold trading.

Simplified for single-instrument trading. Tracks total gold exposure
in USD terms to ensure compliance with max exposure limits.
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from config.settings import settings
from src.broker.base import Position


@dataclass
class ExposureReport:
    """Exposure report for XAU_USD."""
    total_exposure_percent: float
    long_value_usd: float
    short_value_usd: float
    net_value_usd: float
    open_positions_count: int
    under_limit: bool
    limit_percent: float


class ExposureTracker:
    """Track aggregate exposure for XAU_USD."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('exposure_tracker')
        self.max_total_exposure = settings.MAX_TOTAL_EXPOSURE

        self._current_positions: List[Position] = []
        self._current_balance: float = 0.0
        self._last_report: Optional[ExposureReport] = None

    def update_positions(
        self,
        positions: List[Position],
        account_balance: float,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> ExposureReport:
        self._current_positions = positions or []
        self._current_balance = account_balance

        self._last_report = self.calculate_exposure(
            open_positions=self._current_positions,
            account_balance=self._current_balance,
            current_prices=current_prices,
        )

        if self._last_report.open_positions_count > 0:
            self.logger.debug(
                f"Gold exposure: {self._last_report.total_exposure_percent:.1%} "
                f"({self._last_report.open_positions_count} positions)"
            )

        return self._last_report

    def get_current_exposure(self) -> Optional[ExposureReport]:
        return self._last_report

    def calculate_exposure(
        self,
        open_positions: List[Position],
        account_balance: float,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> ExposureReport:
        """Calculate total gold exposure in USD."""
        long_value = 0.0
        short_value = 0.0

        for position in open_positions:
            units = float(position.net_units)
            abs_units = abs(units)

            # Get current price for valuation
            current_price = None
            if current_prices:
                current_price = current_prices.get(position.pair) or current_prices.get('XAU_USD')
            price = current_price or position.average_price
            if not price:
                self.logger.error(f"No price for position {position.pair} — skipping from exposure calc")
                continue

            # XAU_USD: value = units × price (1 unit = 1 oz at current price)
            value_usd = abs_units * price

            if units > 0:
                long_value += value_usd
            else:
                short_value += value_usd

        net_value = long_value - short_value
        total_exposure_percent = (abs(net_value) / account_balance) if account_balance > 0 else 0.0
        under_limit = total_exposure_percent <= self.max_total_exposure

        return ExposureReport(
            total_exposure_percent=total_exposure_percent,
            long_value_usd=long_value,
            short_value_usd=short_value,
            net_value_usd=net_value,
            open_positions_count=len(open_positions),
            under_limit=under_limit,
            limit_percent=self.max_total_exposure * 100,
        )

    def check_new_position_exposure(
        self,
        units: int,
        current_exposure_report: ExposureReport,
        account_balance: float,
        current_price: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Check if adding a new XAU_USD position would exceed exposure limits."""
        abs_units = abs(units)
        if not current_price:
            return False, "Cannot check exposure: no current gold price available"
        price = current_price
        new_position_value = abs_units * price

        new_net_value = abs(current_exposure_report.net_value_usd + (
            new_position_value if units > 0 else -new_position_value
        ))
        new_exposure_percent = new_net_value / account_balance if account_balance > 0 else 0

        limit = self.max_total_exposure

        if new_exposure_percent >= limit:
            return False, (
                f"New position would exceed exposure limit: "
                f"{new_exposure_percent*100:.1f}% >= {limit*100:.0f}%"
            )

        return True, ""

    def get_available_exposure(
        self,
        current_exposure_report: ExposureReport,
        account_balance: float,
    ) -> float:
        """Remaining available exposure as a fraction."""
        used = current_exposure_report.total_exposure_percent
        return max(0.0, self.max_total_exposure - used)
