"""DecisionEngine — deterministic H1 trend-following decision for XAU/USD.

Pipeline per cycle:
  1. TrendAgent.analyze()        -> BUY/SELL/HOLD (+DI/-DI cross + ADX, H1 only)
  2. TrendAgent.get_indicators() -> indicator snapshot (recorded on every trade)

No LLM, no reviewer, no macro. SL/TP and sizing live in the execution engine.
DecisionResult shape is unchanged so downstream consumers need no edits.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.agents.base import AgentVote, Signal
from src.agents.trend_agent import TrendAgent
from config.settings import settings


@dataclass
class DecisionResult:
    pair: str
    final_signal: Signal
    confidence: float
    llm_reasoning: str
    llm_available: bool
    reviewer_verdict: str       # APPROVED / ADJUSTED / REJECTED / SKIPPED / UNAVAILABLE / DISABLED
    reviewer_reason: str
    reviewer_available: bool
    setup_type: str = "NONE"
    indicators: dict = field(default_factory=dict)
    confluence_count: int = 0
    confluence_types: list = field(default_factory=list)


class DecisionEngine:
    """Orchestrates the gold trend-following decision."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("DecisionEngine")
        self._trend = TrendAgent(logger)
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
        """Run the deterministic trend decision for XAU/USD."""
        if htf_candles is None:
            htf_candles = {}

        vote: AgentVote = self._trend.analyze(pair, candles, htf_candles, price)
        indicators: dict = self._trend.get_indicators(pair, candles, htf_candles, price)
        confs = (vote.meta or {}).get('confirmations', [])

        result = DecisionResult(
            pair=pair,
            final_signal=vote.signal,
            confidence=round(vote.confidence, 4),
            llm_reasoning=vote.reasoning,
            llm_available=True,
            reviewer_verdict='DISABLED',
            reviewer_reason='TA trend mode',
            reviewer_available=True,
            setup_type=vote.setup_type,
            indicators=indicators,
            confluence_count=len(confs),
            confluence_types=confs,
        )
        self._last_results[pair] = result
        return result

    # ------------------------------------------------------------------
    # Telegram command helpers (method names unchanged for the poller)
    # ------------------------------------------------------------------

    def get_llm_provider_status(self) -> str:
        return "LLM disabled - TA trend mode"

    def get_reviewer_summary(self) -> str:
        return "Reviewer disabled - TA trend mode"

    def get_analyst_summary(self) -> str:
        if not self._last_results:
            return 'Trend History\n\nNo decisions yet this session.'

        lines = ['Trend History\n']
        for pair, result in self._last_results.items():
            conf_str = (
                f"{result.confluence_count}/{settings.MIN_CONFLUENCES} "
                f"[{', '.join(result.confluence_types)}]"
                if result.confluence_types else "no trend"
            )
            lines.append(
                f'{pair}\n'
                f'  Signal:      {result.final_signal.value}\n'
                f'  Confidence:  {result.confidence:.2f}\n'
                f'  Setup:       {result.setup_type}\n'
                f'  Confluences: {conf_str}\n'
                f'  Reasoning:   {result.llm_reasoning}'
            )
        return '\n\n'.join(lines)
