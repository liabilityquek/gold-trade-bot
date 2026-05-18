"""Risk validator for pre-trade safety checks on XAU_USD.

Gold-specific:
- pip_value = $1 per unit (no currency conversion)
- MIN_RR_RATIO = 1.5 (vs FX bot's 2.5)
- Single instrument — no multi-pair correlation checks
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from config.settings import settings


class ValidationResult(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    WARNING = "warning"


@dataclass
class TradeValidationReport:
    result: ValidationResult
    approved: bool
    reasons: List[str]
    warnings: List[str]
    checks_passed: Dict[str, bool]
    risk_metrics: Dict[str, float]


class RiskValidator:
    """Validate XAU_USD trades against risk management rules."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('risk_validator')
        self.max_risk_per_trade = settings.MAX_RISK_PER_TRADE   # 0.01
        self.max_total_exposure = settings.MAX_TOTAL_EXPOSURE
        self.max_leverage = settings.MAX_LEVERAGE
        self.min_rr_ratio = settings.MIN_RR_RATIO               # 1.5

    def validate_trade(
        self,
        units: int,
        stop_loss_points: float,
        account_balance: float,
        current_exposure_percent: float,
        open_trade_count: int,
        entry_price: Optional[float] = None,
        margin_available: Optional[float] = None,
        rr_ratio: Optional[float] = None,
    ) -> TradeValidationReport:
        """Validate a gold trade before execution.

        Args:
            units: Position size in oz
            stop_loss_points: SL distance in USD/oz points
            account_balance: Current account balance
            current_exposure_percent: Current total exposure (fraction, e.g. 0.05)
            open_trade_count: Number of currently open trades
            entry_price: Entry price (USD/oz)
            margin_available: Available margin
            rr_ratio: Planned risk/reward ratio for this trade
        """
        reasons = []
        warnings = []
        checks_passed = {}
        risk_metrics = {}

        price = entry_price  # None if not provided — price-dependent checks will be skipped

        # Check 1: Max concurrent trades
        check_name = "concurrent_trades"
        max_trades = settings.MAX_CONCURRENT_TRADES
        if open_trade_count >= max_trades:
            reasons.append(
                f"Max concurrent trades reached: {open_trade_count} >= {max_trades}"
            )
            checks_passed[check_name] = False
        else:
            checks_passed[check_name] = True
        risk_metrics['open_trade_count'] = open_trade_count

        # Check 2: Position size vs leverage limit
        check_name = "position_size"
        risk_metrics['position_units'] = abs(units)
        if price:
            max_units = int((account_balance * self.max_leverage) / price)
            if abs(units) > max_units:
                reasons.append(
                    f"Position too large: {abs(units)} oz > max {max_units} oz at {self.max_leverage}x leverage"
                )
                checks_passed[check_name] = False
            else:
                checks_passed[check_name] = True
            risk_metrics['max_units_allowed'] = max_units
        else:
            self.logger.warning("No entry price — leverage size check skipped")
            checks_passed[check_name] = True

        # Check 3: Risk per trade (gold: pip_value = $1/unit)
        check_name = "risk_per_trade"
        risk_amount = abs(units) * stop_loss_points * 1.0  # $1/oz per point
        risk_percent = risk_amount / account_balance if account_balance > 0 else 0
        max_risk_pct = self.max_risk_per_trade

        risk_metrics['risk_amount_usd'] = risk_amount
        risk_metrics['risk_percent'] = risk_percent * 100

        if risk_percent > max_risk_pct:
            reasons.append(
                f"Risk too high: {risk_percent*100:.2f}% > max {max_risk_pct*100:.1f}%"
            )
            checks_passed[check_name] = False
        elif risk_percent > max_risk_pct * 0.85:
            warnings.append(
                f"Risk approaching limit: {risk_percent*100:.2f}% (max {max_risk_pct*100:.1f}%)"
            )
            checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True

        # Check 4: Total exposure after this trade
        check_name = "total_exposure"
        risk_metrics['current_exposure'] = current_exposure_percent * 100
        if price:
            new_margin = (abs(units) * price) / self.max_leverage
            new_exposure = current_exposure_percent + (new_margin / account_balance if account_balance > 0 else 0)
            max_exposure = self.max_total_exposure

            risk_metrics['new_exposure'] = new_exposure * 100

            if new_exposure > max_exposure:
                reasons.append(
                    f"Exposure would exceed limit: {new_exposure*100:.1f}% > {max_exposure*100:.0f}%"
                )
                checks_passed[check_name] = False
            elif new_exposure > max_exposure * 0.9:
                warnings.append(f"Exposure nearing limit: {new_exposure*100:.1f}%")
                checks_passed[check_name] = True
            else:
                checks_passed[check_name] = True
        else:
            self.logger.warning("No entry price — total exposure check skipped")
            checks_passed[check_name] = True

        # Check 5: Margin available
        check_name = "margin_available"
        if margin_available is not None and price:
            margin_required = (abs(units) * price) / self.max_leverage
            risk_metrics['margin_required'] = margin_required
            risk_metrics['margin_available'] = margin_available

            if margin_required > margin_available:
                reasons.append(
                    f"Insufficient margin: need ${margin_required:,.2f}, have ${margin_available:,.2f}"
                )
                checks_passed[check_name] = False
            else:
                checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True

        # Check 6: Stop loss distance reasonable for gold
        check_name = "stop_loss"
        if stop_loss_points < 2.0:
            reasons.append(f"SL too tight: {stop_loss_points:.1f} pts (min 2 pts for gold)")
            checks_passed[check_name] = False
        elif stop_loss_points > 200.0:
            warnings.append(f"SL very wide: {stop_loss_points:.1f} pts")
            checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True
        risk_metrics['stop_loss_points'] = stop_loss_points

        # Check 7: Risk/reward ratio
        check_name = "risk_reward"
        if rr_ratio is not None:
            if rr_ratio < self.min_rr_ratio:
                reasons.append(
                    f"RR ratio too low: {rr_ratio:.2f} < min {self.min_rr_ratio}"
                )
                checks_passed[check_name] = False
            else:
                checks_passed[check_name] = True
            risk_metrics['rr_ratio'] = rr_ratio
        else:
            checks_passed[check_name] = True

        # Check 8: Account balance
        check_name = "account_balance"
        if account_balance <= 0:
            reasons.append(f"Account balance insufficient: ${account_balance:,.2f}")
            checks_passed[check_name] = False
        elif account_balance < 100:
            warnings.append(f"Account balance low: ${account_balance:,.2f}")
            checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True
        risk_metrics['account_balance'] = account_balance

        all_checks_passed = all(checks_passed.values())

        if all_checks_passed:
            result = ValidationResult.WARNING if warnings else ValidationResult.APPROVED
            approved = True
        else:
            result = ValidationResult.REJECTED
            approved = False

        report = TradeValidationReport(
            result=result,
            approved=approved,
            reasons=reasons,
            warnings=warnings,
            checks_passed=checks_passed,
            risk_metrics=risk_metrics,
        )

        if approved:
            self.logger.info(
                f"Trade validation PASSED: {abs(units)} oz, SL={stop_loss_points:.1f} pts, "
                f"risk={risk_percent*100:.2f}%"
            )
            for w in warnings:
                self.logger.warning(f"  {w}")
        else:
            self.logger.error(f"Trade validation FAILED: {', '.join(reasons)}")

        return report
