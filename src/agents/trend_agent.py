"""TrendAgent — deterministic H1 trend-following signal for XAU/USD.

Not predictive: it reads the current H1 trend and fires when three mechanical
conditions agree. Symmetric long/short.

Signal (H1 only):
  BUY  if EMA_FAST > EMA_SLOW AND ADX(14) >= TREND_ADX_MIN AND MACD histogram > 0
  SELL if EMA_FAST < EMA_SLOW AND ADX(14) >= TREND_ADX_MIN AND MACD histogram < 0
  else HOLD

SL/TP and position sizing live downstream (execution engine) and are unchanged.
Confidence is ADX-scaled and recorded only — no gate reads it.
"""

import logging
from typing import Dict, List, Optional

from .base import AgentVote, Signal
from .indicators import (
    to_dataframe, rsi, macd, ema, atr, adx,
    bollinger_bands, fisher_transform, market_structure,
)
from config.settings import settings


class TrendAgent:
    """H1 EMA/ADX/MACD trend follower. analyze() never raises."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("TrendAgent")

    def analyze(
        self,
        pair: str,
        candles: List[Dict],           # H1 candles (execution TF)
        htf_candles: Optional[dict],   # unused — kept for signature parity
        price: float,
    ) -> AgentVote:
        """Return a BUY/SELL/HOLD vote. Always returns AgentVote — never raises."""
        try:
            return self._analyze(pair, candles, price)
        except Exception as exc:
            self.logger.warning(f"TrendAgent analysis failed for {pair}: {exc}")
            return self._hold(pair, "Analysis error")

    def _analyze(self, pair: str, candles: List[Dict], price: float) -> AgentVote:
        df = to_dataframe(candles)
        if df is None or len(df) == 0:
            return self._hold(pair, "No candle data")

        ema_f = ema(df, settings.TREND_EMA_FAST)
        ema_s = ema(df, settings.TREND_EMA_SLOW)
        adx_v = adx(df, 14)
        m = macd(df)  # (line, signal, hist) or None

        if ema_f is None or ema_s is None or adx_v is None or m is None:
            return self._hold(pair, "Insufficient data for trend")

        hist = m[2]
        adx_ok = adx_v >= settings.TREND_ADX_MIN
        up = ema_f > ema_s and adx_ok and hist > 0
        down = ema_f < ema_s and adx_ok and hist < 0

        if not (up or down):
            return self._hold(pair, "No trend alignment")

        fast, slow = settings.TREND_EMA_FAST, settings.TREND_EMA_SLOW
        adx_floor = int(settings.TREND_ADX_MIN)
        conf_list = [
            f"EMA{fast} {'>' if up else '<'} EMA{slow}",
            f"ADX {adx_v:.0f} >= {adx_floor}",
            f"MACD hist {'+' if up else '-'}",
        ]
        confidence = min(0.55 + max(0.0, adx_v - settings.TREND_ADX_MIN) * 0.01, 0.90)
        signal = Signal.BUY if up else Signal.SELL
        reasoning = " | ".join(conf_list)

        self.logger.info(
            f"TrendAgent: {pair} {signal.value} | {reasoning} | conf={confidence:.2f}"
        )
        return AgentVote(
            agent_name="TrendAgent",
            pair=pair,
            signal=signal,
            confidence=round(confidence, 4),
            reasoning=reasoning,
            setup_type="TREND",
            meta={"confirmations": conf_list},
        )

    def get_indicators(
        self,
        pair: str,
        candles: List[Dict],
        htf_candles: Optional[dict],
        price: float,
    ) -> dict:
        """Standard H1 indicator snapshot + trend_* keys. Recorded on every trade."""
        indicators: dict = {}
        try:
            df = to_dataframe(candles)
            if df is not None and len(df) > 0:
                r = rsi(df, 14)
                if r is not None:
                    indicators['rsi'] = round(r, 2)

                m = macd(df)
                if m is not None:
                    indicators['macd_hist'] = round(m[2], 4)

                ema20 = ema(df, settings.TREND_EMA_FAST)
                ema50 = ema(df, settings.TREND_EMA_SLOW)
                if ema20 is not None:
                    indicators['ema_20'] = round(ema20, 2)
                if ema50 is not None:
                    indicators['ema_50'] = round(ema50, 2)
                if ema20 is not None and ema50 is not None:
                    indicators['trend'] = 'bullish' if ema20 > ema50 else 'bearish'

                atr_val = atr(df, 14)
                if atr_val is not None:
                    indicators['atr'] = round(atr_val, 2)

                adx_val = adx(df, 14)
                if adx_val is not None:
                    indicators['adx'] = round(adx_val, 2)

                bb = bollinger_bands(df, 20, 2.0)
                if bb is not None:
                    indicators['bb_upper'] = round(bb[0], 2)
                    indicators['bb_mid'] = round(bb[1], 2)
                    indicators['bb_lower'] = round(bb[2], 2)

                fisher = fisher_transform(df, 9)
                if fisher is not None:
                    indicators['fisher'] = round(fisher[0], 4)

                ms = market_structure(df)
                if ms:
                    indicators['market_structure'] = ms
        except Exception as exc:
            self.logger.debug(f"Indicator calc failed: {exc}")

        # Trend-decision snapshot
        try:
            vote = self.analyze(pair, candles, htf_candles, price)
            indicators['trend_signal'] = vote.signal.value
            indicators['trend_adx'] = indicators.get('adx')
            indicators['trend_ema_fast'] = settings.TREND_EMA_FAST
            indicators['trend_ema_slow'] = settings.TREND_EMA_SLOW
        except Exception:
            pass

        return indicators

    def _hold(self, pair: str, reason: str) -> AgentVote:
        return AgentVote(
            agent_name="TrendAgent",
            pair=pair,
            signal=Signal.HOLD,
            confidence=0.0,
            reasoning=reason,
            setup_type="NONE",
            meta={"confirmations": []},
        )
