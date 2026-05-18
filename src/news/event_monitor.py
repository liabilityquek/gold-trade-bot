"""Economic event monitoring for XAU/USD gold trading.

Uses the jb-news API (jblanked.com) to fetch today's economic calendar events.

Gold-specific:
- Monitors USD events (inverse correlation with gold)
- Also monitors geopolitical keywords (safe-haven demand driver)
- COMEX-related events are tracked
- All high-impact USD events affect XAU_USD
"""

import logging
import requests
from typing import List, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import pytz

from config.settings import settings


class EventImpact(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass
class EconomicEvent:
    event_id: str
    time: datetime
    currency: str
    impact: EventImpact
    event_name: str
    forecast: str
    previous: str
    actual: str
    affects_pairs: List[str]
    minutes_until: float

    def is_imminent(self, minutes: int = 30) -> bool:
        return 0 <= self.minutes_until <= minutes

    def is_past(self) -> bool:
        return self.minutes_until < 0


# Gold-specific VERY_HIGH impact keywords
_VERY_HIGH_KEYWORDS = [
    # US monetary policy — primary gold driver
    'NFP', 'Non-Farm Payroll', 'Nonfarm', 'FOMC', 'Federal Reserve', 'Fed Rate',
    'Federal Open Market', 'Fed Chair', 'Powell', 'Monetary Policy',
    # Inflation — drives real yields → gold
    'CPI', 'Consumer Price Index', 'PCE', 'Core PCE', 'Inflation',
    # Growth
    'GDP', 'Gross Domestic Product',
    # Labour
    'Unemployment Rate', 'Employment Change',
    # Policy rates
    'Interest Rate', 'Rate Decision',
    # Gold-specific
    'COMEX', 'Gold', 'XAU',
    # Geopolitical (safe-haven demand)
    'Geopolitical', 'War', 'Sanctions', 'Crisis',
]

# USD-denominated events always affect XAU_USD
_USD_CURRENCIES = {'USD'}

# jb-news impact string → EventImpact
_IMPACT_MAP = {
    'High': EventImpact.HIGH,
    'Medium': EventImpact.MEDIUM,
    'Low': EventImpact.LOW,
}

_JB_NEWS_BASE = "https://www.jblanked.com/news/api"


class EventMonitor:
    """Monitor economic events relevant to XAU/USD.

    Falls back to empty event list when API unavailable —
    trading continues normally (no false suspensions).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('event_monitor')

        self._cached_events: List[EconomicEvent] = []
        self._last_update: Optional[datetime] = None
        self._update_interval = timedelta(hours=settings.EVENT_CACHE_TTL_HOURS)

        self.high_impact_keywords: List[str] = list(
            set(settings.HIGH_IMPACT_EVENTS) | set(_VERY_HIGH_KEYWORDS)
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_upcoming_events(
        self,
        hours_ahead: int = 24,
        hours_behind: int = 1,
        min_impact: EventImpact = EventImpact.MEDIUM,
        force_refresh: bool = False,
    ) -> List[EconomicEvent]:
        now = datetime.now(pytz.UTC)

        if (not force_refresh
                and self._last_update
                and (now - self._last_update) < self._update_interval
                and self._cached_events is not None):
            self.logger.debug("Using cached events")
            self._update_minutes_until(self._cached_events, now)
            events = [
                e for e in self._cached_events
                if e.minutes_until >= -(hours_behind * 60)
                and e.minutes_until <= hours_ahead * 60
            ]
            return self._filter_events(events, min_impact)

        raw_events = self._fetch_calendar_events()
        self._update_minutes_until(raw_events, now)
        self._cached_events = raw_events
        self._last_update = now

        events = [
            e for e in raw_events
            if e.minutes_until >= -(hours_behind * 60)
            and e.minutes_until <= hours_ahead * 60
        ]

        upcoming_count = sum(1 for e in events if e.minutes_until >= 0)
        self.logger.info(
            f"EventMonitor: {upcoming_count} upcoming gold-relevant events "
            f"({len(events)} total in window)"
        )
        return self._filter_events(events, min_impact)

    def get_imminent_events(
        self,
        minutes: int = 30,
        min_impact: EventImpact = EventImpact.HIGH,
    ) -> List[EconomicEvent]:
        all_events = self.get_upcoming_events(hours_ahead=2)

        imminent = [
            event for event in all_events
            if event.is_imminent(minutes)
            and self._impact_level(event.impact) >= self._impact_level(min_impact)
        ]

        if imminent:
            self.logger.warning(f"Found {len(imminent)} imminent high-impact gold events!")

        return imminent

    def get_events_for_gold(self, hours_ahead: int = 24) -> List[EconomicEvent]:
        """Get all events affecting XAU/USD."""
        all_events = self.get_upcoming_events(hours_ahead=hours_ahead)
        return [e for e in all_events if 'XAU_USD' in e.affects_pairs]

    def should_suspend_trading(
        self,
        pair: Optional[str] = None,
        minutes_before: Optional[int] = None,
    ) -> tuple:
        if minutes_before is None:
            minutes_before = settings.NEWS_SUSPEND_BEFORE_MINUTES

        imminent = self.get_imminent_events(
            minutes=minutes_before,
            min_impact=EventImpact.HIGH,
        )

        if not imminent:
            return False, None

        # For gold: any USD event or geopolitical = suspend XAU_USD
        for event in imminent:
            if 'XAU_USD' in event.affects_pairs:
                self.logger.warning(
                    f"Suspension triggered: {event.event_name} "
                    f"in {event.minutes_until:.0f} min"
                )
                return True, event

        return False, None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _update_minutes_until(self, events: List[EconomicEvent], now: datetime) -> None:
        for e in events:
            e.minutes_until = (e.time - now).total_seconds() / 60.0

    def _fetch_calendar_events(self) -> List[EconomicEvent]:
        api_key = settings.JB_NEWS_API_KEY
        if not api_key:
            self.logger.warning("JB_NEWS_API_KEY not set — calendar events unavailable")
            return []

        url = f"{_JB_NEWS_BASE}/mql5/calendar/today/"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {api_key}",
        }

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.logger.warning(f"jb-news calendar fetch failed: {exc}")
            return []

        if not isinstance(data, list):
            self.logger.warning(f"Unexpected jb-news response: {type(data)}")
            return []

        now = datetime.now(pytz.UTC)
        events: List[EconomicEvent] = []

        for item in data:
            try:
                event = self._parse_event(item, now)
                if event is not None:
                    events.append(event)
            except Exception as exc:
                self.logger.debug(f"Event parse error: {exc}")

        return events

    def _parse_event(self, item: dict, now: datetime) -> Optional[EconomicEvent]:
        name = item.get("Name", "")
        currency = item.get("Currency", "")
        event_id = str(item.get("Event_ID", ""))
        impact_str = item.get("Impact", "Low")
        date_str = item.get("Date", "")
        actual = item.get("Actual", 0.0)
        forecast = item.get("Forecast", 0.0)
        previous = item.get("Previous", 0.0)

        # For gold: only track USD events and geopolitical (affects XAU regardless of currency)
        name_upper = name.upper()
        is_geopolitical = any(kw.upper() in name_upper for kw in ['WAR', 'SANCTIONS', 'CRISIS', 'GEOPOLIT', 'COMEX', 'XAU', 'GOLD'])

        if currency not in _USD_CURRENCIES and not is_geopolitical:
            return None

        try:
            event_dt = datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S")
            event_dt = pytz.UTC.localize(event_dt)
        except ValueError:
            return None

        minutes_until = (event_dt - now).total_seconds() / 60.0

        impact = _IMPACT_MAP.get(impact_str, EventImpact.LOW)

        # Upgrade to VERY_HIGH if name matches critical keywords
        if impact == EventImpact.HIGH:
            if any(kw.upper() in name_upper for kw in _VERY_HIGH_KEYWORDS):
                impact = EventImpact.VERY_HIGH

        # All USD events + geopolitical affect XAU_USD
        affects_pairs = ['XAU_USD']

        return EconomicEvent(
            event_id=event_id,
            time=event_dt,
            currency=currency,
            impact=impact,
            event_name=name,
            forecast=str(forecast),
            previous=str(previous),
            actual=str(actual),
            affects_pairs=affects_pairs,
            minutes_until=minutes_until,
        )

    def _filter_events(
        self,
        events: List[EconomicEvent],
        min_impact: EventImpact,
    ) -> List[EconomicEvent]:
        min_level = self._impact_level(min_impact)
        return [e for e in events if self._impact_level(e.impact) >= min_level]

    def _impact_level(self, impact: EventImpact) -> int:
        return {
            EventImpact.LOW: 1,
            EventImpact.MEDIUM: 2,
            EventImpact.HIGH: 3,
            EventImpact.VERY_HIGH: 4,
        }.get(impact, 0)

    def get_event_summary(self, events: List[EconomicEvent]) -> str:
        if not events:
            return "No upcoming gold-relevant events"

        lines = [f"Upcoming Gold Events ({len(events)}):"]

        for event in sorted(events, key=lambda e: e.minutes_until):
            time_str = f"{int(event.minutes_until)}min" if event.minutes_until >= 0 else "PAST"
            lines.append(
                f"  [{time_str}] {event.currency}: {event.event_name} ({event.impact.value})"
            )

        return "\n".join(lines)
