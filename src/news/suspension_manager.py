"""Trading suspension management during gold-relevant news events.

Rules:
  Rule 1: Suspend 30min before HIGH/VERY_HIGH USD event
  Rule 2: Resume 30min after event passes
  (Rule 3 — pre-event closes — handled by NewsWatcher)

Gold-specific: single instrument XAU_USD (not a set of pairs).
"""

import logging
from typing import Dict, Optional, Set, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import pytz

from .event_monitor import EventMonitor, EconomicEvent, EventImpact
from config.settings import settings


class SuspensionReason(Enum):
    HIGH_IMPACT_NEWS = "high_impact_news"
    MAJOR_EVENT = "major_event"
    MANUAL = "manual"
    VOLATILITY = "volatility"


@dataclass
class SuspensionStatus:
    is_suspended: bool
    reason: Optional[SuspensionReason]
    triggering_event: Optional[EconomicEvent]
    resume_time: Optional[datetime]
    message: str


class SuspensionManager:
    """Manage trading suspensions for XAU/USD based on economic events."""

    def __init__(
        self,
        event_monitor: EventMonitor,
        logger: Optional[logging.Logger] = None,
        on_suspension_lifted=None,
    ):
        self.logger = logger or logging.getLogger('suspension_manager')
        self.event_monitor = event_monitor

        self.suspend_before_minutes = settings.NEWS_SUSPEND_BEFORE_MINUTES
        self.resume_after_minutes = settings.NEWS_RESUME_AFTER_MINUTES

        self._on_suspension_lifted = on_suspension_lifted

        self.manually_suspended = False
        self._suspended: bool = False
        self._suspension_event: Optional[EconomicEvent] = None

    def check_suspension_status(self) -> SuspensionStatus:
        """Check if XAU/USD trading should be suspended."""
        if self.manually_suspended:
            return SuspensionStatus(
                is_suspended=True,
                reason=SuspensionReason.MANUAL,
                triggering_event=None,
                resume_time=None,
                message="Trading manually suspended",
            )

        should_suspend, event = self.event_monitor.should_suspend_trading(
            pair='XAU_USD',
            minutes_before=self.suspend_before_minutes,
        )

        if should_suspend and event:
            resume_time = event.time + timedelta(minutes=self.resume_after_minutes)
            self._suspended = True
            self._suspension_event = event

            return SuspensionStatus(
                is_suspended=True,
                reason=SuspensionReason.HIGH_IMPACT_NEWS,
                triggering_event=event,
                resume_time=resume_time,
                message=f"XAU/USD suspended: {event.event_name} in {event.minutes_until:.0f}min",
            )

        self._check_resume()

        return SuspensionStatus(
            is_suspended=False,
            reason=None,
            triggering_event=None,
            resume_time=None,
            message="Trading allowed",
        )

    def is_suspended(self) -> bool:
        """Quick check: is XAU/USD currently suspended?"""
        status = self.check_suspension_status()
        return status.is_suspended

    def suspend_trading(self, reason: SuspensionReason = SuspensionReason.MANUAL):
        """Manually suspend trading."""
        self.manually_suspended = True
        self.logger.warning(f"XAU/USD trading suspended: {reason.value}")

    def resume_trading(self):
        """Resume trading."""
        self.manually_suspended = False
        self._suspended = False
        self._suspension_event = None
        self.logger.info("XAU/USD trading resumed")

    def should_close_positions(
        self,
        minutes_before_event: int = 5,
    ) -> Tuple[bool, Optional[EconomicEvent]]:
        """Check if positions should be closed (very imminent event)."""
        imminent = self.event_monitor.get_imminent_events(
            minutes=minutes_before_event,
            min_impact=EventImpact.VERY_HIGH,
        )

        if imminent:
            event = imminent[0]
            self.logger.critical(
                f"CLOSE POSITIONS: {event.event_name} in {event.minutes_until:.0f} minutes!"
            )
            return True, event

        return False, None

    def _check_resume(self):
        """Check if a previously suspended instrument should resume."""
        if not self._suspension_event:
            return

        now = datetime.now(pytz.UTC)
        resume_time = self._suspension_event.time + timedelta(minutes=self.resume_after_minutes)

        if now >= resume_time:
            event_name = self._suspension_event.event_name
            self._suspended = False
            self._suspension_event = None

            if self._on_suspension_lifted:
                try:
                    self._on_suspension_lifted('XAU_USD')
                except Exception as e:
                    self.logger.warning(f"on_suspension_lifted callback error: {e}")

            self.logger.info(f"Auto-resumed XAU/USD trading after {event_name}")
