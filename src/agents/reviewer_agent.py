"""ReviewerAgent — Senior Gold Execution Trader (XAU/USD).

Role: 20-year institutional gold trader with final execution authority.

Provider priority:
  1. Groq llama-3.1-8b-instant (primary — fast review task)
  2. Anthropic Claude Haiku (fallback on Groq credit exhaustion)
  3. REJECTED + reviewer_available=False when all exhausted

Gold-specific validation rules:
  - Uncle Lim confluence >= 3 timeframe confirmations required
  - Minimum RR 1.5 required
  - Gold high-impact events: FOMC, NFP, CPI, PCE, GDP, Geopolitical shocks
  - BUY bias enforced in H4 uptrend; SELL requires confirmed H4 breakdown
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from openai import OpenAI

from .base import AgentVote, Signal
from ._llm_utils import _is_credit_exhausted
from config.settings import settings
_last_reviewer_call: float = 0.0
_reviewer_call_lock = threading.Lock()


class ReviewVerdict(Enum):
    APPROVED = 'APPROVED'
    ADJUSTED = 'ADJUSTED'
    REJECTED = 'REJECTED'


@dataclass
class ReviewResult:
    verdict: ReviewVerdict
    adjusted_confidence: float
    reason: str
    reviewer_available: bool


_REVIEWER_SYSTEM_PROMPT = """You are a senior gold trader with 20 years institutional experience in XAU/USD.

You will receive:
1. A complete market briefing (technical indicators, Uncle Lim confluence signals, macro context, upcoming events)
2. A trade recommendation from a quantitative gold analyst including their reasoning

Your job: should this XAU/USD trade be placed RIGHT NOW?

You are NOT re-analysing the market from scratch.
You are reviewing whether the analyst's recommendation is sound given:
  - Is the Uncle Lim confluence >= 3 timeframe confirmations?
  - Does the setup match the stated setup type (SND_ZONE, TRENDLINE_BREAKOUT, LCT, RTB, PULLBACK)?
  - Is the RR ratio >= 1.5 as required by gold strategy rules?
  - Does the direction align with the H4 bias?
  - Are there imminent high-impact gold events that make entry timing poor?

Rules you never break:
  1. Never approve a new position within 30 minutes of FOMC, NFP, CPI, PCE, GDP, or geopolitical shock.
  2. Reject if H4 bias is bearish and analyst recommends BUY (unless analyst explicitly notes H4 reversal).
  3. Reject if Uncle Lim confluence count < 3.
  4. If analyst's reasoning contradicts the indicators shown, REJECT.
  5. If confidence is inflated vs actual signal quality, ADJUST it down.
  6. You cannot change the direction (BUY to SELL). If you disagree with direction, REJECT.
  7. For SELL signals: require explicit H4 breakdown confirmation in reasoning.

Gold-specific notes:
  - Real yields bearish signal + DXY bullish = strong headwind for gold longs
  - Geopolitical risk overrides technical bearish signals (safe-haven demand)
  - COMEX expiry dates can cause abnormal volatility — flag as caution

Respond with valid JSON only — no markdown, no preamble:
{"verdict": "APPROVED|ADJUSTED|REJECTED", "adjusted_confidence": 0.0-1.0, "reason": "max 150 chars"}

