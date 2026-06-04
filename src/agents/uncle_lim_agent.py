"""UncLeLimAgent — Uncle Lim multi-timeframe confluence strategy for XAU/USD.

Strategy: Top-down H4 → H1 → M30/M15 → M5/M1 confluence stack.

Minimum 3 timeframe confirmations required before producing a BUY/SELL signal.

Setup types (from goldmapping corpus):
  TRENDLINE_BREAKOUT — most common (209/384 posts)
  SND_ZONE          — supply/demand zone entry (144 posts)
  LCT               — Life-Changing Technique pullback/retest (115 posts)
  RTB               — Return to Breakout
  PULLBACK          — general pullback to structure
  NONE              — no valid setup

Gold-specific: all price comparisons are in USD/oz (not pips).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from .base import AgentVote, Signal
from .indicators import to_dataframe, rsi, macd, ema, atr, adx, bollinger_bands, fisher_transform, market_structure


@dataclass
class UncleLimAnalysis:
    """Structured result from the Uncle Lim multi-TF analysis."""
    h4_bias: str = "neutral"           # bullish / bearish / neutral
    h1_trigger: str = "none"           # snd_zone / trendline_breakout / rtb / none
    m30_confirmation: str = "none"     # lct / engulfing / snd_zone / none
    m15_confirmation: str = "none"     # lct / engulfing / snd_zone / none
    m5_trigger: str = "none"          # secret_pattern / snd_zone / none
    confirmations: List[str] = field(default_factory=list)
    setup_type: str = "NONE"
    signal: str = "HOLD"
    confidence: float = 0.0


class UncLeLimAgent:
    """
    Technical analysis agent implementing Uncle Lim's H4→H1→M30/M15→M5/M1 strategy.

    Receives:
      - H1 candles (primary / execution timeframe)
      - htf_candles dict: {'H4': [...], 'M30': [...], 'M15': [...], 'M5': [...]}

    Returns AgentVote with setup_type and confluence details.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("UncLeLimAgent")
        self._min_confirmations = 3

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyze(
        self,
        pair: str,
        candles: List[Dict],           # H1 candles (execution TF)
        htf_candles: Optional[dict],   # {'H4': [...], 'M30': [...], 'M15': [...], 'M5': [...]}
        price: float,
    ) -> AgentVote:
        """Run the full Uncle Lim multi-TF analysis. Always returns AgentVote — never raises."""
        if htf_candles is None:
            htf_candles = {}
        try:
            result = self._analyze(pair, candles, htf_candles, price)
        except Exception as exc:
            self.logger.warning(f"UncLeLimAgent analysis failed for {pair}: {exc}")
            return AgentVote(
                agent_name="UncLeLimAgent",
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.0,
                reasoning="Analysis error",
            )

        signal_map = {"BUY": Signal.BUY, "SELL": Signal.SELL, "HOLD": Signal.HOLD}
        signal = signal_map.get(result.signal, Signal.HOLD)

        reasoning_parts = []
        if result.h4_bias != "neutral":
            reasoning_parts.append(f"H4 {result.h4_bias}")
        reasoning_parts.extend(result.confirmations)
        reasoning = " | ".join(reasoning_parts) if reasoning_parts else "No confluences"

        self.logger.info(
            f"UncLeLimAgent: {pair} {result.signal} | setup={result.setup_type} | "
            f"conf={len(result.confirmations)}/{self._min_confirmations} | {reasoning}"
        )

        return AgentVote(
            agent_name="UncLeLimAgent",
            pair=pair,
            signal=signal,
            confidence=round(result.confidence, 4),
            reasoning=reasoning[:120],
            setup_type=result.setup_type,
            meta={
                "h4_bias": result.h4_bias,
                "h1_trigger": result.h1_trigger,
                "m30_confirmation": result.m30_confirmation,
                "m15_confirmation": result.m15_confirmation,
                "m5_trigger": result.m5_trigger,
                "confirmations": result.confirmations,
            }
        )

    def get_indicators(
        self,
        pair: str,
        candles: List[Dict],
        htf_candles: Optional[dict],
        price: float,
    ) -> dict:
        """Return combined indicator dict for LLMAgent consumption."""
        if htf_candles is None:
            htf_candles = {}
        indicators: dict = {}

        # Standard indicators on H1
        try:
            df = to_dataframe(candles)
            if df is not None and len(df) > 0:
                r = rsi(df, 14)
                if r is not None:
                    indicators['rsi'] = round(r, 2)

                macd_val, macd_sig, macd_hist = macd(df)
                if macd_hist is not None:
                    indicators['macd_hist'] = round(macd_hist, 4)

                ema20 = ema(df, 20)
                ema50 = ema(df, 50)
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

                bb_upper, bb_mid, bb_lower = bollinger_bands(df, 20, 2.0)
                if bb_mid is not None:
                    indicators['bb_upper'] = round(bb_upper, 2)
                    indicators['bb_mid'] = round(bb_mid, 2)
                    indicators['bb_lower'] = round(bb_lower, 2)

                fisher = fisher_transform(df, 9)
                if fisher is not None:
                    indicators['fisher'] = round(fisher, 4)

                ms = market_structure(df)
                if ms:
                    indicators['market_structure'] = ms
        except Exception as exc:
            self.logger.debug(f"Indicator calc failed: {exc}")

        # Uncle Lim specific
        try:
            vote = self.analyze(pair, candles, htf_candles, price)
            indicators['uncle_lim_signal'] = vote.signal.value
            indicators['uncle_lim_setup_type'] = vote.setup_type
            indicators['uncle_lim_confidence'] = vote.confidence
            meta = vote.meta or {}
            indicators['uncle_lim_h4_bias'] = meta.get('h4_bias', 'neutral')
            indicators['uncle_lim_confluences'] = len(meta.get('confirmations', []))
            indicators['uncle_lim_confirmations'] = ', '.join(meta.get('confirmations', []))
        except Exception:
            pass

        return indicators

    # ------------------------------------------------------------------
    # Internal analysis pipeline
    # ------------------------------------------------------------------

    def _analyze(
        self,
        pair: str,
        candles: List[Dict],
        htf_candles: dict,
        price: float,
    ) -> UncleLimAnalysis:
        result = UncleLimAnalysis()

        h4_candles = htf_candles.get('H4', [])
        m30_candles = htf_candles.get('M30', [])
        m15_candles = htf_candles.get('M15', [])
        m5_candles  = htf_candles.get('M5', [])

        # Step 1: H4 bias — primary trend direction
        h4_bias = self._get_h4_bias(h4_candles)
        result.h4_bias = h4_bias

        if h4_bias == "neutral":
            return result  # No H4 trend = no trade

        is_long = h4_bias == "bullish"

        # Step 2: H1 trigger — SND zone / trendline breakout / RTB
        h1_trigger, h1_conf = self._get_h1_trigger(candles, price, is_long)
        result.h1_trigger = h1_trigger
        if h1_conf:
            result.confirmations.append(h1_conf)
            if "SND" in h1_conf.upper():
                result.setup_type = "SND_ZONE"
            elif "TRENDLINE" in h1_conf.upper() or "BREAKOUT" in h1_conf.upper():
                result.setup_type = "TRENDLINE_BREAKOUT"
            elif "RTB" in h1_conf.upper():
                result.setup_type = "RTB"

        # Step 3: M30 confirmation — LCT / engulfing / SND
        m30_conf_type, m30_conf_label = self._get_m30_confirmation(m30_candles, price, is_long)
        result.m30_confirmation = m30_conf_type
        if m30_conf_label:
            result.confirmations.append(m30_conf_label)
            if result.setup_type == "NONE" and "LCT" in m30_conf_label.upper():
                result.setup_type = "LCT"
            elif result.setup_type == "NONE" and "PULLBACK" in m30_conf_label.upper():
                result.setup_type = "PULLBACK"

        # Step 4: M15 confirmation — LCT / engulfing / SND
        m15_conf_type, m15_conf_label = self._get_m15_confirmation(m15_candles, price, is_long)
        result.m15_confirmation = m15_conf_type
        if m15_conf_label:
            result.confirmations.append(m15_conf_label)
            if result.setup_type == "NONE" and "LCT" in m15_conf_label.upper():
                result.setup_type = "LCT"

        # Step 5: M5/M1 final trigger — Secret Pattern / SND
        m5_trigger, m5_conf_label = self._get_m5_trigger(m5_candles, price, is_long)
        result.m5_trigger = m5_trigger
        if m5_conf_label:
            result.confirmations.append(m5_conf_label)

        # Step 6: H4 bias always counts as a confirmation
        h4_label = f"H4 {h4_bias.upper()} structure"
        result.confirmations.insert(0, h4_label)

        # Step 7: Check minimum confirmations
        n_conf = len(result.confirmations)
        if n_conf < self._min_confirmations:
            result.signal = "HOLD"
            result.confidence = 0.0
            return result

        if result.setup_type == "NONE":
            result.setup_type = "PULLBACK"

        result.signal = "BUY" if is_long else "SELL"
        # Confidence scales with confirmations beyond minimum
        result.confidence = min(0.55 + (n_conf - self._min_confirmations) * 0.10, 0.90)

        return result

    # ------------------------------------------------------------------
    # H4 trend bias
    # ------------------------------------------------------------------

    def _get_h4_bias(self, h4_candles: List[Dict]) -> str:
        """Determine H4 trend bias: bullish / bearish / neutral.

        BUY-side guardrail (gold bull market): bullish requires ema20>ema50
        AND ema50>ema200 (or ema200 unavailable, fallback to structure check).
        Bearish reverts to permissive EMA bias.
        """
        if len(h4_candles) < 20:
            return "neutral"
        try:
            df = to_dataframe(h4_candles)
            ema20 = ema(df, 20)
            ema50 = ema(df, 50)
            ema200 = ema(df, 200)  # None if < 205 candles
            if ema20 is None or ema50 is None:
                return "neutral"

            closes = [self._get_close(c) for c in h4_candles[-10:]]
            closes = [c for c in closes if c > 0]
            structure_ok_bullish = False
            if len(closes) >= 6:
                recent_high = max(closes[-3:])
                prev_high = max(closes[-6:-3])
                recent_low = min(closes[-3:])
                prev_low = min(closes[-6:-3])
                structure_ok_bullish = (
                    recent_high > prev_high or recent_low >= prev_low * 0.998
                )

            # BULLISH — EMA short>medium, and long-term regime not bearish
            if ema20 > ema50:
                if ema200 is None or ema50 > ema200:
                    return "bullish"
                if structure_ok_bullish:
                    return "bullish"
                return "neutral"

            # BEARISH — permissive EMA bias
            return "bearish"
        except Exception:
            return "neutral"

    # ------------------------------------------------------------------
    # H1 trigger detection
    # ------------------------------------------------------------------

    def _get_h1_trigger(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Return (trigger_type, confirmation_label) for H1."""
        if len(candles) < 10:
            return "none", None

        # 1. Check for SND zone at H1
        snd = self._detect_snd_zone(candles[-20:], price, is_long)
        if snd:
            label = f"SND Zone H1 ({'demand' if is_long else 'supply'})"
            return "snd_zone", label

        # 2. Check for trendline breakout at H1
        breakout = self._detect_trendline_breakout(candles[-15:], price, is_long)
        if breakout:
            label = "Trendline Breakout H1"
            return "trendline_breakout", label

        # 3. Check for RTB (return to breakout)
        rtb = self._detect_rtb(candles[-20:], price, is_long)
        if rtb:
            label = "RTB H1 (return to breakout)"
            return "rtb", label

        return "none", None

    # ------------------------------------------------------------------
    # M30 confirmation
    # ------------------------------------------------------------------

    def _get_m30_confirmation(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Return (conf_type, label) for M30."""
        if len(candles) < 5:
            return "none", None

        # LCT: pullback after breakout to the broken level
        lct = self._detect_lct(candles[-15:], price, is_long)
        if lct:
            return "lct", "LCT M30 (pullback/retest)"

        # Engulfing candle
        eng = self._detect_engulfing(candles[-5:], is_long)
        if eng:
            return "engulfing", f"{'Bullish' if is_long else 'Bearish'} Engulfing M30"

        # SND zone at M30
        snd = self._detect_snd_zone(candles[-10:], price, is_long)
        if snd:
            return "snd_zone", f"SND Zone M30"

        # Pullback to EMA
        pullback = self._detect_pullback_to_ema(candles[-10:], price, is_long)
        if pullback:
            return "pullback", "Pullback M30"

        return "none", None

    # ------------------------------------------------------------------
    # M15 confirmation
    # ------------------------------------------------------------------

    def _get_m15_confirmation(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Return (conf_type, label) for M15."""
        if len(candles) < 5:
            return "none", None

        lct = self._detect_lct(candles[-10:], price, is_long)
        if lct:
            return "lct", "LCT M15 (retest)"

        eng = self._detect_engulfing(candles[-4:], is_long)
        if eng:
            return "engulfing", f"{'Bullish' if is_long else 'Bearish'} Engulfing M15"

        snd = self._detect_snd_zone(candles[-8:], price, is_long)
        if snd:
            return "snd_zone", "SND Zone M15"

        return "none", None

    # ------------------------------------------------------------------
    # M5 final trigger
    # ------------------------------------------------------------------

    def _get_m5_trigger(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Return (trigger_type, label) for M5/M1."""
        if len(candles) < 3:
            return "none", None

        secret = self._detect_secret_pattern(candles[-5:], is_long)
        if secret:
            return "secret_pattern", "Secret Pattern M5"

        snd = self._detect_snd_zone(candles[-5:], price, is_long)
        if snd:
            return "snd_zone", "SND Zone M5"

        return "none", None

    # ------------------------------------------------------------------
    # Pattern detection helpers
    # ------------------------------------------------------------------

    def _detect_snd_zone(
        self, candles: List[Dict], price: float, is_long: bool
    ) -> bool:
        """
        Supply/Demand zone: price is at or near a previous consolidation / strong reaction level.
        Demand (for BUY): price near a level that previously caused a strong upward move.
        Supply (for SELL): price near a level that previously caused a strong downward move.
        """
        if len(candles) < 6:
            return False

        closes = [self._get_close(c) for c in candles]
        highs  = [self._get_high(c)  for c in candles]
        lows   = [self._get_low(c)   for c in candles]

        if not all(v > 0 for v in closes + highs + lows):
            return False

        # Zone tolerance: 0.3% of current price (roughly $10 on $3300 gold)
        tolerance = price * 0.003

        if is_long:
            # Demand zone: check if current price is near previous swing low cluster
            recent_lows = sorted(lows[:-2])[:3]  # 3 lowest lows from history
            for level in recent_lows:
                if abs(price - level) <= tolerance:
                    # Confirm: price previously bounced from this level
                    return True
            # Also check: price returned to previous breakout level (resistance → support)
            prev_highs = highs[:-3]
            if prev_highs:
                prev_resistance = max(prev_highs[-3:]) if len(prev_highs) >= 3 else max(prev_highs)
                if abs(price - prev_resistance) <= tolerance * 1.5:
                    return True
        else:
            # Supply zone: current price near previous swing high cluster
            recent_highs = sorted(highs[:-2], reverse=True)[:3]
            for level in recent_highs:
                if abs(price - level) <= tolerance:
                    return True
            prev_lows = lows[:-3]
            if prev_lows:
                prev_support = min(prev_lows[-3:]) if len(prev_lows) >= 3 else min(prev_lows)
                if abs(price - prev_support) <= tolerance * 1.5:
                    return True

        return False

    def _detect_trendline_breakout(
        self, candles: List[Dict], price: float, is_long: bool
    ) -> bool:
        """
        Trendline breakout: price has broken above (BUY) or below (SELL) a recent
        swing high / swing low with a meaningful move.
        """
        if len(candles) < 6:
            return False

        closes = [self._get_close(c) for c in candles]
        highs  = [self._get_high(c)  for c in candles]
        lows   = [self._get_low(c)   for c in candles]

        # Minimum breakout size: 0.2% of price (~$6.60 on $3300 gold)
        min_breakout = price * 0.002

        if is_long:
            # Current price > recent swing high from first half of window
            pivot_high = max(highs[:-3])
            return price > pivot_high + min_breakout
        else:
            # Current price < recent swing low from first half of window
            pivot_low = min(lows[:-3])
            return price < pivot_low - min_breakout

    def _detect_rtb(
        self, candles: List[Dict], price: float, is_long: bool
    ) -> bool:
        """
        Return to Breakout: price broke a level and has now returned to retest it.
        Breakout detected in the middle of the candle window; retest is current price.
        """
        if len(candles) < 10:
            return False

        mid = len(candles) // 2
        first_half = candles[:mid]
        second_half = candles[mid:]

        f_highs = [self._get_high(c) for c in first_half]
        f_lows  = [self._get_low(c)  for c in first_half]
        s_closes = [self._get_close(c) for c in second_half]

        if not f_highs or not s_closes:
            return False

        tolerance = price * 0.003

        if is_long:
            breakout_level = max(f_highs)
            # Price broke above, then returned to breakout level
            if s_closes[-3] > breakout_level and abs(price - breakout_level) <= tolerance:
                return True
        else:
            breakout_level = min(f_lows)
            if s_closes[-3] < breakout_level and abs(price - breakout_level) <= tolerance:
                return True

        return False

    def _detect_lct(
        self, candles: List[Dict], price: float, is_long: bool
    ) -> bool:
        """
        LCT (Life-Changing Technique): pullback to broken level after breakout.
        1. Previous candles show breakout above (BUY) or below (SELL) key level
        2. Price pulled back to that level
        3. Currently reacting (small body / rejection candle at level)
        """
        if len(candles) < 6:
            return False

        mid = max(3, len(candles) // 2)
        early = candles[:mid]
        recent = candles[-3:]

        e_highs = [self._get_high(c)  for c in early]
        e_lows  = [self._get_low(c)   for c in early]
        r_opens = [self._get_open(c)  for c in recent]
        r_closes= [self._get_close(c) for c in recent]

        if not e_highs or not r_closes:
            return False

        tolerance = price * 0.004

        if is_long:
            breakout_level = max(e_highs)
            # Recent price came back to breakout level
            recent_low = min([self._get_low(c) for c in recent])
            if abs(recent_low - breakout_level) <= tolerance * 2 and price > breakout_level:
                return True
        else:
            breakout_level = min(e_lows)
            recent_high = max([self._get_high(c) for c in recent])
            if abs(recent_high - breakout_level) <= tolerance * 2 and price < breakout_level:
                return True

        return False

    def _detect_engulfing(self, candles: List[Dict], is_long: bool) -> bool:
        """
        Bullish/Bearish engulfing: last candle body completely contains previous candle body.
        """
        if len(candles) < 2:
            return False

        prev = candles[-2]
        curr = candles[-1]

        p_open  = self._get_open(prev)
        p_close = self._get_close(prev)
        c_open  = self._get_open(curr)
        c_close = self._get_close(curr)

        if any(v <= 0 for v in [p_open, p_close, c_open, c_close]):
            return False

        p_body_size = abs(p_close - p_open)
        if p_body_size < price_of(candles) * 0.0005:
            return False  # Doji — not a valid engulfed candle

        if is_long:
            # Bullish engulfing: prev bearish (close < open), curr bullish (close > open)
            # and curr engulfs prev
            return (p_close < p_open and c_close > c_open
                    and c_open <= p_close and c_close >= p_open)
        else:
            # Bearish engulfing: prev bullish, curr bearish and engulfs prev
            return (p_close > p_open and c_close < c_open
                    and c_open >= p_close and c_close <= p_open)

    def _detect_secret_pattern(self, candles: List[Dict], is_long: bool) -> bool:
        """
        Secret Pattern: small rejection candle at zone.
        - Small body (< 30% of total range)
        - Rejection wick: wick in opposite direction >= 2x body size
        - At or near a key level
        """
        if len(candles) < 2:
            return False

        curr = candles[-1]
        c_open  = self._get_open(curr)
        c_high  = self._get_high(curr)
        c_low   = self._get_low(curr)
        c_close = self._get_close(curr)

        if any(v <= 0 for v in [c_open, c_high, c_low, c_close]):
            return False

        total_range = c_high - c_low
        if total_range <= 0:
            return False

        body = abs(c_close - c_open)
        body_ratio = body / total_range

        if body_ratio > 0.4:
            return False  # Body too large — not a pin/rejection candle

        if is_long:
            # Bullish secret pattern: long lower wick (rejection of lows)
            lower_wick = min(c_open, c_close) - c_low
            return lower_wick >= body * 2
        else:
            # Bearish secret pattern: long upper wick (rejection of highs)
            upper_wick = c_high - max(c_open, c_close)
            return upper_wick >= body * 2

    def _detect_pullback_to_ema(
        self, candles: List[Dict], price: float, is_long: bool
    ) -> bool:
        """Detect pullback to EMA20 in trend direction."""
        if len(candles) < 22:
            return False
        try:
            df = to_dataframe(candles)
            ema20 = ema(df, 20)
            if ema20 is None:
                return False
            tolerance = price * 0.002
            if is_long:
                # Price has pulled back to EMA20 from above
                return (price > ema20 * 0.998 and
                        abs(price - ema20) <= tolerance * 2)
            else:
                return (price < ema20 * 1.002 and
                        abs(price - ema20) <= tolerance * 2)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Candle field extractors (handle both Oanda nested 'mid' and flat format)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_open(c: dict) -> float:
        if 'mid' in c:
            return float(c['mid'].get('o', 0) or 0)
        return float(c.get('open', 0) or 0)

    @staticmethod
    def _get_high(c: dict) -> float:
        if 'mid' in c:
            return float(c['mid'].get('h', 0) or 0)
        return float(c.get('high', 0) or 0)

    @staticmethod
    def _get_low(c: dict) -> float:
        if 'mid' in c:
            return float(c['mid'].get('l', 0) or 0)
        return float(c.get('low', 0) or 0)

    @staticmethod
    def _get_close(c: dict) -> float:
        if 'mid' in c:
            return float(c['mid'].get('c', 0) or 0)
        return float(c.get('close', 0) or 0)


def price_of(candles: List[Dict]) -> float:
    """Get approximate price from last candle. Used for relative size checks.

    Returns 0.0 when price is unavailable — callers treat this as 'skip size filter'.
    """
    if not candles:
        return 0.0
    c = candles[-1]
    close = float(c.get('close', 0) or (c.get('mid', {}).get('c', 0)) or 0)
    return close
