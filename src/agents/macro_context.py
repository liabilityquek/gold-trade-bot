"""Macro context assembler for XAU/USD gold trading.

Gold-specific macro drivers (vs FX bot which uses rate differentials):
  1. DXY trend — inverse correlation with gold (strong USD = bearish gold)
  2. Real yields (10Y nominal - inflation) — inverse correlation with gold
  3. Fed tone — hawkish = bearish gold, dovish = bullish gold
  4. Geopolitical risk — safe-haven demand driver for gold
  5. Risk appetite (equity direction) — risk-off = gold bullish

Provides macro context dict for LLMAgent prompt injection.
All methods fail silently — data fetch failure never blocks a trade cycle.
"""

import logging
import threading
import time
from typing import Optional

import requests

from config.settings import settings


# ---------------------------------------------------------------------------
# FRED API — central bank rate auto-fetch
# ---------------------------------------------------------------------------
_FRED_BASE = 'https://api.stlouisfed.org/fred/series/observations'

_FRED_SERIES = {
    'USD': 'FEDFUNDS',   # Fed Funds Effective Rate
    # 10Y Treasury nominal yield (used with inflation to compute real yield)
    '10Y_NOMINAL': 'DGS10',
    # 10Y TIPS (real yield proxy) — direct measure
    '10Y_REAL': 'DFII10',
    # PCE inflation (Fed's preferred measure)
    'PCE_INFLATION': 'PCEPI',
}

_CB_RATES_CACHE: dict = {}
_CB_RATES_CACHE_TS: float = 0.0
_CB_RATES_CACHE_TTL: float = 86400.0  # 24h
_CB_RATES_LOCK = threading.Lock()


def _fetch_fred_data(api_key: str) -> dict:
    """Fetch gold-relevant FRED series. Returns partial dict on partial failure."""
    result = {}
    for label, series_id in _FRED_SERIES.items():
        try:
            resp = requests.get(
                _FRED_BASE,
                params={
                    'series_id': series_id,
                    'api_key': api_key,
                    'sort_order': 'desc',
                    'limit': '1',
                    'file_type': 'json',
                },
                timeout=8,
            )
            resp.raise_for_status()
            obs = resp.json().get('observations', [])
            if obs and obs[0].get('value') not in ('.', '', None):
                result[label] = round(float(obs[0]['value']), 4)
        except Exception:
            pass
    return result


def _get_fred_data() -> dict:
    """Return FRED data, auto-fetched and cached 24h."""
    global _CB_RATES_CACHE, _CB_RATES_CACHE_TS

    api_key = settings.FRED_API_KEY
    if api_key:
        with _CB_RATES_LOCK:
            needs_refresh = time.time() - _CB_RATES_CACHE_TS > _CB_RATES_CACHE_TTL
            if needs_refresh:
                fetched = _fetch_fred_data(api_key)
                if fetched:
                    _CB_RATES_CACHE = fetched
                    _CB_RATES_CACHE_TS = time.time()

    with _CB_RATES_LOCK:
        return dict(_CB_RATES_CACHE)


# ---------------------------------------------------------------------------
# JB News API — gold-relevant headlines
# ---------------------------------------------------------------------------
_JB_NEWS_BASE = 'https://www.jblanked.com/news/api'
_NEWS_ENDPOINT = f'{_JB_NEWS_BASE}/forex/news/'
_news_cache: dict = {}


