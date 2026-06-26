"""LLMAgent — Groq (primary) + Anthropic (fallback) analyst for XAU/USD.

Gold-specific adaptation:
  - System prompt tuned for Uncle Lim strategy and XAU/USD macro drivers
  - Setup types: SND_ZONE / TRENDLINE_BREAKOUT / LCT / RTB / PULLBACK / NONE
  - Macro context: DXY inverse correlation, real yields, Fed tone, geopolitical risk
  - Loads sample Uncle Lim analysis context from goldmapping_corpus.jsonl for RAG

Provider priority:
  1. Groq llama-3.3-70b-versatile (primary)
  2. Anthropic Claude Haiku (fallback on credit exhaustion)
  3. HOLD when all providers are exhausted
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from openai import OpenAI

from .base import AgentVote, Signal
from ._llm_utils import _is_credit_exhausted
from config.settings import settings
_last_call_time: float = 0.0
_call_time_lock = threading.Lock()

_GOLD_SETUP_TYPES = {"SND_ZONE", "TRENDLINE_BREAKOUT", "LCT", "RTB", "PULLBACK", "NONE"}

_SYSTEM_PROMPT = (
    "You are an elite gold trading signal analyst specialising in XAU/USD.\n"
    "You are trained on Uncle Lim's multi-timeframe confluence strategy:\n"
    "  - H4→H1→M30/M15→M5/M1 top-down analysis\n"
    "  - SND (Supply & Demand) zones as primary entry concept\n"
    "  - LCT (Life-Changing Technique): pullback/retest after breakout\n"
    "  - Secret Pattern: small rejection candle at zone as final trigger\n"
    "  - RTB (Return to Breakout): price returns to broken level\n"
    "  - Minimum 3 timeframe confirmations required\n\n"
    "Gold macro drivers (inverse correlations):\n"
    "  - DXY rising = bearish gold pressure\n"
    "  - Real yields rising = bearish gold pressure\n"
    "  - Fed hawkish = bearish gold; dovish = bullish gold\n"
    "  - Geopolitical risk = bullish gold (safe-haven)\n"
    "  - Risk-off / equity selloff = bullish gold\n\n"
    "BUY bias enforced in uptrend (gold bull market). SELL only on confirmed H4 breakdown.\n\n"
    "Respond with valid JSON only — no markdown, no preamble:\n"
    '{"vote": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reasoning": "max 120 chars", '
    '"setup_type": "SND_ZONE|TRENDLINE_BREAKOUT|LCT|RTB|PULLBACK|NONE"}\n\n'
    "Only vote BUY or SELL if confidence > 0.55 AND Uncle Lim agent already signalled the same direction.\n"
    "setup_type must be NONE if vote is HOLD."
)

# Load goldmapping RAG samples (first N posts for context injection)
_GOLDMAPPING_SAMPLES: List[str] = []
_goldmapping_loaded = False
_goldmapping_lock = threading.Lock()


def _load_goldmapping_samples(n: int = 5) -> List[str]:
    """Load sample Uncle Lim analysis posts from goldmapping_corpus.jsonl."""
    global _GOLDMAPPING_SAMPLES, _goldmapping_loaded
    with _goldmapping_lock:
        if _goldmapping_loaded:
            return _GOLDMAPPING_SAMPLES

        corpus_path = Path(__file__).parent.parent.parent / "output" / "goldmapping_corpus.jsonl"
        if not corpus_path.exists():
            _goldmapping_loaded = True
            return _GOLDMAPPING_SAMPLES

        samples = []
        try:
            with open(corpus_path, encoding='utf-8') as f:
                for i, line in enumerate(f):
                    if i >= n:
                        break
                    try:
                        item = json.loads(line.strip())
                        text = item.get('text') or item.get('content') or item.get('message', '')
                        if text:
                            samples.append(text[:300])
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

        _GOLDMAPPING_SAMPLES = samples
        _goldmapping_loaded = True
        return _GOLDMAPPING_SAMPLES


class LLMAgent:
    """Gold analyst: Groq primary, Anthropic fallback on credit exhaustion."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("LLMAgent")

        self._groq_client: Optional[OpenAI] = None
        self._groq_model: str = ""
        self._groq_exhausted: bool = False

        self._anthropic_client = None
        self._anthropic_model: str = ""
        self._anthropic_exhausted: bool = False

        self._init_groq()
        self._init_anthropic()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_groq(self) -> None:
        try:
            from config.settings import settings
            if not settings.GROQ_API_KEY:
                self.logger.warning("GROQ_API_KEY not set — Groq provider disabled")
                self._groq_exhausted = True
                return
            self._groq_client = OpenAI(
                api_key=settings.GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
            self._groq_model = settings.LLM_MODEL
            self.logger.info(f"LLMAgent: Groq initialised ({self._groq_model})")
        except ImportError:
            self.logger.warning("openai package not installed — Groq provider disabled")
            self._groq_exhausted = True
        except Exception as exc:
            self.logger.warning(f"LLMAgent: Groq init failed: {exc}")
            self._groq_exhausted = True

    def _init_anthropic(self) -> None:
        try:
            from config.settings import settings
            if not settings.ANTHROPIC_API_KEY:
                self.logger.info("ANTHROPIC_API_KEY not set — Anthropic fallback disabled")
                self._anthropic_exhausted = True
                return
            import anthropic as _anthropic_sdk
            self._anthropic_client = _anthropic_sdk.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY
            )
            self._anthropic_model = settings.ANTHROPIC_LLM_MODEL
            self.logger.info(f"LLMAgent: Anthropic fallback ready ({self._anthropic_model})")
        except ImportError:
            self.logger.info("anthropic package not installed — Anthropic fallback disabled")
            self._anthropic_exhausted = True
        except Exception as exc:
            self.logger.warning(f"LLMAgent: Anthropic init failed: {exc}")
            self._anthropic_exhausted = True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return not (self._groq_exhausted and self._anthropic_exhausted)

    @property
    def both_exhausted(self) -> bool:
        return self._groq_exhausted and self._anthropic_exhausted

    @property
    def active_provider(self) -> str:
        if not self._groq_exhausted:
            return "groq"
        if not self._anthropic_exhausted:
            return "anthropic"
        return "none"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def vote(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: dict,
        macro_context: Optional[dict] = None,
        htf_candles: Optional[dict] = None,
    ) -> AgentVote:
        """Generate a synthesizer vote. Always returns an AgentVote — never raises."""
        if self.both_exhausted:
            return AgentVote(
                agent_name="LLMAgent",
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="All LLM providers exhausted",
            )
        try:
            return self._vote(pair, candles, price, indicators, macro_context, htf_candles)
        except Exception as exc:
            self.logger.warning(f"LLMAgent vote failed for {pair}: {exc}")
            return AgentVote(
                agent_name="LLMAgent",
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="LLM call failed",
            )

    # ------------------------------------------------------------------
    # Internal logic
    # ------------------------------------------------------------------

    def _vote(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: dict,
        macro_context: Optional[dict] = None,
        htf_candles: Optional[dict] = None,
    ) -> AgentVote:
        global _last_call_time

        with _call_time_lock:
            elapsed = time.time() - _last_call_time
            wait = max(0.0, settings.LLM_MIN_CALL_SPACING_SECONDS - elapsed)
            _last_call_time = time.time() + wait

        if wait > 0.0:
            time.sleep(wait)

        user_msg = _build_analyst_message(pair, candles, price, indicators, macro_context, htf_candles)

        if not self._groq_exhausted and self._groq_client is not None:
            try:
                return self._call_groq(user_msg, pair)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(f"LLMAgent: Groq credits exhausted — switching to Anthropic. ({exc})")
                    self._groq_exhausted = True
                else:
                    self.logger.warning(f"LLMAgent: Groq transient error for {pair}: {exc}")
                    return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "Groq transient error")

        if not self._anthropic_exhausted and self._anthropic_client is not None:
            try:
                return self._call_anthropic(user_msg, pair)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(f"LLMAgent: Anthropic credits exhausted — all providers down. ({exc})")
                    self._anthropic_exhausted = True
                else:
                    self.logger.warning(f"LLMAgent: Anthropic transient error for {pair}: {exc}")
                    return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "Anthropic transient error")

        return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "All LLM providers exhausted")

    def _call_groq(self, user_msg: str, pair: str) -> AgentVote:
        response = self._groq_client.chat.completions.create(
            model=self._groq_model,
            max_tokens=settings.LLM_MAX_TOKENS,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        if not response.choices:
            return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "Empty Groq response")
        return _parse_response(response.choices[0].message.content.strip(), pair)

    def _call_anthropic(self, user_msg: str, pair: str) -> AgentVote:
        response = self._anthropic_client.messages.create(
            model=self._anthropic_model,
            max_tokens=settings.LLM_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        if not response.content:
            return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "Empty Anthropic response")
        return _parse_response(response.content[0].text.strip(), pair)


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_analyst_message(
    pair: str,
    candles: List[Dict],
    price: float,
    indicators: dict,
    macro_context: Optional[dict] = None,
    htf_candles: Optional[dict] = None,
) -> str:
    """Build gold-specific LLM analyst message."""
    recent = candles[-10:] if len(candles) >= 10 else candles
    candle_lines = []
    for c in recent:
        if 'mid' in c:
            mid = c['mid']
            o, h, l, cl = mid.get('o','?'), mid.get('h','?'), mid.get('l','?'), mid.get('c','?')
        else:
            o = c.get('open', '?')
            h = c.get('high', '?')
            l = c.get('low', '?')
            cl = c.get('close', '?')
        candle_lines.append(f"  O={o} H={h} L={l} C={cl}")
    candle_table = "\n".join(candle_lines)

    ind_lines = []
    for key, val in indicators.items():
        if isinstance(val, float):
            ind_lines.append(f"  {key}: {val:.4f}")
        else:
            ind_lines.append(f"  {key}: {val}")
    ind_summary = "\n".join(ind_lines) if ind_lines else "  (none)"

    msg = (
        f"Instrument: {pair} (XAU/USD — Gold)\n"
        f"Current price: {price:.2f} USD/oz\n\n"
        f"Last 10 candles (H1):\n{candle_table}\n\n"
        f"Technical + Uncle Lim indicators:\n{ind_summary}\n\n"
    )

    if macro_context:
        macro_lines = []
        if macro_context.get('fed_rate'):
            macro_lines.append(f"  Fed Funds Rate: {macro_context['fed_rate']}%")
        if macro_context.get('real_yield'):
            signal = macro_context.get('real_yield_signal', 'NEUTRAL')
            macro_lines.append(f"  US Real Yield: {macro_context['real_yield']} ({signal})")
        if macro_context.get('fed_tone'):
            macro_lines.append(f"  Fed tone: {macro_context['fed_tone']}")
        if macro_context.get('dxy_note'):
            macro_lines.append(f"  DXY note: {macro_context['dxy_note']}")
        if macro_context.get('recent_news'):
            macro_lines.append(f"  Recent gold/USD news:\n{macro_context['recent_news']}")
        if macro_context.get('upcoming_events'):
            macro_lines.append(f"  Upcoming events:\n{macro_context['upcoming_events']}")
        if macro_lines:
            msg += "Gold macro context:\n" + "\n".join(macro_lines) + "\n\n"

    if htf_candles:
        htf_lines = ["Multi-timeframe context:"]
        from .indicators import ema as _ema, adx as _adx, to_dataframe as _to_df
        for tf_label, tf_clist in htf_candles.items():
            if not tf_clist:
                continue
            try:
                tf_df = _to_df(tf_clist)
                tf_ema20 = _ema(tf_df, 20)
                tf_ema50 = _ema(tf_df, 50)
                tf_adx   = _adx(tf_df, 14)
                if tf_ema20 is not None and tf_ema50 is not None:
                    trend_str = "bullish" if tf_ema20 > tf_ema50 else "bearish"
                    adx_str   = f" | ADX={tf_adx:.1f}" if tf_adx is not None else ""
                    htf_lines.append(
                        f"  {tf_label} trend: {trend_str}"
                        f" | EMA20={tf_ema20:.2f} EMA50={tf_ema50:.2f}{adx_str}"
                    )
            except Exception:
                pass
        if len(htf_lines) > 1:
            msg += "\n".join(htf_lines) + "\n\n"

    # RAG: inject Uncle Lim sample posts for grounding
    samples = _load_goldmapping_samples(settings.LLM_RAG_SAMPLE_COUNT)
    if samples:
        sample_text = "\n---\n".join(samples[:settings.LLM_RAG_SAMPLE_COUNT])
        msg += (
            f"Uncle Lim strategy reference posts (sample):\n"
            f"{sample_text}\n\n"
        )

    # Shadow-mode learning: observational historical prior + reflection rules.
    # Never influences confidence/gating here — it's prompt context only.
    if settings.LEARNING_ENABLED:
        try:
            from src.learning.experience_store import get_experience_store
            block = get_experience_store().prompt_block(
                indicators.get("uncle_lim_signal", "HOLD"),
                indicators.get("uncle_lim_setup_type", "NONE"),
                datetime.now(timezone.utc).hour,
            )
            if block:
                msg += block + "\n\n"
        except Exception:
            pass

    msg += "Based on the above, provide your XAU/USD trading signal as JSON."
    return msg


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_response(raw: str, pair: str) -> AgentVote:
    """Parse LLM JSON response defensively."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "JSON parse error")
        else:
            return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "JSON parse error")

    vote_str = data.get("vote") or data.get("signal") or "HOLD"
    try:
        signal = Signal[vote_str.upper()]
    except KeyError:
        signal = Signal.HOLD

    try:
        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    reasoning = str(data.get("reasoning", ""))[:120]

    setup_type = str(data.get("setup_type", "NONE")).upper()
    if setup_type not in _GOLD_SETUP_TYPES:
        setup_type = "NONE"

    return AgentVote(
        agent_name="LLMAgent",
        pair=pair,
        signal=signal,
        confidence=round(confidence, 4),
        reasoning=reasoning,
        setup_type=setup_type,
    )
