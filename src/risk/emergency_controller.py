"""Emergency Risk Controller for XAU_USD gold trading.

Triggers emergency shutdown on:
- Max drawdown > 20%
- Daily loss limit > 3% NAV
- Margin call (balance <= 0)
- Manual trigger via Telegram /stop

Ported from fx-trading-bot emergency_controller.py — gold thresholds same.
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum

from config.settings import settings


class EmergencyLevel(IntEnum):
    NONE = 0
    WARNING = 1
    CRITICAL = 2
    PANIC = 3


class ShutdownReason(Enum):
    MANUAL = "manual"
    EXPOSURE_BREACH = "exposure_breach"
    DRAWDOWN_LIMIT = "drawdown_limit"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MARGIN_CALL = "margin_call"
    NEWS_EVENT = "news_event"
    SYSTEM_ERROR = "system_error"


@dataclass
class EmergencyStatus:
    level: EmergencyLevel
    active_alerts: List[str]
    positions_at_risk: int
    recommended_action: str
    requires_shutdown: bool
    shutdown_reason: Optional[ShutdownReason]


@dataclass
class ShutdownReport:
    success: bool
    positions_closed: int
    orders_cancelled: int
    reason: ShutdownReason
    timestamp: datetime
    total_loss_usd: float
    errors: List[str]


class EmergencyRiskController:
    """Emergency risk management for gold trading."""

    def __init__(
        self,
        max_drawdown_percent: float = 0.20,  # 20% max drawdown
        max_daily_loss_percent: float = 0.03,  # 3% daily loss limit (gold CLAUDE.md)
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or logging.getLogger('emergency_controller')

        self.max_total_exposure = settings.MAX_TOTAL_EXPOSURE
        self.max_drawdown = max_drawdown_percent
        self.max_daily_loss = max_daily_loss_percent

        self.emergency_active = False
        self.daily_pnl_start_balance = None
        self.daily_pnl_reset_date = None
        self.circuit_breaker_triggered = False
        self.shutdown_history: List[ShutdownReport] = []

    def check_emergency_conditions(
        self,
        account_balance: float,
        initial_balance: float,
        open_positions: List,
        current_exposure_percent: float,
        unrealized_pnl: float,
        account_nav: Optional[float] = None,
    ) -> EmergencyStatus:
        alerts = []
        level = EmergencyLevel.NONE
        requires_shutdown = False
        shutdown_reason = None
        positions_at_risk = 0

        # Check 1: Exposure breach
        max_exposure_percent = self.max_total_exposure * 100

        if current_exposure_percent > max_exposure_percent * 1.5:
            level = EmergencyLevel.PANIC
            requires_shutdown = True
            shutdown_reason = ShutdownReason.EXPOSURE_BREACH
            alerts.append(
                f"CRITICAL: Exposure at {current_exposure_percent:.1f}% "
                f"(limit {max_exposure_percent:.0f}%) - SHUTDOWN REQUIRED"
            )
        elif current_exposure_percent > max_exposure_percent:
            level = EmergencyLevel.CRITICAL
            alerts.append(
                f"EXPOSURE BREACH: {current_exposure_percent:.1f}% > "
                f"{max_exposure_percent:.0f}% - Close positions"
            )
            positions_at_risk = len(open_positions)
        elif current_exposure_percent > max_exposure_percent * 0.9:
            level = EmergencyLevel.WARNING
            alerts.append(f"Exposure high: {current_exposure_percent:.1f}%")

        # Check 2: Drawdown limit (uses NAV including unrealized P/L)
        if initial_balance > 0:
            nav_for_drawdown = account_nav if account_nav is not None else account_balance
            current_drawdown = (initial_balance - nav_for_drawdown) / initial_balance

            if current_drawdown >= self.max_drawdown:
                level = EmergencyLevel.PANIC
                requires_shutdown = True
                shutdown_reason = ShutdownReason.DRAWDOWN_LIMIT
                alerts.append(
                    f"MAX DRAWDOWN EXCEEDED: {current_drawdown*100:.1f}% "
                    f"(limit {self.max_drawdown*100:.0f}%) - EMERGENCY STOP"
                )
            elif current_drawdown >= self.max_drawdown * 0.75:
                if level < EmergencyLevel.CRITICAL:
                    level = EmergencyLevel.CRITICAL
                alerts.append(
                    f"High drawdown: {current_drawdown*100:.1f}% "
                    f"(limit {self.max_drawdown*100:.0f}%)"
                )

        # Check 3: Daily loss limit
        daily_loss = self._calculate_daily_loss(account_balance)

        if daily_loss is not None and daily_loss >= self.max_daily_loss:
            level = EmergencyLevel.PANIC
            requires_shutdown = True
            shutdown_reason = ShutdownReason.DAILY_LOSS_LIMIT
            alerts.append(
                f"DAILY LOSS LIMIT: {daily_loss*100:.1f}% "
                f"(limit {self.max_daily_loss*100:.0f}%) - STOP TRADING"
            )

        # Check 4: Margin call
        if account_balance <= 0:
            level = EmergencyLevel.PANIC
            requires_shutdown = True
            shutdown_reason = ShutdownReason.MARGIN_CALL
            alerts.append("MARGIN CALL: Account balance <= 0")

        # Check 5: Large unrealized losses (15% of balance)
        if unrealized_pnl < -(account_balance * 0.15):
            if level < EmergencyLevel.CRITICAL:
                level = EmergencyLevel.CRITICAL
            alerts.append(
                f"Large unrealized loss: ${unrealized_pnl:,.2f} "
                f"({unrealized_pnl / account_balance * 100:.1f}%)"
            )
            positions_at_risk = len(open_positions)

        if requires_shutdown:
            recommended_action = "EMERGENCY SHUTDOWN — Close all positions immediately"
        elif level == EmergencyLevel.CRITICAL:
            recommended_action = "Close positions to reduce exposure/risk"
        elif level == EmergencyLevel.WARNING:
            recommended_action = "Monitor closely, tighten stop losses"
        else:
            recommended_action = "Continue normal operations"

        status = EmergencyStatus(
            level=level,
            active_alerts=alerts,
            positions_at_risk=positions_at_risk,
            recommended_action=recommended_action,
            requires_shutdown=requires_shutdown,
            shutdown_reason=shutdown_reason,
        )

        for alert in alerts:
            if level == EmergencyLevel.PANIC:
                self.logger.critical(alert)
            elif level == EmergencyLevel.CRITICAL:
                self.logger.error(alert)
            else:
                self.logger.warning(alert)

        return status

    def execute_emergency_shutdown(
        self,
        broker_client,
        reason: ShutdownReason,
        force: bool = False,
    ) -> ShutdownReport:
        """Close all positions immediately."""
        if self.circuit_breaker_triggered and not force:
            self.logger.error("Circuit breaker active — shutdown already in progress")
            return ShutdownReport(
                success=False,
                positions_closed=0,
                orders_cancelled=0,
                reason=reason,
                timestamp=datetime.now(timezone.utc),
                total_loss_usd=0.0,
                errors=["Circuit breaker active"],
            )

        self.logger.critical(f"EMERGENCY SHUTDOWN INITIATED — Reason: {reason.value}")

        self.emergency_active = True
        self.circuit_breaker_triggered = True

        errors = []
        positions_closed = 0
        total_loss = 0.0

        try:
            open_positions = broker_client.get_positions()
            self.logger.info(f"Found {len(open_positions)} open positions to close")

            for position in open_positions:
                instrument = "unknown"
                try:
                    instrument = position.pair
                    unrealized_pl = position.unrealized_pnl
                    total_loss += unrealized_pl

                    broker_client.close_position(instrument)
                    positions_closed += 1

                    self.logger.warning(
                        f"Closed position: {instrument} (P/L: ${unrealized_pl:,.2f})"
                    )

                except Exception as e:
                    error_msg = f"Failed to close {instrument}: {e}"
                    errors.append(error_msg)
                    self.logger.error(error_msg)

            success = len(errors) == 0 or positions_closed > 0

            report = ShutdownReport(
                success=success,
                positions_closed=positions_closed,
                orders_cancelled=0,
                reason=reason,
                timestamp=datetime.now(timezone.utc),
                total_loss_usd=total_loss,
                errors=errors,
            )

            self.shutdown_history.append(report)

            self.logger.critical(
                f"Emergency shutdown complete: {positions_closed} positions closed, "
                f"total loss: ${total_loss:,.2f}"
            )

            return report

        except Exception as e:
            return ShutdownReport(
                success=False,
                positions_closed=positions_closed,
                orders_cancelled=0,
                reason=reason,
                timestamp=datetime.now(timezone.utc),
                total_loss_usd=total_loss,
                errors=[f"Emergency shutdown failed: {e}"] + errors,
            )

    def reset_circuit_breaker(self):
        """Reset circuit breaker — use with caution."""
        self.logger.warning("Circuit breaker RESET — trading can resume")
        self.circuit_breaker_triggered = False
        self.emergency_active = False

    def _calculate_daily_loss(self, current_balance: float) -> Optional[float]:
        """Calculate daily loss percentage. Resets at midnight UTC."""
        today = datetime.now(timezone.utc).date()

        if self.daily_pnl_reset_date != today:
            self.daily_pnl_start_balance = current_balance
            self.daily_pnl_reset_date = today
            return None

        if self.daily_pnl_start_balance is None or self.daily_pnl_start_balance <= 0:
            self.daily_pnl_start_balance = current_balance
            return None

        loss = (self.daily_pnl_start_balance - current_balance) / self.daily_pnl_start_balance
        return max(0.0, loss)