If APPROVED: set adjusted_confidence equal to the original analyst confidence.
If REJECTED: set adjusted_confidence to 0.0.
Speak like a gold trader — direct and specific."""


class ReviewerAgent:
    """
    Senior XAU/USD execution trader.
    Provider priority: Groq (primary) → Anthropic (fallback).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('ReviewerAgent')

        self._groq_client: Optional[OpenAI] = None
        self._groq_model:     str  = ''
        self._groq_exhausted: bool = False

        self._anthropic_client = None
        self._anthropic_model:     str  = ''
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
                self.logger.warning('ReviewerAgent: GROQ_API_KEY not set — Groq disabled')
                self._groq_exhausted = True
                return
            self._groq_client = OpenAI(
                api_key=settings.GROQ_API_KEY,
                base_url='https://api.groq.com/openai/v1',
            )
            self._groq_model = settings.REVIEWER_LLM_MODEL
            self.logger.info(f'ReviewerAgent: Groq ready ({self._groq_model})')
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent: Groq init failed: {exc}')
            self._groq_exhausted = True

    def _init_anthropic(self) -> None:
        try:
            from config.settings import settings
            if not settings.ANTHROPIC_API_KEY:
                self.logger.info('ReviewerAgent: ANTHROPIC_API_KEY not set — Anthropic fallback disabled')
                self._anthropic_exhausted = True
                return
            import anthropic as _sdk
            self._anthropic_client = _sdk.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            self._anthropic_model = settings.ANTHROPIC_LLM_MODEL
            self.logger.info(f'ReviewerAgent: Anthropic fallback ready ({self._anthropic_model})')
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent: Anthropic init failed: {exc}')
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
            return 'groq'
        if not self._anthropic_exhausted:
            return 'anthropic'
        return 'none'

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def review(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: dict,
        analyst_vote: AgentVote,
    ) -> ReviewResult:
        """Review the analyst's recommendation. Always returns ReviewResult — never raises."""
        if self.both_exhausted:
            self.logger.warning('ReviewerAgent: both providers exhausted — blocking trade')
            return ReviewResult(
                verdict=ReviewVerdict.REJECTED,
                adjusted_confidence=0.0,
                reason='All reviewer providers exhausted',
                reviewer_available=False,
            )
        try:
            return self._review(pair, candles, price, indicators, analyst_vote)
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent review failed for {pair}: {exc}')
            return ReviewResult(
                verdict=ReviewVerdict.REJECTED,
                adjusted_confidence=0.0,
                reason='Reviewer unexpected error — trade blocked for safety',
                reviewer_available=True,
            )

    # ------------------------------------------------------------------
    # Internal logic
    # ------------------------------------------------------------------

    def _review(self, pair, candles, price, indicators, analyst_vote) -> ReviewResult:
        global _last_reviewer_call
        with _reviewer_call_lock:
            elapsed = time.time() - _last_reviewer_call
            wait = max(0.0, settings.REVIEWER_MIN_CALL_SPACING_SECONDS - elapsed)
            _last_reviewer_call = time.time() + wait

        if wait > 0.0:
            time.sleep(wait)

        user_msg = _build_review_message(pair, candles, price, indicators, analyst_vote)

        if not self._groq_exhausted and self._groq_client is not None:
            try:
                return self._call_groq(user_msg, analyst_vote)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning('ReviewerAgent: Groq credits exhausted — switching to Anthropic')
                    self._groq_exhausted = True
                else:
                    self.logger.warning(f'ReviewerAgent: Groq transient error for {pair}: {exc}')
                    return ReviewResult(
                        verdict=ReviewVerdict.REJECTED,
                        adjusted_confidence=0.0,
                        reason='Reviewer transient error — trade blocked for safety',
                        reviewer_available=True,
                    )

        if not self._anthropic_exhausted and self._anthropic_client is not None:
            try:
                return self._call_anthropic(user_msg, analyst_vote)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning('ReviewerAgent: Anthropic exhausted — all providers down')
                    self._anthropic_exhausted = True
                else:
                    self.logger.warning(f'ReviewerAgent: Anthropic transient error for {pair}: {exc}')
                    return ReviewResult(
                        verdict=ReviewVerdict.REJECTED,
                        adjusted_confidence=0.0,
                        reason='Reviewer transient error — trade blocked for safety',
                        reviewer_available=True,
                    )

        return ReviewResult(
            verdict=ReviewVerdict.REJECTED,
            adjusted_confidence=0.0,
            reason='All reviewer providers exhausted',
            reviewer_available=False,
        )

    def _call_groq(self, user_msg: str, analyst_vote: AgentVote) -> ReviewResult:
        response = self._groq_client.chat.completions.create(
            model=self._groq_model,
            max_tokens=settings.REVIEWER_MAX_TOKENS,
            messages=[
                {'role': 'system', 'content': _REVIEWER_SYSTEM_PROMPT},
                {'role': 'user',   'content': user_msg},
            ],
        )
        if not response.choices:
            return ReviewResult(ReviewVerdict.REJECTED, 0.0, 'Empty Groq response — blocked for safety', True)
        return _parse_review_response(response.choices[0].message.content.strip(), analyst_vote)

    def _call_anthropic(self, user_msg: str, analyst_vote: AgentVote) -> ReviewResult:
        response = self._anthropic_client.messages.create(
            model=self._anthropic_model,
            max_tokens=settings.REVIEWER_MAX_TOKENS,
            system=_REVIEWER_SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': user_msg}],
        )
        if not response.content:
            return ReviewResult(ReviewVerdict.REJECTED, 0.0, 'Empty Anthropic response — blocked for safety', True)
        return _parse_review_response(response.content[0].text.strip(), analyst_vote)


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_review_message(
    pair: str,
    candles: List[Dict],
    price: float,
    indicators: dict,
    analyst_vote: AgentVote,
) -> str:
    from .llm_agent import _build_analyst_message
    briefing = _build_analyst_message(pair, candles, price, indicators)

    meta = analyst_vote.meta or {}
    confluence_count = indicators.get('uncle_lim_confluences', 0)
    confirmations = indicators.get('uncle_lim_confirmations', '')
    h4_bias = indicators.get('uncle_lim_h4_bias', 'unknown')

    return (
        f'{briefing}\n\n'
        f'=== ANALYST RECOMMENDATION ===\n'
        f'Vote:              {analyst_vote.signal.value}\n'
        f'Confidence:        {analyst_vote.confidence:.2f}\n'
        f'Setup type:        {analyst_vote.setup_type}\n'
        f'H4 bias:           {h4_bias}\n'
        f'Uncle Lim confluences: {confluence_count}/3 [{confirmations}]\n'
        f'Reasoning:         {analyst_vote.reasoning}\n\n'
        f'Review this XAU/USD recommendation. Should we execute this trade right now?'
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_review_response(raw: str, analyst_vote: AgentVote) -> ReviewResult:
    text = raw.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(lines[1:-1]) if len(lines) > 2 else text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return ReviewResult(ReviewVerdict.APPROVED, analyst_vote.confidence, 'JSON parse error — passing through', True)
        else:
            return ReviewResult(ReviewVerdict.APPROVED, analyst_vote.confidence, 'JSON parse error — passing through', True)

    verdict_str = data.get('verdict', 'APPROVED').upper()
    try:
        verdict = ReviewVerdict[verdict_str]
    except KeyError:
        verdict = ReviewVerdict.APPROVED

    try:
        adj_conf = float(data.get('adjusted_confidence', analyst_vote.confidence))
        adj_conf = max(0.0, min(1.0, adj_conf))
    except (TypeError, ValueError):
        adj_conf = analyst_vote.confidence

    reason = str(data.get('reason', ''))[:150]

    return ReviewResult(
        verdict=verdict,
        adjusted_confidence=round(adj_conf, 4),
        reason=reason,
        reviewer_available=True,
    )
