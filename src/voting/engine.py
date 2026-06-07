"""DecisionEngine — sequential three-agent decision pipeline for XAU/USD.

Pipeline per cycle:
  1. UncLeLimAgent.analyze()       → Uncle Lim confluence vote + indicators dict
  2. MacroContext.build()          → gold macro dict (DXY, real yields, Fed, geopolitical)
  3. LLMAgent.vote(indicators, macro_context) → BUY/SELL/HOLD + confidence
  4. If HOLD or confidence < threshold        → DecisionResult(HOLD)
  5. ReviewerAgent.review(indicators, llm_vote) → APPROVED/ADJUSTED/REJECTED
  6. Apply reviewer verdict                   → final DecisionResult

Gold-specific:
  - UncLeLimAgent replaces TechAgent + TrendAgent + MomentumAgent
  - confluence_types: Uncle Lim confirmation labels from multi-TF stack
  - MIN_CONFLUENCES = 3 (from CLAUDE.md)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.agents.base import AgentVote, Signal
from src.agents.uncle_lim_agent import UncLeLimAgent
from src.agents.llm_agent import LLMAgent
from src.agents.macro_context import MacroContext
from src.agents.reviewer_agent import ReviewResult, ReviewVerdict, ReviewerAgent
from config.settings import settings


@dataclass
class DecisionResult:
    pair: str
    final_signal: Signal
    confidence: float
    llm_reasoning: str
    llm_available: bool
    reviewer_verdict: str       # APPROVED / ADJUSTED / REJECTED / SKIPPED / UNAVAILABLE
    reviewer_reason: str
    reviewer_available: bool
    setup_type: str = "NONE"
    indicators: dict = field(default_factory=dict)
    confluence_count: int = 0
    confluence_types: list = field(default_factory=list)


class DecisionEngine:
    """Orchestrates the gold trading decision pipeline."""

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        alert_manager=None,
        event_monitor=None,
    ):
        self.logger = logger or logging.getLogger("DecisionEngine")
        self._alert_manager = alert_manager
        self._credits_alert_sent = False

        self._uncle_lim = UncLeLimAgent(logger)
        self._llm       = LLMAgent(logger)
        self._reviewer  = ReviewerAgent(logger)
        self._macro     = MacroContext(event_monitor=event_monitor, logger=logger)

        self._last_results: dict = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_decision(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        htf_candles: Optional[dict] = None,
    ) -> DecisionResult:
        """Run the full decision pipeline for XAU/USD."""
        if htf_candles is None:
            htf_candles = {}
        threshold = settings.CONSENSUS_THRESHOLD

        # 1. Uncle Lim multi-TF analysis → indicators + uncle_lim vote
        uncle_lim_vote: AgentVote = self._uncle_lim.analyze(pair, candles, htf_candles, price)
        indicators: dict = self._uncle_lim.get_indicators(pair, candles, htf_candles, price)

        # Extract Uncle Lim confluences for DecisionResult
        meta = uncle_lim_vote.meta or {}
        uncle_lim_confirmations = meta.get('confirmations', [])
        uncle_lim_conf_count = len(uncle_lim_confirmations)

        # 2. Build macro context (fails silently)
        macro: dict = {}
        try:
            macro = self._macro.build()
        except Exception as exc:
            self.logger.debug(f"MacroContext.build failed: {exc}")

        # 3. LLM analyst vote (synthesizes Uncle Lim + macro)
        llm_vote: AgentVote = self._llm.vote(
            pair, candles, price, indicators,
            macro_context=macro,
            htf_candles=htf_candles,
        )
        llm_available = self._llm.is_available and llm_vote.reasoning not in (
            "LLM call failed", "All LLM providers exhausted"
        )

        # 4. One-shot alert when both LLM providers are permanently exhausted
        if self._llm.both_exhausted and not self._credits_alert_sent:
            self._credits_alert_sent = True
            self.logger.critical("Both LLM providers (Groq + Anthropic) credits exhausted")
            if self._alert_manager is not None:
                try:
                    self._alert_manager.alert_llm_credits_exhausted()
                except Exception as alert_exc:
                    self.logger.warning(f"Failed to send credits-exhausted alert: {alert_exc}")

        # Use Uncle Lim signal as veto: if it says HOLD, don't proceed regardless of LLM
        # (Uncle Lim is the strategy foundation — LLM synthesizes on top of it)
        effective_signal = llm_vote.signal
        if uncle_lim_vote.signal == Signal.HOLD and llm_vote.signal != Signal.HOLD:
            self.logger.info(
                f"{pair}: UncLeLim says HOLD (confluences={uncle_lim_conf_count}) — "
                f"overriding LLM {llm_vote.signal.value}"
            )
            effective_signal = Signal.HOLD

        # 5. If HOLD or below confidence threshold — skip reviewer
        if effective_signal == Signal.HOLD or llm_vote.confidence < threshold:
            result = DecisionResult(
                pair=pair,
                final_signal=Signal.HOLD,
                confidence=llm_vote.confidence,
                llm_reasoning=llm_vote.reasoning,
                llm_available=llm_available,
                reviewer_verdict='SKIPPED',
                reviewer_reason='HOLD or confidence below threshold',
                reviewer_available=True,
                setup_type='NONE',
                indicators=indicators,
                confluence_count=uncle_lim_conf_count,
                confluence_types=uncle_lim_confirmations,
            )
            self._last_results[pair] = result
            return result

        # 6. Reviewer
        # Uncle Lim is the strategic source of truth for setup classification.
        # Override the LLM analyst's guessed setup_type so the reviewer doesn't
        # reject on a label mismatch between Uncle Lim and the analyst.
        if uncle_lim_vote.setup_type and uncle_lim_vote.setup_type != "NONE":
            llm_vote.setup_type = uncle_lim_vote.setup_type

        review: ReviewResult = self._reviewer.review(
            pair, candles, price, indicators, llm_vote
        )

        # 7. Apply reviewer verdict
        final_signal = llm_vote.signal
        final_conf   = llm_vote.confidence
        rev_verdict  = review.verdict.value
        rev_reason   = review.reason

        if not review.reviewer_available:
            final_signal = Signal.HOLD
            rev_verdict  = 'UNAVAILABLE'
            if self._alert_manager is not None:
                try:
                    self._alert_manager.alert_reviewer_unavailable(review.reason)
                except Exception:
                    pass
        elif review.verdict == ReviewVerdict.REJECTED:
            final_signal = Signal.HOLD
            final_conf   = 0.0
        elif review.verdict == ReviewVerdict.ADJUSTED:
            final_conf = review.adjusted_confidence
            if final_conf < threshold:
                final_signal = Signal.HOLD

        # Use uncle_lim setup_type if LLM returned NONE
        final_setup = llm_vote.setup_type
        if final_setup == "NONE" and uncle_lim_vote.setup_type != "NONE":
            final_setup = uncle_lim_vote.setup_type

        result = DecisionResult(
            pair=pair,
            final_signal=final_signal,
            confidence=round(final_conf, 4),
            llm_reasoning=llm_vote.reasoning,
            llm_available=llm_available,
            reviewer_verdict=rev_verdict,
            reviewer_reason=rev_reason,
            reviewer_available=review.reviewer_available,
            setup_type=final_setup,
            indicators=indicators,
            confluence_count=uncle_lim_conf_count,
            confluence_types=uncle_lim_confirmations,
        )
        self._last_results[pair] = result
        return result

    def get_llm_provider_status(self) -> str:
        groq_ok     = not self._llm._groq_exhausted and self._llm._groq_client is not None
        ant_ok      = not self._llm._anthropic_exhausted and self._llm._anthropic_client is not None
        rev_groq_ok = not self._reviewer._groq_exhausted and self._reviewer._groq_client is not None
        rev_ant_ok  = not self._reviewer._anthropic_exhausted and self._reviewer._anthropic_client is not None

        return (
            "=== Analyst ===\n"
            f"Groq: {'active' if groq_ok else 'exhausted / unavailable'}\n"
            f"Anthropic: {'active (fallback)' if ant_ok else 'exhausted / unavailable'}\n"
            f"Active provider: {self._llm.active_provider}\n\n"
            "=== Reviewer ===\n"
            f"Groq: {'active' if rev_groq_ok else 'exhausted / unavailable'}\n"
            f"Anthropic: {'active (fallback)' if rev_ant_ok else 'exhausted / unavailable'}\n"
            f"Active provider: {self._reviewer.active_provider}"
        )

    def get_analyst_summary(self) -> str:
        if not self._last_results:
            return 'Analyst History\n\nNo decisions yet this session.'

        lines = ['Analyst History\n']
        for pair, result in self._last_results.items():
            conf_str = (
                f"{result.confluence_count}/{settings.MIN_CONFLUENCES} "
                f"[{', '.join(result.confluence_types)}]"
                if result.confluence_types else "not yet computed"
            )
            lines.append(
                f'{pair}\n'
                f'  Signal:      {result.final_signal.value}\n'
                f'  Confidence:  {result.confidence:.2f}\n'
                f'  Setup:       {result.setup_type}\n'
                f'  Confluences: {conf_str}\n'
                f'  Reasoning:   {result.llm_reasoning}\n'
                f'  LLM:         {"available" if result.llm_available else "unavailable"}'
            )
        return '\n\n'.join(lines)

    def get_reviewer_summary(self) -> str:
        if not self._last_results:
            return 'Reviewer History\n\nNo decisions yet this session.'

        verdict_labels = {
            'APPROVED':    'APPROVED',
            'ADJUSTED':    'ADJUSTED',
            'REJECTED':    'REJECTED',
            'SKIPPED':     'SKIPPED (HOLD/low-conf)',
            'UNAVAILABLE': 'REVIEWER DOWN',
        }

        lines = ['Reviewer History\n']
        for pair, result in self._last_results.items():
            badge = verdict_labels.get(result.reviewer_verdict, result.reviewer_verdict)
            conf_str = (
                f"{result.confluence_count}/{settings.MIN_CONFLUENCES} "
                f"[{', '.join(result.confluence_types)}]"
                if result.confluence_types else "not yet computed"
            )
            lines.append(
                f'{pair}\n'
                f'  Verdict:     {badge}\n'
                f'  Confidence:  {result.confidence:.2f}\n'
                f'  Setup:       {result.setup_type}\n'
                f'  Confluences: {conf_str}\n'
                f'  Reason:      {result.reviewer_reason}'
            )
        return '\n\n'.join(lines)
