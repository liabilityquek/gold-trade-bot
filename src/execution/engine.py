"""TradingEngine — main H1 loop for the gold multi-agent bot.

Every cycle:
  1. Kill switch check
  2. Gold market hours check (Sun 22:00–Fri 21:00 UTC)
  3. Account info + daily loss circuit breaker
  4. News suspension check (Rule 1 & 2)
  5. Fetch H1 candles + multi-TF candles (H4, M30, M15, M5)
  6. decision_engine.run_decision() → risk checks → place order
  7. Trade close detection
  8. TradeManager.update_all_trades() — trailing stops + age alerts

Gold-specific:
  - Single XAU_USD instrument (no pair loop)
  - No conflict_checker, no holiday_guard (gold is 24/5)
  - pip_size = 1.0 USD/oz (no JPY/non-JPY branching)
  - 3 TP levels: tp1 (1.5× SL), tp2 (2.0×), tp3 (3.0×)
  - Uncle Lim confluences from DecisionResult (pre-computed by UncLeLimAgent)
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from config.settings import settings
from config.instrument import INSTRUMENT_INFO
from src.broker.base import BaseBroker, OrderSide, Trade
from src.monitoring.alerts import AlertManager
from src.monitoring.logger import get_logger
from src.risk import (
    ExposureTracker,
    PositionSizer,
    PositionSizingMethod,
    RiskValidator,
)
from src.risk.emergency_controller import EmergencyRiskController
from src.execution.trade_manager import TradeManager
from src.execution.order_executor import OrderExecutor, OrderRequest, OrderType
from src.voting.engine import DecisionResult, DecisionEngine
from src.agents.base import Signal
from src.news.suspension_manager import SuspensionManager

_INSTRUMENT = 'XAU_USD'
_GOLD_INFO = INSTRUMENT_INFO.get(_INSTRUMENT, {})


class TradingEngine:
    """Main execution loop — gold multi-agent decision pipeline."""

    def __init__(
        self,
        broker: BaseBroker,
        decision_engine: DecisionEngine,
        alert_manager: AlertManager,
        kill_switch=None,
        logger: Optional[logging.Logger] = None,
        dry_run: bool = False,
        event_monitor=None,
        news_watcher=None,
    ):
        self.broker = broker
        self.decision_engine = decision_engine
        self.alert_manager = alert_manager
        self.kill_switch = kill_switch
        self.logger = logger or get_logger("TradingEngine")
        self.dry_run = dry_run

        self.risk_validator = RiskValidator(self.logger)
        self.position_sizer = PositionSizer(self.logger)
        self.exposure_tracker = ExposureTracker(self.logger)
        self.emergency_controller = EmergencyRiskController(
            logger=self.logger,
            max_daily_loss_percent=settings.MAX_DAILY_DRAWDOWN,
        )
        self.trade_manager = TradeManager(
            broker=self.broker,
            logger=self.logger,
            alert_manager=self.alert_manager,
        )
        self.order_executor = OrderExecutor(
            broker=self.broker,
            logger=self.logger,
            max_retries=settings.ORDER_MAX_RETRIES,
            initial_retry_delay=settings.ORDER_RETRY_INITIAL_DELAY_SECONDS,
            max_retry_delay=settings.ORDER_RETRY_MAX_DELAY_SECONDS,
            backoff_multiplier=settings.ORDER_RETRY_BACKOFF_MULTIPLIER,
        )

        self.suspension_manager = (
            SuspensionManager(event_monitor=event_monitor, logger=self.logger)
            if event_monitor else None
        )
        self.news_watcher = news_watcher

        self._stop_event = threading.Event()
        self._cycle_count = 0
        self._trades_lock = threading.Lock()
        self._known_open_trades: Dict[str, Trade] = {}
        self._initial_balance: Optional[float] = None

        self._daily_loss_start_balance: Optional[float] = None
        self._daily_loss_date: Optional[str] = None
        self._daily_loss_halted: bool = False

        self._last_known_price: Optional[float] = None
        self._outside_window_logged: bool = False

        self._monitoring_stop_event = threading.Event()
        self._monitoring_thread = None
        self._monitoring_cycle_count = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(
        self,
        interval_seconds: Optional[int] = None,
        max_cycles: Optional[int] = None,
    ) -> None:
        interval = interval_seconds or settings.EXECUTION_INTERVAL_SECONDS
        self.alert_manager.alert_system_start()
        self.logger.info(
            f"TradingEngine started | interval={interval}s | "
            f"dry_run={self.dry_run} | instrument={_INSTRUMENT}"
        )

        try:
            with self._trades_lock:
                for t in self.broker.get_open_trades():
                    self._known_open_trades[t.trade_id] = t
        except Exception as exc:
            self.logger.warning(f"Failed to seed open trades on startup: {exc}")

        try:
            price_info = self.broker.get_current_price(_INSTRUMENT)
            if price_info:
                self._last_known_price = (price_info['bid'] + price_info['ask']) / 2
        except Exception as exc:
            self.logger.warning(f"Startup price seed failed: {exc}")

        if self.news_watcher:
            self.news_watcher.start()

        self._monitoring_stop_event = threading.Event()
        self._monitoring_thread = threading.Thread(
            target=self._run_monitoring_loop,
            name="MonitoringThread",
            daemon=True,
        )
        self._monitoring_thread.start()
        self.logger.info("Monitoring thread started")

        while not self._stop_event.is_set():
            self._run_cycle()
            self._cycle_count += 1

            if max_cycles and self._cycle_count >= max_cycles:
                self.logger.info(f"Reached max_cycles={max_cycles}, stopping.")
                break

            self._wait(interval)

        self.alert_manager.alert_system_stop()

    def stop(self) -> None:
        self._stop_event.set()
        if self.news_watcher:
            self.news_watcher.stop()
        self._monitoring_stop_event.set()
        if self._monitoring_thread:
            self._monitoring_thread.join(timeout=5)

    def get_status(self) -> str:
        lines = [
            f"Instrument: {_INSTRUMENT}",
            f"Cycle: #{self._cycle_count}",
            f"Dry run: {self.dry_run}",
        ]

        try:
            account = self.broker.get_account_info()
            if account:
                lines.append(f"Balance: ${account.balance:.2f} | NAV: ${account.nav:.2f}")
                pnl_sign = "+" if account.unrealized_pnl >= 0 else ""
                lines.append(f"Unrealized P/L: {pnl_sign}${account.unrealized_pnl:.2f}")
        except Exception:
            pass

        try:
            trades = self.broker.get_open_trades()
        except Exception:
            trades = list(self._known_open_trades.values())

        if not trades:
            lines.append("\nNo open positions.")
        else:
            lines.append(f"\nOpen positions ({len(trades)}):")
            for t in trades:
                direction = "LONG" if t.is_long else "SHORT"
                pnl_sign = "+" if t.unrealized_pnl >= 0 else ""
                sl_str = f"{t.stop_loss:.2f}" if t.stop_loss else "none"
                tp_str = f"{t.take_profit:.2f}" if t.take_profit else "none"
                lines.append(
                    f"  {t.pair} {direction} {t.units:,} oz"
                    f" | Entry: {t.entry_price:.2f}"
                    f" | P/L: {pnl_sign}${t.unrealized_pnl:.2f}"
                    f" | SL: {sl_str} | TP: {tp_str}"
                )

        lines.append(f"\nMonitoring cycles: #{self._monitoring_cycle_count}")
        return "\n".join(lines)

    def get_known_trades_snapshot(self) -> Dict[str, Trade]:
        with self._trades_lock:
            return dict(self._known_open_trades)

    def remove_known_trade(self, trade_id: str) -> None:
        with self._trades_lock:
            self._known_open_trades.pop(trade_id, None)

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def _run_cycle(self) -> None:
        cycle_num = self._cycle_count + 1
        cycle_start = time.time()
        self.logger.info(
            f"{'='*60}\n"
            f"Cycle #{cycle_num} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )

        # 1. Kill switch
        if self.kill_switch and self.kill_switch.is_active():
            reason = self.kill_switch.get_reason()
            self.logger.critical(f"KILL SWITCH ACTIVE ({reason}) — skipping cycle")
            return

        # 2. Gold market hours + SGT trading window
        if not _is_gold_market_open():
            if not self._outside_window_logged:
                self.logger.info(
                    "Outside trading window (market closed or outside SGT 6pm–midnight). "
                    "Monitoring thread continues."
                )
                self._outside_window_logged = True
            return
        self._outside_window_logged = False

        # 3. Account info
        account = self.broker.get_account_info()
        if not account:
            self.logger.error("Failed to get account info — skipping cycle")
            return

        if self._initial_balance is None:
            self._initial_balance = account.balance
            self.logger.info(f"Initial balance recorded: ${self._initial_balance:.2f}")

        self.logger.info(
            f"Account: balance=${account.balance:.2f} "
            f"NAV=${account.nav:.2f} "
            f"open_trades={account.open_trade_count}"
        )

        # 4. Daily loss circuit breaker
        today = datetime.utcnow().strftime('%Y-%m-%d')
        if self._daily_loss_date != today:
            self._daily_loss_date = today
            self._daily_loss_start_balance = account.nav
            self._daily_loss_halted = False

        if self._daily_loss_start_balance and not self._daily_loss_halted:
            daily_loss_pct = (
                (self._daily_loss_start_balance - account.nav) / self._daily_loss_start_balance
            )
            if daily_loss_pct >= settings.MAX_DAILY_DRAWDOWN:
                self.logger.critical(
                    f"Daily loss limit reached ({daily_loss_pct:.1%}) — halting new trades today"
                )
                self._daily_loss_halted = True
                self.alert_manager.alert_error(
                    f"Daily loss limit reached ({daily_loss_pct:.1%}) — trading halted"
                )

        # 5. Update exposure tracker
        positions = self.broker.get_positions()
        last_prices = {_INSTRUMENT: self._last_known_price} if self._last_known_price else {}
        self.exposure_tracker.update_positions(positions, account.balance, last_prices)

        # 6. Process XAU_USD (if daily loss not halted)
        if not self._daily_loss_halted:
            try:
                self._process_gold(account, positions)
            except Exception as exc:
                self.logger.error(f"Error processing {_INSTRUMENT}: {exc}")

        elapsed = time.time() - cycle_start
        self.logger.info(f"Cycle #{cycle_num} complete in {elapsed:.1f}s")

    def _process_gold(self, account, positions) -> None:
        self.logger.info(f"--- {_INSTRUMENT} ---")

        # Rule 1 & 2 — news suspension check
        if self.suspension_manager:
            status = self.suspension_manager.check_suspension_status()
            if status.is_suspended:
                resume_str = (
                    status.resume_time.strftime('%H:%M')
                    if status.resume_time else 'TBD'
                )
                self.logger.info(
                    f"{_INSTRUMENT}: suspended — {status.message} (resumes ~{resume_str})"
                )
                return

        # Max concurrent trades check
        open_count = sum(1 for p in positions if not p.is_flat)
        if open_count >= settings.MAX_CONCURRENT_TRADES:
            self.logger.info(
                f"{_INSTRUMENT}: max concurrent trades reached ({open_count}/{settings.MAX_CONCURRENT_TRADES})"
            )
            return

        # Fetch H1 candles (primary)
        candles: List[Dict] = []
        try:
            candles = self.broker.get_historical_candles(
                _INSTRUMENT, granularity=settings.TIMEFRAME, count=settings.CANDLE_COUNT
            ) or []
        except Exception as exc:
            self.logger.warning(f"{_INSTRUMENT}: candle fetch failed: {exc}")

        if not candles:
            self.logger.warning(f"{_INSTRUMENT}: no H1 candle data")
            return

        # Current price
        price_info = self.broker.get_current_price(_INSTRUMENT)
        if not price_info:
            self.logger.warning(f"{_INSTRUMENT}: no price data")
            return
        price = (price_info['bid'] + price_info['ask']) / 2
        self._last_known_price = price

        # Fetch higher-timeframe candles for Uncle Lim multi-TF analysis
        htf_candles: dict = {}
        for tf, count in [
            ('H4',  settings.H4_CANDLE_COUNT),
            ('M30', settings.M30_CANDLE_COUNT),
            ('M15', settings.M15_CANDLE_COUNT),
            ('M5',  settings.M5_CANDLE_COUNT),
        ]:
            try:
                tf_data = self.broker.get_historical_candles(_INSTRUMENT, granularity=tf, count=count) or []
                if tf_data:
                    htf_candles[tf] = tf_data
            except Exception:
                pass

        # Run decision pipeline
        result: DecisionResult = self.decision_engine.run_decision(
            _INSTRUMENT, candles, price, htf_candles=htf_candles
        )
        self._log_decision_result(result)

        if result.final_signal == Signal.HOLD:
            self.logger.info(
                f"{_INSTRUMENT}: HOLD (confidence={result.confidence:.2f} | "
                f"reviewer={result.reviewer_verdict})"
            )
            return

        is_long = result.final_signal == Signal.BUY

        # Confluence gate — Uncle Lim minimum 3 confirmations
        min_confluences = settings.MIN_CONFLUENCES
        self.logger.info(
            f"{_INSTRUMENT}: confluence check — {result.confluence_count}/{min_confluences} "
            f"[{', '.join(result.confluence_types) if result.confluence_types else 'none'}] "
            f"({'PASS' if result.confluence_count >= min_confluences else 'FAIL'})"
        )
        if result.confluence_count < min_confluences:
            self.logger.info(
                f"{_INSTRUMENT}: REJECTED — insufficient Uncle Lim confluences "
                f"({result.confluence_count} < {min_confluences})"
            )
            return

        # Setup quality gate
        setup_quality = _get_gold_setup_quality(result.setup_type)
        if setup_quality == 0:
            self.logger.info(f"{_INSTRUMENT}: REJECTED — low-quality setup ({result.setup_type})")
            return

        min_conf_for_setup = _get_min_confidence_for_gold_setup(result.setup_type)
        if result.confidence < min_conf_for_setup:
            self.logger.info(
                f"{_INSTRUMENT}: REJECTED — confidence {result.confidence:.2f} below minimum "
                f"{min_conf_for_setup:.2f} for {result.setup_type}"
            )
            return

        # Skip if existing position in opposite direction
        for pos in positions:
            if pos.pair == _INSTRUMENT and not pos.is_flat:
                if (is_long and pos.is_short) or (not is_long and pos.is_long):
                    self.logger.info(
                        f"{_INSTRUMENT}: skipping — existing position in opposite direction"
                    )
                    return

        # M15 momentum gate
        m15_candles = htf_candles.get('M15', [])
        if m15_candles and not _m15_momentum_aligned(m15_candles, is_long):
            self.logger.info(
                f"{_INSTRUMENT}: M15 momentum gate blocked — "
                f"{'BUY' if is_long else 'SELL'} conflicts with M15 direction"
            )
            return

        # SL/TP calculation
        entry_price = price_info['ask'] if is_long else price_info['bid']
        sl_distance, stop_loss, take_profit, tp2, tp3, atr_val = self._calc_sl_tp(
            candles, entry_price, is_long
        )

        # RR validation
        tp_distance = abs(take_profit - entry_price)
        rr_ratio = round(tp_distance / sl_distance, 4) if sl_distance > 0 else 0.0
        if rr_ratio < settings.MIN_RR_RATIO:
            self.logger.info(
                f"{_INSTRUMENT}: REJECTED — poor RR ({rr_ratio:.2f} < {settings.MIN_RR_RATIO:.2f})"
            )
            return

        # Position sizing: units = (NAV × 0.01) / sl_distance_in_usd
        size_result = self.position_sizer.calculate(
            account_balance=account.balance,
            stop_loss_points=sl_distance,
            method=PositionSizingMethod.PERCENT_RISK,
            current_price=entry_price,
        )
        if not size_result:
            self.logger.warning(f"{_INSTRUMENT}: position sizing failed")
            return

        units = size_result.units

        # Risk validator
        exposure_report = self.exposure_tracker.get_current_exposure()
        margin_util_pct = (account.margin_used / account.nav * 100) if account.nav > 0 else 0.0
        validation = self.risk_validator.validate_trade(
            units=units,
            stop_loss_points=sl_distance,
            account_balance=account.balance,
            current_exposure_percent=margin_util_pct,
            open_trade_count=open_count,
            entry_price=entry_price,
            margin_available=account.margin_available,
            rr_ratio=rr_ratio,
        )
        if not validation.approved:
            self.logger.info(f"{_INSTRUMENT}: risk rejected — {', '.join(validation.reasons)}")
            return

        # Place order
        if self.dry_run:
            self.logger.info(
                f"{_INSTRUMENT}: DRY RUN — would {result.final_signal.value} "
                f"{units:,} oz @ {entry_price:.2f} "
                f"SL={stop_loss:.2f} TP1={take_profit:.2f} TP2={tp2:.2f} TP3={tp3:.2f}"
            )
            self._send_trade_alert(
                result, entry_price, stop_loss, take_profit, tp2, tp3, units,
                sl_distance, rr_ratio, dry_run=True,
            )
            return

        side = OrderSide.BUY if is_long else OrderSide.SELL
        # Small positions (<4 units) can't scale out — close entire position at TP1.
        # Larger positions use TP3 as broker safety net; monitor handles TP2 partial close.
        broker_tp = take_profit if units < 4 else tp3

        # Stable per-decision id: same bar + direction can't double-fire, but a
        # fresh candle can re-enter. Falls back to cycle count if candle has no time.
        last_candle = candles[-1] if candles else {}
        candle_time = last_candle.get('time') or last_candle.get('timestamp')
        signal_id = (
            f"{_INSTRUMENT}-{candle_time}-{result.final_signal.value}"
            if candle_time else
            f"{_INSTRUMENT}-{self._cycle_count}-{result.final_signal.value}"
        )

        # Route through OrderExecutor for circuit-breaker, rate-limit, slippage
        # tracking and duplicate-signal protection.
        exec_result = self.order_executor.execute(
            OrderRequest(
                pair=_INSTRUMENT,
                side=side,
                units=units,
                order_type=OrderType.MARKET,
                stop_loss=stop_loss,
                take_profit=broker_tp,
                expected_price=entry_price,
                signal_id=signal_id,
                strategy_name=f"uncle_lim_{result.setup_type.lower()}",
            )
        )

        if not exec_result.success:
            self.logger.error(f"{_INSTRUMENT}: order failed: {exec_result.error_message}")
            return

        trade_id = exec_result.trade_id
        if trade_id:
            self.logger.info(
                f"{_INSTRUMENT}: order filled | trade_id={trade_id} "
                f"entry~{(exec_result.fill_price or entry_price):.2f} "
                f"slippage={exec_result.slippage_points:+.2f} pts"
            )
            filled_price = exec_result.fill_price or entry_price
            placed_trade = None
            for t in self.broker.get_open_trades():
                if t.trade_id == trade_id:
                    filled_price = t.entry_price
                    with self._trades_lock:
                        self._known_open_trades[trade_id] = t
                    placed_trade = t
                    break

            if placed_trade:
                self.trade_manager.register_trade(
                    placed_trade,
                    strategy_name=f"uncle_lim_{result.setup_type.lower()}",
                    trailing_stop=True,
                    confidence=result.confidence,
                    entry_reason=result.llm_reasoning,
                    setup_type=result.setup_type,
                    reviewer_verdict=result.reviewer_verdict,
                    reviewer_reason=result.reviewer_reason,
                    tp2=tp2,
                    tp3=tp3,
                )
                if atr_val and atr_val > 0:
                    self.trade_manager.update_trade_atr(trade_id, atr_val)

                if settings.LEARNING_ENABLED:
                    try:
                        from src.learning.experience_store import get_experience_store
                        get_experience_store().record_entry(
                            trade_id,
                            result.final_signal,
                            result.setup_type,
                            indicators=result.indicators,
                            confidence=result.confidence,
                            rr=rr_ratio,
                        )
                    except Exception as exc:
                        self.logger.debug(f"learning record_entry failed: {exc}")

            self._send_trade_alert(
                result, filled_price, stop_loss, take_profit, tp2, tp3, units,
                sl_distance, rr_ratio,
            )

    # ------------------------------------------------------------------
    # Monitoring loop
    # ------------------------------------------------------------------

    def _run_monitoring_cycle(self) -> None:
        self._monitoring_cycle_count += 1
        self.logger.debug(f"Monitoring cycle #{self._monitoring_cycle_count}")

        if self.kill_switch and self.kill_switch.is_active():
            return

        account = self.broker.get_account_info()
        if not account:
            return

        positions = self.broker.get_positions()
        last_prices = {_INSTRUMENT: self._last_known_price} if self._last_known_price else {}
        self.exposure_tracker.update_positions(positions, account.balance, last_prices)

        try:
            self._run_emergency_check(account, positions)
        except Exception as exc:
            self.logger.error(f"Emergency check error: {exc}")

        try:
            self._check_closed_trades()
        except Exception as exc:
            self.logger.error(f"Trade close detection error: {exc}")

        try:
            self.trade_manager.update_all_trades()
        except Exception as exc:
            self.logger.error(f"Trade manager update error: {exc}")

    def _run_monitoring_loop(self) -> None:
        interval = settings.MONITORING_INTERVAL_SECONDS
        self.logger.info(f"Monitoring loop started | interval={interval}s")
        while not self._monitoring_stop_event.is_set():
            self._run_monitoring_cycle()
            self._monitoring_stop_event.wait(interval)
        self.logger.info("Monitoring loop stopped")

    # ------------------------------------------------------------------
    # Trade close detection
    # ------------------------------------------------------------------

    def _check_closed_trades(self) -> None:
        with self._trades_lock:
            if not self._known_open_trades:
                return
            snapshot = dict(self._known_open_trades)

        current_trades = self.broker.get_open_trades()
        current_ids = {t.trade_id for t in current_trades}

        for trade_id, trade in snapshot.items():
            if trade_id not in current_ids:
                info = self.broker.get_closed_trade_info(trade_id)
                close_price = info.get('close_price', trade.current_price)
                realized_pnl = info.get('realized_pnl', 0.0)
                raw_reason = info.get('reason', 'user')

                reason_label = {
                    'stop_loss': 'Stop Loss Hit',
                    'take_profit': 'Take Profit Hit',
                    'user': 'Closed by User',
                }.get(raw_reason, 'Closed by User')

                # Gold: points = direct USD difference (no pip_size division)
                points_gained = close_price - trade.entry_price
                if not trade.is_long:
                    points_gained = -points_gained

                self.logger.info(
                    f"Trade closed: {trade_id} ({trade.pair}) | "
                    f"Entry: {trade.entry_price:.2f} | Close: {close_price:.2f} "
                    f"({points_gained:+.1f} pts) | P/L: ${realized_pnl:+.2f} | {reason_label}"
                )

                self.alert_manager.alert_trade_closed(
                    trade_id=trade_id,
                    pnl=realized_pnl,
                    close_price=close_price,
                    entry_price=trade.entry_price,
                    points=points_gained,
                    reason=reason_label,
                )

                if settings.LEARNING_ENABLED:
                    try:
                        from src.learning.experience_store import get_experience_store
                        hold_hours = None
                        open_time = getattr(trade, 'open_time', None)
                        if open_time is not None:
                            try:
                                hold_hours = (
                                    datetime.now(open_time.tzinfo) - open_time
                                ).total_seconds() / 3600.0
                            except Exception:
                                hold_hours = None
                        get_experience_store().record_outcome(
                            trade_id,
                            pnl=realized_pnl,
                            close_reason=raw_reason,
                            hold_hours=hold_hours,
                        )
                    except Exception as exc:
                        self.logger.debug(f"learning record_outcome failed: {exc}")

                self.trade_manager.unregister_trade(trade_id)
                with self._trades_lock:
                    self._known_open_trades.pop(trade_id, None)

        with self._trades_lock:
            for t in current_trades:
                if t.trade_id not in self._known_open_trades:
                    self._known_open_trades[t.trade_id] = t

    # ------------------------------------------------------------------
    # Emergency risk check
    # ------------------------------------------------------------------

    def _run_emergency_check(self, account, positions) -> None:
        margin_util_pct = (account.margin_used / account.nav * 100) if account.nav > 0 else 0.0

        status = self.emergency_controller.check_emergency_conditions(
            account_balance=account.balance,
            initial_balance=self._initial_balance or account.balance,
            open_positions=positions,
            current_exposure_percent=margin_util_pct,
            unrealized_pnl=account.unrealized_pnl,
            account_nav=account.nav,
        )

        if status.requires_shutdown:
            reason = status.shutdown_reason.value if status.shutdown_reason else "risk_limit"
            open_positions = [p for p in positions if not p.is_flat]
            if open_positions:
                self.logger.critical(
                    f"Emergency shutdown: {reason} — "
                    f"{len(open_positions)} position(s) still open, closing"
                )
                self.trade_manager.emergency_close_all(reason=reason)
            # else: positions already closed — stay silent

    # ------------------------------------------------------------------
    # SL/TP calculation — gold (USD/oz points)
    # ------------------------------------------------------------------

    def _calc_sl_tp(
        self,
        candles: List[Dict],
        entry_price: float,
        is_long: bool,
    ):
        """Return (sl_distance, stop_loss, tp1, tp2, tp3, atr_val).

        All distances are in USD/oz (1 point = $1/oz).
        """
        atr_val = _calc_atr(candles)

        if atr_val and atr_val > 0:
            multiplier = _get_atr_multiplier(atr_val, candles)
            self.logger.info(f"{_INSTRUMENT} ATR multiplier: {multiplier}x (ATR={atr_val:.2f})")
            sl_distance = atr_val * multiplier
        else:
            sl_distance = float(settings.DEFAULT_ATR_POINTS)  # fallback in points

        # Enforce minimum SL distance (2 pts as per CLAUDE.md)
        sl_distance = max(sl_distance, 2.0)

        # 3 TP targets — multipliers from settings (env-overridable)
        tp1_distance = sl_distance * settings.TP1_MULTIPLIER
        tp2_distance = sl_distance * settings.TP2_MULTIPLIER
        tp3_distance = sl_distance * settings.TP3_MULTIPLIER

        if is_long:
            stop_loss = entry_price - sl_distance
            tp1 = entry_price + tp1_distance
            tp2 = entry_price + tp2_distance
            tp3 = entry_price + tp3_distance
        else:
            stop_loss = entry_price + sl_distance
            tp1 = entry_price - tp1_distance
            tp2 = entry_price - tp2_distance
            tp3 = entry_price - tp3_distance

        return sl_distance, stop_loss, tp1, tp2, tp3, atr_val

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def _send_trade_alert(
        self,
        result: DecisionResult,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        tp2: float,
        tp3: float,
        units: int,
        sl_distance: float,
        rr_ratio: float,
        dry_run: bool = False,
    ) -> None:
        prefix = "DRY RUN -- " if dry_run else ""
        lines = [
            f"{prefix}TRADE OPENED -- {_INSTRUMENT}",
            f"Direction:    {result.final_signal.value}",
            f"Setup:        {result.setup_type}",
            f"Confluences:  {result.confluence_count}/{settings.MIN_CONFLUENCES} "
            f"[{', '.join(result.confluence_types)}]",
            f"Entry:        {entry_price:.2f}",
            f"SL:           {stop_loss:.2f} ({sl_distance:.1f} pts)",
            f"TP1:          {take_profit:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}",
            f"RR (TP1):     {rr_ratio:.2f}",
            f"Size:         {units:,} oz",
            f"Confidence:   {result.confidence:.2f}",
            "",
            f"LLM: {result.final_signal.value} ({result.confidence:.2f}) | "
            f"Reviewer: {result.reviewer_verdict} -- {result.reviewer_reason}",
        ]
        self.alert_manager._send_telegram("\n".join(lines), parse_mode='')

    def _log_decision_result(self, result: DecisionResult) -> None:
        self.logger.info(
            f"{result.pair}: {result.final_signal.value} "
            f"conf={result.confidence:.2f} | setup={result.setup_type} | "
            f"confluences={result.confluence_count} | "
            f"reviewer={result.reviewer_verdict} — {result.reviewer_reason}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wait(self, seconds: int) -> None:
        for _ in range(seconds):
            if self._stop_event.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _is_gold_market_open() -> bool:
    """Gold trades Sun 22:00–Fri 21:00 UTC, restricted to SGT 6pm–midnight (UTC 10:00–16:00)."""
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    hour = now.hour

    # Saturday: always closed
    if weekday == 5:
        return False
    # Friday: closed after 21:00 UTC
    if weekday == 4 and hour >= 21:
        return False
    # Sunday: open only after 22:00 UTC
    if weekday == 6 and hour < 22:
        return False

    # SGT trading window: only trade during configured UTC hours (default 10:00–16:00 = SGT 6pm–midnight)
    if settings.TRADING_WINDOW_ENABLED:
        start = settings.TRADING_WINDOW_START_UTC
        end = settings.TRADING_WINDOW_END_UTC
        if not (start <= hour < end):
            return False

    return True


def _calc_atr(candles: List[Dict], period: int = 14) -> Optional[float]:
    if not candles or len(candles) < period + 1:
        return None
    df = pd.DataFrame([
        {
            'high':  float(c.get('high', 0) or c.get('mid', {}).get('h', 0) or 0),
            'low':   float(c.get('low',  0) or c.get('mid', {}).get('l', 0) or 0),
            'close': float(c.get('close',0) or c.get('mid', {}).get('c', 0) or 0),
        }
        for c in candles
    ])
    high = df['high']
    low  = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


def _get_atr_multiplier(atr_val: float, candles: List[Dict], atr_period: int = 14, avg_period: int = 50) -> float:
    if not candles or len(candles) < atr_period + avg_period + 1:
        return 2.0
    df = pd.DataFrame([
        {
            'high':  float(c.get('high', 0) or c.get('mid', {}).get('h', 0) or 0),
            'low':   float(c.get('low',  0) or c.get('mid', {}).get('l', 0) or 0),
            'close': float(c.get('close',0) or c.get('mid', {}).get('c', 0) or 0),
        }
        for c in candles
    ])
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low']  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(window=atr_period).mean()
    atr_avg = atr_series.iloc[-avg_period:].mean()
    if pd.isna(atr_avg) or atr_avg == 0:
        return 2.0
    ratio = atr_val / atr_avg
    if ratio > 1.5:
        return 3.0
    if ratio < 0.8:
        return 1.5
    return 2.0


def _m15_momentum_aligned(m15_candles: list, is_long: bool) -> bool:
    recent = m15_candles[-5:] if len(m15_candles) >= 5 else m15_candles
    if len(recent) < 3:
        return True

    bullish = sum(
        1 for c in recent
        if float(c.get('close', 0) or c.get('mid', {}).get('c', 0)) >
           float(c.get('open',  0) or c.get('mid', {}).get('o', 0))
    )
    bearish = len(recent) - bullish

    first_close = float(recent[0].get('close', 0) or recent[0].get('mid', {}).get('c', 0))
    last_close  = float(recent[-1].get('close', 0) or recent[-1].get('mid', {}).get('c', 0))
    net_move = last_close - first_close

    if is_long:
        return not (bearish > bullish and net_move < 0)
    else:
        return not (bullish > bearish and net_move > 0)


def _get_gold_setup_quality(setup_type: str) -> int:
    """Quality tiers for Uncle Lim gold setups."""
    quality_map = {
        'TRENDLINE_BREAKOUT': 5,
        'SND_ZONE':           4,
        'LCT':                4,
        'RTB':                3,
        'PULLBACK':           3,
        'NONE':               0,
    }
    return quality_map.get(setup_type.upper(), 0)


def _get_min_confidence_for_gold_setup(setup_type: str) -> float:
    confidence_map = {
        'TRENDLINE_BREAKOUT': 0.60,
        'SND_ZONE':           0.60,
        'LCT':                0.62,
        'RTB':                0.65,
        'PULLBACK':           0.65,
        'NONE':               1.00,
    }
    return confidence_map.get(setup_type.upper(), 0.65)