class MacroContext:
    """Assembles macro briefing for XAU/USD gold trading."""

    def __init__(
        self,
        event_monitor=None,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or logging.getLogger('macro_context')
        self._event_monitor = event_monitor

    def build(self) -> dict:
        """Build complete macro context dict for XAU/USD.

        Returns:
            {
                'fed_rate': float,
                'real_yield': str,
                'real_yield_signal': str,  'BEARISH_GOLD' | 'BULLISH_GOLD' | 'NEUTRAL'
                'fed_tone': str,
                'dxy_note': str,
                'recent_news': str,
                'upcoming_events': str,
            }

        Never raises — returns empty strings on any failure.
        """
        result = {
            'fed_rate': settings.CB_RATE_USD,
            'real_yield': '',
            'real_yield_signal': 'NEUTRAL',
            'fed_tone': '',
            'dxy_note': (
                'DXY: inverse correlation — strong USD = bearish gold pressure; '
                'weak USD = bullish gold support'
            ),
            'recent_news': '',
            'upcoming_events': '',
        }

        try:
            result.update(self._get_fred_context())
        except Exception as exc:
            self.logger.debug(f'MacroContext: FRED context failed: {exc}')

        try:
            result['recent_news'] = self._get_gold_news()
        except Exception as exc:
            self.logger.debug(f'MacroContext: gold news fetch failed: {exc}')

        try:
            result['upcoming_events'] = self._get_gold_events()
        except Exception as exc:
            self.logger.debug(f'MacroContext: gold events failed: {exc}')

        return result

    # ------------------------------------------------------------------
    # FRED context — real yields and Fed rate
    # ------------------------------------------------------------------

    def _get_fred_context(self) -> dict:
        """Compute real yield signal and Fed rate from FRED data."""
        fred = _get_fred_data()
        result = {}

        # Fed funds rate
        if 'USD' in fred:
            result['fed_rate'] = fred['USD']

        # Real yield (10Y TIPS or estimate from nominal - PCE)
        if '10Y_REAL' in fred:
            real_yield = fred['10Y_REAL']
            result['real_yield'] = f"{real_yield:.2f}%"
            # Higher real yields = stronger opportunity cost = bearish gold
            if real_yield > settings.MACRO_REAL_YIELD_BEARISH_THRESHOLD:
                result['real_yield_signal'] = 'BEARISH_GOLD'
                result['fed_tone'] = 'restrictive (high real yields — gold headwind)'
            elif real_yield < settings.MACRO_REAL_YIELD_BULLISH_THRESHOLD:
                result['real_yield_signal'] = 'BULLISH_GOLD'
                result['fed_tone'] = 'stimulative (negative real yields — gold tailwind)'
            else:
                result['real_yield_signal'] = 'NEUTRAL'
                result['fed_tone'] = 'moderate real yields — neutral for gold'
        elif '10Y_NOMINAL' in fred and 'PCE_INFLATION' in fred:
            # Estimate real yield
            nominal = fred['10Y_NOMINAL']
            pce = fred.get('PCE_INFLATION', 2.0)
            real_yield = nominal - pce
            result['real_yield'] = f"~{real_yield:.2f}% (est)"
            result['real_yield_signal'] = (
                'BEARISH_GOLD' if real_yield > settings.MACRO_REAL_YIELD_BEARISH_THRESHOLD
                else 'BULLISH_GOLD' if real_yield < settings.MACRO_REAL_YIELD_BULLISH_THRESHOLD
                else 'NEUTRAL'
            )

        return result

    # ------------------------------------------------------------------
    # JB News — gold/USD headlines
    # ------------------------------------------------------------------

    def _get_gold_news(self) -> str:
        api_key = settings.JB_NEWS_API_KEY
        if not api_key:
            return '(JB_NEWS_API_KEY not set)'

        cache_ttl = settings.EVENT_CACHE_TTL_HOURS * 3600
        headlines = {}

        for currency in ['USD', 'XAU']:
            cached = _news_cache.get(currency)
            if cached:
                ts, text = cached
                if time.time() - ts < cache_ttl:
                    headlines[currency] = text
                    continue

            text = self._fetch_currency_news(currency, api_key)
            _news_cache[currency] = (time.time(), text)
            headlines[currency] = text

        sections = []
        for currency, text in headlines.items():
            if text and text not in ('(no recent news)', '(news fetch failed)'):
                sections.append(f'  [{currency}]\n{text}')

        return '\n'.join(sections) if sections else '  (no recent gold/USD news)'

    def _fetch_currency_news(self, currency: str, api_key: str) -> str:
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Api-Key {api_key}',
        }
        try:
            resp = requests.get(
                _NEWS_ENDPOINT,
                headers=headers,
                params={'currency': currency},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.logger.debug(f'JB News fetch failed for {currency}: {exc}')
            return '(news fetch failed)'

        if not isinstance(data, list) or not data:
            return '(no recent news)'

        lines = []
        for item in data[:5]:
            title = item.get('title', '')
            source = item.get('source', '')
            if title:
                lines.append(f'  - {title}' + (f' [{source}]' if source else ''))

        return '\n'.join(lines) if lines else '(no recent news)'

    # ------------------------------------------------------------------
    # Upcoming gold-relevant events
    # ------------------------------------------------------------------

    def _get_gold_events(self) -> str:
        if self._event_monitor is None:
            return '(event monitor not connected)'

        try:
            events = self._event_monitor.get_events_for_gold(hours_ahead=8)
        except Exception:
            return '(event fetch failed)'

        if not events:
            return '  None in next 8 hours'

        lines = []
        for e in sorted(events, key=lambda x: x.minutes_until):
            if e.minutes_until < 0:
                continue
            h = int(e.minutes_until // 60)
            m = int(e.minutes_until % 60)
            countdown = f'{h}h {m}m' if h > 0 else f'{m}m'
            lines.append(
                f'  [{countdown}] {e.currency} — {e.event_name} ({e.impact.value.upper()})'
            )

        return '\n'.join(lines) if lines else '  None in next 8 hours'
