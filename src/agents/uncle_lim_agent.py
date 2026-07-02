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
from config.settings import settings


@dataclass
class UncleLimAnalysis:
    """Structured result from the Uncle Lim multi-TF analysis."""
    h4_bias: str = "neutral"           # bullish / bearish / neutral
    h4_quality: str = "weak"           # strong (EMA stack + ADX) / weak
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
                "h4_quality": result.h4_quality,
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
            indicators['uncle_lim_h4_quality'] = meta.get('h4_quality', 'weak')
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

        # Step 1: H4 bias — primary trend direction + quality (EMA stack + ADX)
        h4_bias, h4_strong = self._get_h4_bias(h4_candles)
        result.h4_bias = h4_bias
        result.h4_quality = "strong" if h4_strong else "weak"

        if h4_bias == "neutral":
            return result  # No H4 trend = no trade

        is_long = h4_bias == "bullish"

        # Dedup tolerance ~1 execution-TF ATR (H1 always has enough candles).
        dedup_tol = (self._safe_atr(candles) or price * 0.001) * settings.UNCLE_LIM_DEDUP_ATR_MULT

        collected: List = []      # List[Tuple[label, Optional[level]]]
        levels_seen: List[float] = []

        # Step 2: H1 trigger — SND zone / trendline breakout / RTB
        h1_trigger, h1_label, h1_level = self._get_h1_trigger(candles, price, is_long)
        result.h1_trigger = h1_trigger
        if h1_label:
            collected.append((h1_label, h1_level))
            if h1_level is not None:
                levels_seen.append(h1_level)
            if "SND" in h1_label.upper():
                result.setup_type = "SND_ZONE"
            elif "TRENDLINE" in h1_label.upper() or "BREAKOUT" in h1_label.upper():
                result.setup_type = "TRENDLINE_BREAKOUT"
            elif "RTB" in h1_label.upper():
                result.setup_type = "RTB"

        # Step 3: M30 confirmation — LCT / engulfing / SND
        m30_type, m30_label, m30_level = self._get_m30_confirmation(m30_candles, price, is_long)
        result.m30_confirmation = m30_type
        if m30_label:
            collected.append((m30_label, m30_level))
            if m30_level is not None:
                levels_seen.append(m30_level)
            if result.setup_type == "NONE" and "LCT" in m30_label.upper():
                result.setup_type = "LCT"
            elif result.setup_type == "NONE" and "PULLBACK" in m30_label.upper():
                result.setup_type = "PULLBACK"

        # Step 4: M15 confirmation — LCT / engulfing / SND
        m15_type, m15_label, m15_level = self._get_m15_confirmation(m15_candles, price, is_long)
        result.m15_confirmation = m15_type
        if m15_label:
            collected.append((m15_label, m15_level))
            if m15_level is not None:
                levels_seen.append(m15_level)
            if result.setup_type == "NONE" and "LCT" in m15_label.upper():
                result.setup_type = "LCT"

        # Step 5: M5/M1 final trigger — Secret Pattern (key-level checked) / SND
        m5_trigger, m5_label, m5_level = self._get_m5_trigger(
            m5_candles, price, is_long, key_levels=levels_seen
        )
        result.m5_trigger = m5_trigger
        if m5_label:
            collected.append((m5_label, m5_level))

        # Step 6: H4 counts as a confirmation ONLY when strong (EMA stack + ADX).
        # Weak H4 still set the bias/gate above but no longer earns a free +1.
        if h4_strong:
            collected.insert(0, (f"H4 {h4_bias.upper()} structure", None))

        # Step 7: Collapse confirmations that reference the same price level.
        result.confirmations = self._dedup_confirmations(collected, dedup_tol)

        self.logger.debug(
            f"UncLeLim {pair}: h4_quality={result.h4_quality} "
            f"collected={[c[0] for c in collected]} "
            f"deduped={result.confirmations} dedup_tol={dedup_tol:.4f}"
        )

        # Step 8: Check minimum confirmations
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

    def _get_h4_bias(self, h4_candles: List[Dict]):
        """Determine H4 trend bias and quality: returns (bias, strong).

        bias: bullish / bearish / neutral.
        strong: True only when the EMA stack is fully aligned AND
        ADX >= UNCLE_LIM_H4_MIN_ADX. Structure-only bullishness and a missing
        ADX both fail closed (strong=False), so H4 earns a counted confirmation
        only in a genuine, trending regime.

        BUY-side guardrail (gold bull market): bullish requires ema20>ema50
        AND ema50>ema200 (or ema200 unavailable, fallback to structure check).
        Bearish reverts to permissive EMA bias.
        """
        if len(h4_candles) < 20:
            return "neutral", False
        try:
            df = to_dataframe(h4_candles)
            ema20 = ema(df, 20)
            ema50 = ema(df, 50)
            ema200 = ema(df, 200)  # None if < 205 candles
            if ema20 is None or ema50 is None:
                return "neutral", False

            adx_val = adx(df, 14)
            strong_adx = adx_val is not None and adx_val >= settings.UNCLE_LIM_H4_MIN_ADX

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
                stack_aligned = ema200 is None or ema50 > ema200
                if stack_aligned:
                    return "bullish", strong_adx
                if structure_ok_bullish:
                    return "bullish", False  # structure-only => never strong
                return "neutral", False

            # BEARISH — permissive EMA bias
            stack_aligned = ema200 is None or ema50 < ema200
            return "bearish", (stack_aligned and strong_adx)
        except Exception as exc:
            self.logger.debug(f"_get_h4_bias failed: {exc}")
            return "neutral", False

    # ------------------------------------------------------------------
    # H1 trigger detection
    # ------------------------------------------------------------------

    def _get_h1_trigger(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Return (trigger_type, confirmation_label, level) for H1."""
        if len(candles) < 10:
            return "none", None, None

        # Tolerances computed once on the FULL H1 array (>=19 => real ATR).
        snd_tol = self._tf_tolerance(
            candles, price,
            settings.UNCLE_LIM_SND_PCT_FLOOR, settings.UNCLE_LIM_SND_ATR_MULT,
        )
        bo_tol = self._tf_tolerance(
            candles, price,
            settings.UNCLE_LIM_BREAKOUT_PCT_FLOOR, settings.UNCLE_LIM_BREAKOUT_ATR_MULT,
        )

        # 1. Check for SND zone at H1
        ok, level = self._detect_snd_zone(candles[-20:], price, is_long, tol=snd_tol)
        if ok:
            label = f"SND Zone H1 ({'demand' if is_long else 'supply'})"
            return "snd_zone", label, level

        # 2. Check for trendline breakout at H1
        ok, level = self._detect_trendline_breakout(candles[-15:], price, is_long, tol=bo_tol)
        if ok:
            return "trendline_breakout", "Trendline Breakout H1", level

        # 3. Check for RTB (return to breakout)
        ok, level = self._detect_rtb(candles[-20:], price, is_long, tol=snd_tol)
        if ok:
            return "rtb", "RTB H1 (return to breakout)", level

        return "none", None, None

    # ------------------------------------------------------------------
    # M30 confirmation
    # ------------------------------------------------------------------

    def _get_m30_confirmation(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Return (conf_type, label, level) for M30."""
        if len(candles) < 5:
            return "none", None, None

        # Tolerances computed once on the FULL M30 array (>=19 => real ATR).
        lct_tol = self._tf_tolerance(
            candles, price,
            settings.UNCLE_LIM_LCT_PCT_FLOOR, settings.UNCLE_LIM_LCT_ATR_MULT,
        )
        snd_tol = self._tf_tolerance(
            candles, price,
            settings.UNCLE_LIM_SND_PCT_FLOOR, settings.UNCLE_LIM_SND_ATR_MULT,
        )

        # LCT: pullback after breakout to the broken level
        ok, level = self._detect_lct(candles[-15:], price, is_long, tol=lct_tol)
        if ok:
            return "lct", "LCT M30 (pullback/retest)", level

        # Engulfing candle (level synthesized from body midpoint for dedup)
        eng = self._detect_engulfing(candles[-5:], is_long, ref_price=price)
        if eng:
            return "engulfing", f"{'Bullish' if is_long else 'Bearish'} Engulfing M30", self._body_mid(candles[-1])

        # SND zone at M30
        ok, level = self._detect_snd_zone(candles[-10:], price, is_long, tol=snd_tol)
        if ok:
            return "snd_zone", "SND Zone M30", level

        # Pullback to EMA
        ok, level = self._detect_pullback_to_ema(candles[-10:], price, is_long)
        if ok:
            return "pullback", "Pullback M30", level

        return "none", None, None

    # ------------------------------------------------------------------
    # M15 confirmation
    # ------------------------------------------------------------------

    def _get_m15_confirmation(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Return (conf_type, label, level) for M15."""
        if len(candles) < 5:
            return "none", None, None

        # Tolerances computed once on the FULL M15 array (>=19 => real ATR).
        lct_tol = self._tf_tolerance(
            candles, price,
            settings.UNCLE_LIM_LCT_PCT_FLOOR, settings.UNCLE_LIM_LCT_ATR_MULT,
        )
        snd_tol = self._tf_tolerance(
            candles, price,
            settings.UNCLE_LIM_SND_PCT_FLOOR, settings.UNCLE_LIM_SND_ATR_MULT,
        )

        ok, level = self._detect_lct(candles[-10:], price, is_long, tol=lct_tol)
        if ok:
            return "lct", "LCT M15 (retest)", level

        eng = self._detect_engulfing(candles[-4:], is_long, ref_price=price)
        if eng:
            return "engulfing", f"{'Bullish' if is_long else 'Bearish'} Engulfing M15", self._body_mid(candles[-1])

        ok, level = self._detect_snd_zone(candles[-8:], price, is_long, tol=snd_tol)
        if ok:
            return "snd_zone", "SND Zone M15", level

        return "none", None, None

    # ------------------------------------------------------------------
    # M5 final trigger
    # ------------------------------------------------------------------

    def _get_m5_trigger(
        self, candles: List[Dict], price: float, is_long: bool,
        key_levels: Optional[List[float]] = None,
    ):
        """Return (trigger_type, label, level) for M5/M1. The secret pattern is
        checked against collected higher-TF levels (key_levels)."""
        if len(candles) < 3:
            return "none", None, None

        tol = self._tf_tolerance(
            candles, price,
            settings.UNCLE_LIM_SND_PCT_FLOOR, settings.UNCLE_LIM_SND_ATR_MULT,
        )
        secret = self._detect_secret_pattern(
            candles[-5:], is_long, ref_price=price, key_levels=key_levels, tol=tol
        )
        if secret:
            return "secret_pattern", "Secret Pattern M5", self._body_mid(candles[-1])

        ok, level = self._detect_snd_zone(candles[-5:], price, is_long, tol=tol)
        if ok:
            return "snd_zone", "SND Zone M5", level

        return "none", None, None

    # ------------------------------------------------------------------
    # Volatility / dedup helpers
    # ------------------------------------------------------------------

    def _safe_atr(self, candles: List[Dict]) -> Optional[float]:
        """ATR(14) or None if <19 candles / bad data. Never raises."""
        try:
            df = to_dataframe(candles)
            if df is None or len(df) < 19:
                return None
            return atr(df, 14)
        except Exception as exc:
            self.logger.debug(f"_safe_atr failed: {exc}")
            return None

    def _tf_tolerance(
        self, candles: List[Dict], ref_price: float, pct: float, atr_mult: float
    ) -> float:
        """Volatility-aware tolerance: max(pct*price, atr_mult*ATR).

        Falls back to the pct-of-price floor when ATR is unavailable (thin TF).
        Epsilon-guarded so it is never zero.
        """
        floor = pct * ref_price
        a = self._safe_atr(candles)
        tol = max(floor, atr_mult * a) if a is not None else floor
        return tol if tol > 0 else max(abs(ref_price) * 1e-6, 1e-6)

    def _reaction_ok(
        self, candles: List[Dict], level: float, is_long: bool, tol: float
    ) -> bool:
        """True if the LAST candle shows a genuine rejection at `level`.

        BUY: low within tol of level AND a lower-wick rejection that closes back
        up (hammer-ish). SELL: mirror with the upper wick. False on a degenerate
        (range<=0) candle. This is what upgrades "price is near a level" into
        "price reacted at a level".
        """
        if not candles:
            return False
        c = candles[-1]
        o = self._get_open(c)
        h = self._get_high(c)
        lo = self._get_low(c)
        cl = self._get_close(c)
        if any(v <= 0 for v in (o, h, lo, cl)) or (h - lo) <= 0:
            return False
        body = abs(cl - o)
        mult = settings.UNCLE_LIM_REACTION_WICK_MULT
        if is_long:
            if abs(lo - level) > tol:
                return False
            lower_wick = min(o, cl) - lo
            return lower_wick > 0 and lower_wick >= body * mult and cl >= o
        else:
            if abs(h - level) > tol:
                return False
            upper_wick = h - max(o, cl)
            return upper_wick > 0 and upper_wick >= body * mult and cl <= o

    def _dedup_confirmations(self, items: List, tol: float) -> List[str]:
        """Collapse confirmations that reference the same price level.

        items: List[Tuple[label, Optional[level]]]. Entries whose level is within
        tol of an already-kept level are dropped (cross-TF double-count). A None
        level is always kept. Order preserved. Returns surviving labels.
        """
        kept_labels: List[str] = []
        kept_levels: List[float] = []
        for label, level in items:
            if level is None:
                kept_labels.append(label)
                continue
            if any(abs(level - kl) <= tol for kl in kept_levels):
                continue
            kept_labels.append(label)
            kept_levels.append(level)
        return kept_labels

    def _body_mid(self, candle: dict) -> Optional[float]:
        """Body midpoint of a candle — a synthetic level for wickless patterns
        (engulfing / secret) so they still participate in dedup."""
        o = self._get_open(candle)
        cl = self._get_close(candle)
        if o <= 0 or cl <= 0:
            return None
        return (o + cl) / 2

    # ------------------------------------------------------------------
    # Pattern detection helpers
    # ------------------------------------------------------------------

    def _detect_snd_zone(
        self, candles: List[Dict], price: float, is_long: bool, tol: Optional[float] = None
    ):
        """
        Supply/Demand zone: price is at or near a previous reaction level AND the
        last candle actually rejects off it. Returns (matched, level).
        Demand (for BUY): price near a prior swing-low / broken-resistance level.
        Supply (for SELL): price near a prior swing-high / broken-support level.

        Volatility-aware: tolerance = max(pct-floor, atr_mult*ATR). Requires
        _reaction_ok so proximity alone is not enough.
        """
        if len(candles) < 6:
            return False, None

        closes = [self._get_close(c) for c in candles]
        highs  = [self._get_high(c)  for c in candles]
        lows   = [self._get_low(c)   for c in candles]

        if not all(v > 0 for v in closes + highs + lows):
            return False, None

        if tol is None:
            tol = self._tf_tolerance(
                candles, price,
                settings.UNCLE_LIM_SND_PCT_FLOOR, settings.UNCLE_LIM_SND_ATR_MULT,
            )

        if is_long:
            # Demand zone: near a previous swing low cluster, reacting up
            recent_lows = sorted(lows[:-2])[:3]  # 3 lowest lows from history
            for level in recent_lows:
                if abs(price - level) <= tol and self._reaction_ok(candles, level, True, tol):
                    return True, level
            # Or: returned to a broken resistance (resistance -> support)
            prev_highs = highs[:-3]
            if prev_highs:
                prev_resistance = max(prev_highs[-3:]) if len(prev_highs) >= 3 else max(prev_highs)
                band = tol * 1.5
                if abs(price - prev_resistance) <= band and self._reaction_ok(candles, prev_resistance, True, band):
                    return True, prev_resistance
        else:
            # Supply zone: near a previous swing high cluster, reacting down
            recent_highs = sorted(highs[:-2], reverse=True)[:3]
            for level in recent_highs:
                if abs(price - level) <= tol and self._reaction_ok(candles, level, False, tol):
                    return True, level
            prev_lows = lows[:-3]
            if prev_lows:
                prev_support = min(prev_lows[-3:]) if len(prev_lows) >= 3 else min(prev_lows)
                band = tol * 1.5
                if abs(price - prev_support) <= band and self._reaction_ok(candles, prev_support, False, band):
                    return True, prev_support

        return False, None

    def _detect_trendline_breakout(
        self, candles: List[Dict], price: float, is_long: bool, tol: Optional[float] = None
    ):
        """
        Trendline breakout: the last CLOSE (not a live wick/spread spike) has
        broken above (BUY) or below (SELL) a recent swing pivot by at least a
        volatility-aware margin. Returns (matched, pivot_level).
        """
        if len(candles) < 6:
            return False, None

        highs  = [self._get_high(c)  for c in candles]
        lows   = [self._get_low(c)   for c in candles]

        if tol is None:
            tol = self._tf_tolerance(
                candles, price,
                settings.UNCLE_LIM_BREAKOUT_PCT_FLOOR, settings.UNCLE_LIM_BREAKOUT_ATR_MULT,
            )

        # Confirm on the closed candle, not the live price.
        last_close = self._get_close(candles[-1])
        if last_close <= 0:
            return False, None

        if is_long:
            pivot_high = max(highs[:-3])
            if last_close > pivot_high + tol:
                return True, pivot_high
        else:
            pivot_low = min(lows[:-3])
            if last_close < pivot_low - tol:
                return True, pivot_low
        return False, None

    def _detect_rtb(
        self, candles: List[Dict], price: float, is_long: bool, tol: Optional[float] = None
    ):
        """
        Return to Breakout: price broke a level (first half of window) and has now
        returned to retest it. Returns (matched, breakout_level). Volatility-aware
        tolerance (reuses the SND floor/mult).
        """
        if len(candles) < 10:
            return False, None

        mid = len(candles) // 2
        first_half = candles[:mid]
        second_half = candles[mid:]

        f_highs = [self._get_high(c) for c in first_half]
        f_lows  = [self._get_low(c)  for c in first_half]
        s_closes = [self._get_close(c) for c in second_half]

        if not f_highs or not s_closes:
            return False, None

        if tol is None:
            tol = self._tf_tolerance(
                candles, price,
                settings.UNCLE_LIM_SND_PCT_FLOOR, settings.UNCLE_LIM_SND_ATR_MULT,
            )

        if is_long:
            breakout_level = max(f_highs)
            # Price broke above, then returned to breakout level
            if s_closes[-3] > breakout_level and abs(price - breakout_level) <= tol:
                return True, breakout_level
        else:
            breakout_level = min(f_lows)
            if s_closes[-3] < breakout_level and abs(price - breakout_level) <= tol:
                return True, breakout_level

        return False, None

    def _detect_lct(
        self, candles: List[Dict], price: float, is_long: bool, tol: Optional[float] = None
    ):
        """
        LCT (Life-Changing Technique): pullback to a broken level after breakout,
        with price holding on the correct side. Returns (matched, breakout_level).
        Retest band is volatility-aware (LCT floor/mult) instead of a fixed ~$26.
        """
        if len(candles) < 6:
            return False, None

        mid = max(3, len(candles) // 2)
        early = candles[:mid]
        recent = candles[-3:]

        e_highs = [self._get_high(c)  for c in early]
        e_lows  = [self._get_low(c)   for c in early]
        r_closes= [self._get_close(c) for c in recent]

        if not e_highs or not r_closes:
            return False, None

        if tol is None:
            tol = self._tf_tolerance(
                candles, price,
                settings.UNCLE_LIM_LCT_PCT_FLOOR, settings.UNCLE_LIM_LCT_ATR_MULT,
            )

        if is_long:
            breakout_level = max(e_highs)
            recent_low = min([self._get_low(c) for c in recent])
            if abs(recent_low - breakout_level) <= tol and price > breakout_level:
                return True, breakout_level
        else:
            breakout_level = min(e_lows)
            recent_high = max([self._get_high(c) for c in recent])
            if abs(recent_high - breakout_level) <= tol and price < breakout_level:
                return True, breakout_level

        return False, None

    def _detect_engulfing(self, candles: List[Dict], is_long: bool, ref_price: float = 0.0) -> bool:
        """
        Bullish/Bearish engulfing: last candle body completely contains previous
        candle body. Doji filter FAILS CLOSED: if the reference price is missing
        or the prior body is smaller than DOJI_BODY_PCT of price, reject.
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
        ref = ref_price or self._get_close(curr)
        if ref <= 0 or p_body_size < ref * settings.UNCLE_LIM_DOJI_BODY_PCT:
            return False  # Doji / no reference — not a valid engulfed candle

        if is_long:
            # Bullish engulfing: prev bearish (close < open), curr bullish (close > open)
            # and curr engulfs prev
            return (p_close < p_open and c_close > c_open
                    and c_open <= p_close and c_close >= p_open)
        else:
            # Bearish engulfing: prev bullish, curr bearish and engulfs prev
            return (p_close > p_open and c_close < c_open
                    and c_open >= p_close and c_close <= p_open)

    def _detect_secret_pattern(
        self,
        candles: List[Dict],
        is_long: bool,
        ref_price: float = 0.0,
        key_levels: Optional[List[float]] = None,
        tol: Optional[float] = None,
    ) -> bool:
        """
        Secret Pattern: small rejection (pin) candle at a key level.
        - Small body (<= PIN_BODY_RATIO of total range)
        - Rejection wick opposite the trade direction >= PIN_WICK_MULT x body
        - Pin's extreme wick within tol of a collected key level. When no key
          levels are supplied (thin M5), fall back to shape-only (avoid
          over-tightening on the noisiest TF).
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

        if body_ratio > settings.UNCLE_LIM_PIN_BODY_RATIO:
            return False  # Body too large — not a pin/rejection candle

        if is_long:
            # Bullish secret pattern: long lower wick (rejection of lows)
            lower_wick = min(c_open, c_close) - c_low
            if lower_wick < body * settings.UNCLE_LIM_PIN_WICK_MULT:
                return False
            extreme = c_low
        else:
            # Bearish secret pattern: long upper wick (rejection of highs)
            upper_wick = c_high - max(c_open, c_close)
            if upper_wick < body * settings.UNCLE_LIM_PIN_WICK_MULT:
                return False
            extreme = c_high

        # Key-level check: the pin must reject AT a level we already care about.
        if key_levels and tol is not None:
            return any(abs(extreme - lvl) <= tol for lvl in key_levels)
        return True

    def _detect_pullback_to_ema(
        self, candles: List[Dict], price: float, is_long: bool
    ):
        """Detect pullback to EMA20 in trend direction. Returns (matched, ema20).
        Volatility-aware band (reuses the SND floor/mult)."""
        if len(candles) < 22:
            return False, None
        try:
            df = to_dataframe(candles)
            ema20 = ema(df, 20)
            if ema20 is None:
                return False, None
            tol = self._tf_tolerance(
                candles, price,
                settings.UNCLE_LIM_SND_PCT_FLOOR, settings.UNCLE_LIM_SND_ATR_MULT,
            )
            if is_long:
                # Price has pulled back to EMA20 from above
                if price > ema20 * 0.998 and abs(price - ema20) <= tol:
                    return True, ema20
            else:
                if price < ema20 * 1.002 and abs(price - ema20) <= tol:
                    return True, ema20
            return False, None
        except Exception as exc:
            self.logger.debug(f"_detect_pullback_to_ema failed: {exc}")
            return False, None

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
