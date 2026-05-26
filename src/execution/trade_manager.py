"""Active trade management for XAU/USD.

Gold-specific adaptations:
  - pip_size = 1.0 (USD/oz points — no JPY branch needed)
  - Break-even: at 5 pts profit → SL to entry + 1 pt + 50% partial close
  - Trailing stop: at 7 pts profit → trail ATR×1.5 behind price peak
  - Three TP levels: tp1, tp2, tp3 (tracked in ManagedTrade)
  - All price comparisons in USD/oz (e.g. 3285.00)
"""

import json
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from src.broker.base import BaseBroker, Trade, OrderSide, TradeCloseResult
from src.monitoring.logger import get_logger
from src.monitoring.alerts import AlertManager
from config.settings import settings

# Gold: 1 point = $1/oz — no currency conversion factor
_GOLD_POINT = 1.0


class TradeAction(Enum):
    NONE = "none"
    CLOSE = "close"
    MODIFY_SL = "modify_sl"
    MODIFY_TP = "modify_tp"
    TRAILING_STOP = "trailing_stop"
    EMERGENCY_CLOSE = "emergency_close"


@dataclass
class ManagedTrade:
    """Extended trade information for gold management."""
    trade: Trade
    strategy_name: str = ""
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    initial_sl: Optional[float] = None
    initial_tp: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    trailing_stop_active: bool = False
    trailing_stop_distance: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    partial_closes: List[Dict] = field(default_factory=list)
    break_even_triggered: bool = False
    partial_tp_triggered: bool = False
    tp2_triggered: bool = False
    atr_value: Optional[float] = None
    confidence: float = 0.0
    entry_reason: str = ""
    setup_type: str = "NONE"
    reviewer_verdict: str = ""
    reviewer_reason: str = ""

    @property
    def age_hours(self) -> float:
        now = datetime.now(timezone.utc)
        start = self.entry_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return max(0.0, (now - start).total_seconds() / 3600)


@dataclass
class TradeManagementResult:
    trade_id: str
    action: TradeAction
    success: bool
    details: str = ""
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None
    pnl: float = 0.0


class TradeManager:
    """Manage active XAU/USD trades with trailing stops and break-even logic."""

    def __init__(
        self,
        broker: BaseBroker,
        logger: Optional[logging.Logger] = None,
        alert_manager: Optional[AlertManager] = None,
    ):
        self.broker = broker
        self.logger = logger or get_logger('trade_manager')
        self.alert_manager = alert_manager

        self.managed_trades: Dict[str, ManagedTrade] = {}
        self._lock = threading.Lock()

        self._state_file = Path(__file__).parent.parent.parent / "data" / "managed_trades.json"
        self._persisted_state: Dict[str, dict] = {}
        self._load_state()

        self.trailing_stop_enabled = True
        # Gold: all thresholds in USD/oz points (not pips)
        self.trailing_stop_activation_points = settings.TRAILING_STOP_ACTIVATION_POINTS  # 7.0
        self.break_even_activation_points = settings.BREAK_EVEN_ACTIVATION_POINTS        # 5.0
        self.break_even_buffer_points = settings.BREAK_EVEN_BUFFER_POINTS                # 1.0
        self.max_trade_age_hours = settings.MAX_TRADE_AGE_HOURS

    def register_trade(
        self,
        trade: Trade,
        strategy_name: str = "",
        trailing_stop: bool = False,
        trailing_distance: float = 0.0,
        confidence: float = 0.0,
        entry_reason: str = "",
        setup_type: str = "NONE",
        reviewer_verdict: str = "",
        reviewer_reason: str = "",
        tp2: Optional[float] = None,
        tp3: Optional[float] = None,
    ) -> ManagedTrade:
        managed = ManagedTrade(
            trade=trade,
            strategy_name=strategy_name,
            entry_time=trade.open_time or datetime.now(timezone.utc),
            initial_sl=trade.stop_loss,
            initial_tp=trade.take_profit,
            tp2=tp2,
            tp3=tp3,
            trailing_stop_active=trailing_stop,
            trailing_stop_distance=trailing_distance,
            highest_price=trade.entry_price,
            lowest_price=trade.entry_price,
            confidence=confidence,
            entry_reason=entry_reason[:120] if entry_reason else "",
            setup_type=setup_type,
            reviewer_verdict=reviewer_verdict,
            reviewer_reason=reviewer_reason,
        )

        with self._lock:
            self.managed_trades[trade.trade_id] = managed

        self.logger.info(
            f"Registered trade {trade.trade_id}: {trade.pair} "
            f"{trade.side.value.upper()} {trade.units:,} oz @ {trade.entry_price:.2f}"
        )
        return managed

    def unregister_trade(self, trade_id: str):
        with self._lock:
            if trade_id in self.managed_trades:
                del self.managed_trades[trade_id]
            self._save_state()
        self.logger.info(f"Unregistered trade {trade_id}")

    def update_trade_atr(self, trade_id: str, atr_value: float) -> None:
        with self._lock:
            if trade_id in self.managed_trades:
                self.managed_trades[trade_id].atr_value = atr_value

    def sync_trades(self) -> Dict[str, str]:
        with self._lock:
            result = {}
            broker_trades = {t.trade_id: t for t in self.broker.get_open_trades()}

            closed_ids = set(self.managed_trades.keys()) - set(broker_trades.keys())
            for trade_id in closed_ids:
                self.logger.info(f"Trade {trade_id} closed (no longer at broker)")
                del self.managed_trades[trade_id]
                result[trade_id] = "removed"

            for trade_id, trade in broker_trades.items():
                if trade_id in self.managed_trades:
                    self.managed_trades[trade_id].trade = trade
                    result[trade_id] = "synced"
                else:
                    persisted = self._persisted_state.get(trade_id, {})
                    managed = self._restore_from_persisted(trade, persisted)
                    self.managed_trades[trade_id] = managed
                    result[trade_id] = "added"

            return result

    def update_all_trades(self) -> List[TradeManagementResult]:
        broker_trades = {t.trade_id: t for t in self.broker.get_open_trades()}
        sl_recovery_needed: list = []

        with self._lock:
            closed_ids = set(self.managed_trades.keys()) - set(broker_trades.keys())
            for trade_id in closed_ids:
                self.logger.info(f"Trade {trade_id} closed (no longer at broker)")
                del self.managed_trades[trade_id]

            for trade_id, trade in broker_trades.items():
                if trade_id in self.managed_trades:
                    self.managed_trades[trade_id].trade = trade
                else:
                    persisted = self._persisted_state.get(trade_id, {})
                    managed = self._restore_from_persisted(trade, persisted)
                    self.managed_trades[trade_id] = managed

                    if trade.stop_loss is None:
                        recovered_sl = persisted.get('initial_sl') or managed.initial_sl
                        if recovered_sl is not None:
                            sl_recovery_needed.append((trade_id, trade.pair, recovered_sl))

            managed_snapshot = list(self.managed_trades.items())

        for trade_id, pair, recovered_sl in sl_recovery_needed:
            self.logger.warning(
                f"Trade {trade_id} ({pair}) has no SL — re-applying recovered SL {recovered_sl:.2f}"
            )
            try:
                self.broker.modify_trade(trade_id, pair, stop_loss=recovered_sl)
            except Exception as exc:
                self.logger.error(f"Failed to re-apply SL for trade {trade_id}: {exc}")

        results = []
        for trade_id, managed in managed_snapshot:
            self._update_price_tracking(managed)
            self._check_break_even(managed)
            self._check_partial_tp(managed)
            self._check_tp2(managed)
            if managed.trailing_stop_active:
                result = self._check_trailing_stop(managed)
                if result and result.action != TradeAction.NONE:
                    results.append(result)

            if managed.age_hours > self.max_trade_age_hours:
                self.logger.warning(
                    f"Trade {trade_id} is {managed.age_hours:.1f} hours old"
                )
                if self.alert_manager:
                    self.alert_manager.send_alert(
                        f"Old trade alert: {managed.trade.pair} open for "
                        f"{managed.age_hours:.0f} hours",
                        priority='WARNING',
                    )

        with self._lock:
            self._save_state()
        return results

    def _restore_from_persisted(self, trade: Trade, persisted: dict) -> 'ManagedTrade':
        managed = ManagedTrade(
            trade=trade,
            strategy_name=persisted.get("strategy_name", "unknown"),
            entry_time=trade.open_time or datetime.now(timezone.utc),
            initial_sl=trade.stop_loss,
            initial_tp=trade.take_profit,
            tp2=persisted.get("tp2"),
            tp3=persisted.get("tp3"),
            trailing_stop_active=persisted.get("trailing_stop_active", False),
            trailing_stop_distance=persisted.get("trailing_stop_distance", 0.0),
            highest_price=trade.entry_price,
            lowest_price=trade.entry_price,
        )
        if persisted:
            managed.highest_price = persisted.get("highest_price", trade.entry_price)
            managed.lowest_price  = persisted.get("lowest_price",  trade.entry_price)
            managed.break_even_triggered  = persisted.get("break_even_triggered",  False)
            managed.partial_tp_triggered  = persisted.get("partial_tp_triggered",  False)
            managed.tp2_triggered         = persisted.get("tp2_triggered",         False)
            managed.atr_value = persisted.get("atr_value", None)
            self.logger.info(f"Restored persisted state for trade {trade.trade_id}")
        return managed

    # ------------------------------------------------------------------
    # Price tracking
    # ------------------------------------------------------------------

    def _update_price_tracking(self, managed: ManagedTrade):
        current_price = managed.trade.current_price
        if current_price > managed.highest_price:
            managed.highest_price = current_price
        if current_price < managed.lowest_price or managed.lowest_price == 0:
            managed.lowest_price = current_price

    # ------------------------------------------------------------------
    # Trailing stop — gold: USD/oz points (not pips)
    # ------------------------------------------------------------------

    def _check_trailing_stop(self, managed: ManagedTrade) -> Optional[TradeManagementResult]:
        trade = managed.trade

        # Trail distance: ATR×1.5 if available, else fixed activation threshold
        if managed.atr_value and managed.atr_value > 0:
            trail_distance = managed.atr_value * 1.5  # USD/oz
        else:
            trail_distance = self.trailing_stop_activation_points * _GOLD_POINT  # fallback

        if trade.is_long:
            profit_points = trade.current_price - trade.entry_price
            new_sl = managed.highest_price - trail_distance
        else:
            profit_points = trade.entry_price - trade.current_price
            new_sl = managed.lowest_price + trail_distance

        # Only activate after minimum profit threshold (7 pts)
        if profit_points < self.trailing_stop_activation_points:
            return None

        current_sl = trade.stop_loss
        should_update = False

        if current_sl is None:
            should_update = True
        elif trade.is_long and new_sl > current_sl:
            should_update = True
        elif trade.is_short and new_sl < current_sl:
            should_update = True

        if should_update:
            # Clamp: new_sl must not cross TP1
            if trade.take_profit:
                buffer = 2.0 * _GOLD_POINT
                if trade.is_long and new_sl >= trade.take_profit - buffer:
                    new_sl = trade.take_profit - buffer
                elif trade.is_short and new_sl <= trade.take_profit + buffer:
                    new_sl = trade.take_profit + buffer

            success = self.broker.modify_trade(
                trade_id=trade.trade_id,
                pair=trade.pair,
                stop_loss=new_sl,
            )

            if success:
                self.logger.info(
                    f"Trailing stop updated for {trade.trade_id}: "
                    f"{current_sl} -> {new_sl:.2f} (profit={profit_points:.1f} pts)"
                )
                return TradeManagementResult(
                    trade_id=trade.trade_id,
                    action=TradeAction.TRAILING_STOP,
                    success=True,
                    details=f"SL moved from {current_sl} to {new_sl:.2f}",
                    new_sl=new_sl,
                )
            else:
                self.logger.error(f"Failed to update trailing stop for {trade.trade_id}")
                return TradeManagementResult(
                    trade_id=trade.trade_id,
                    action=TradeAction.TRAILING_STOP,
                    success=False,
                    details="Broker rejected modification",
                )

        return None

    # ------------------------------------------------------------------
    # Break-even — at 5 pts profit → SL to entry + 1 pt + 50% partial close
    # ------------------------------------------------------------------

    def _check_break_even(self, managed: ManagedTrade) -> None:
        if managed.break_even_triggered:
            return
        trade = managed.trade

        if trade.is_long:
            profit_points = trade.current_price - trade.entry_price
        else:
            profit_points = trade.entry_price - trade.current_price

        if profit_points < self.break_even_activation_points:
            return

        # Move SL to break-even: entry + buffer (BUY) or entry - buffer (SELL)
        if trade.is_long:
            new_sl = trade.entry_price + self.break_even_buffer_points
            if trade.stop_loss and new_sl <= trade.stop_loss:
                return
        else:
            new_sl = trade.entry_price - self.break_even_buffer_points
            if trade.stop_loss and new_sl >= trade.stop_loss:
                return

        success = self.broker.modify_trade(
            trade_id=trade.trade_id,
            pair=trade.pair,
            stop_loss=new_sl,
        )
        if success:
            managed.break_even_triggered = True
            self.logger.info(
                f"Break-even set for {trade.trade_id} ({trade.pair}): "
                f"SL -> {new_sl:.2f} at {profit_points:.1f} pts profit"
            )
            # Partial close 50% at break-even trigger
            if not managed.partial_tp_triggered:
                units_to_close = int(abs(trade.units) * settings.PARTIAL_TP_RATIO)
                if units_to_close >= 1:
                    closed = self.broker.partial_close_trade(trade.trade_id, units_to_close)
                    if closed:
                        managed.partial_tp_triggered = True
                        self.logger.info(
                            f"Break-even partial close: {trade.pair} {trade.trade_id} — "
                            f"closed {units_to_close} oz at {profit_points:.1f} pts"
                        )
                        if self.alert_manager:
                            try:
                                self.alert_manager._send_telegram(
                                    f"Break-even partial close: {trade.pair} — "
                                    f"closed {units_to_close} oz at {profit_points:.1f} pts profit "
                                    f"({int(settings.PARTIAL_TP_RATIO*100)}% of position)",
                                    parse_mode='',
                                )
                            except Exception:
                                pass
                    else:
                        self.logger.warning(f"Break-even partial close failed for {trade.trade_id}")

    # ------------------------------------------------------------------
    # Partial TP — close 50% at 1:1 RR before break-even activates
    # ------------------------------------------------------------------

    def _check_partial_tp(self, managed: ManagedTrade) -> None:
        if not settings.PARTIAL_TP_ENABLED:
            return
        if managed.partial_tp_triggered or managed.break_even_triggered:
            return
        if managed.initial_sl is None:
            return

        trade = managed.trade
        sl_distance = abs(trade.entry_price - managed.initial_sl)
        if sl_distance <= 0:
            return

        target_points = sl_distance * settings.PARTIAL_TP_RR_TARGET

        if trade.is_long:
            profit_points = trade.current_price - trade.entry_price
        else:
            profit_points = trade.entry_price - trade.current_price

        if profit_points < target_points:
            return

        units_to_close = int(abs(trade.units) * settings.PARTIAL_TP_RATIO)
        if units_to_close < 1:
            return

        success = self.broker.partial_close_trade(trade.trade_id, units_to_close)
        if success:
            managed.partial_tp_triggered = True
            # Also move SL to break-even immediately
            new_sl = (trade.entry_price + self.break_even_buffer_points if trade.is_long
                      else trade.entry_price - self.break_even_buffer_points)
            sl_moved = self.broker.modify_trade(
                trade_id=trade.trade_id,
                pair=trade.pair,
                stop_loss=new_sl,
            )
            if sl_moved:
                managed.break_even_triggered = True
                self.logger.info(
                    f"Partial TP + break-even: {trade.trade_id} — "
                    f"closed {units_to_close} oz, SL -> {new_sl:.2f}"
                )
            else:
                self.logger.warning(f"Partial TP: could not move SL to break-even for {trade.trade_id}")

            if self.alert_manager:
                try:
                    self.alert_manager._send_telegram(
                        f"Partial TP: {trade.pair} — closed {units_to_close} oz "
                        f"at {profit_points:.1f} pts ({int(settings.PARTIAL_TP_RATIO*100)}% of position)",
                        parse_mode='',
                    )
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # TP2 — close 50% of remaining position when TP2 level is hit
    # ------------------------------------------------------------------

    def _check_tp2(self, managed: ManagedTrade) -> None:
        if not managed.partial_tp_triggered:
            return
        if managed.tp2_triggered or managed.tp2 is None:
            return
        trade = managed.trade
        current_price = trade.current_price
        if current_price is None:
            return
        tp2_hit = (trade.is_long and current_price >= managed.tp2) or \
                   (not trade.is_long and current_price <= managed.tp2)
        if not tp2_hit:
            return
        remaining_units = abs(trade.units)
        if remaining_units < 2:
            # Position too small to split — broker TP3 order will close the remaining unit
            return
        close_units = int(remaining_units * 0.5)
        success = self.broker.partial_close_trade(trade.trade_id, close_units)
        if success:
            managed.tp2_triggered = True
            self.logger.info(
                f"TP2 hit: {trade.pair} {trade.trade_id} — "
                f"closed {close_units} oz @ {current_price:.2f}"
            )
            if self.alert_manager:
                tp3_str = f"\nTP3 target: {managed.tp3:.2f}" if managed.tp3 else ""
                self.alert_manager.send_alert(
                    f"TP2 Hit — {trade.pair}\n"
                    f"Closed {close_units} oz @ {current_price:.2f}{tp3_str}",
                    priority='INFO',
                )

    # ------------------------------------------------------------------
    # Close operations
    # ------------------------------------------------------------------

    def close_trade(self, trade_id: str, reason: str = "manual") -> TradeManagementResult:
        with self._lock:
            managed = self.managed_trades.get(trade_id)

        if not managed:
            self.logger.warning(f"Trade {trade_id} not in managed trades")

        result = self.broker.close_trade(trade_id)

        if result.success:
            realized_pnl = result.realized_pnl
            close_price = result.close_price

            reason_label = {
                'sl': 'Stop Loss Hit', 'stop_loss': 'Stop Loss Hit',
                'tp': 'Take Profit Hit', 'take_profit': 'Take Profit Hit',
                'news': 'News Close',
                'emergency': 'Emergency Close',
            }.get(reason, 'Closed by User')

            if managed:
                entry = managed.trade.entry_price
                points = close_price - entry
                if not managed.trade.is_long:
                    points = -points

                sl = managed.trade.stop_loss
                tp = managed.trade.take_profit

                self.logger.info(
                    f"Trade closed: {trade_id} | {managed.trade.pair} "
                    f"{managed.trade.side.value.upper()} | "
                    f"Entry: {entry:.2f} | "
                    f"Close: {close_price:.2f} ({points:+.1f} pts) | "
                    f"P/L: ${realized_pnl:+.2f} | {reason_label}"
                )

                if self.alert_manager:
                    self.alert_manager.alert_trade_closed(
                        trade_id=trade_id,
                        pnl=realized_pnl,
                        close_price=close_price,
                        entry_price=entry,
                        points=points,
                        reason=reason_label,
                    )
            else:
                self.logger.info(
                    f"Trade closed: {trade_id} | Close: {close_price:.2f} | "
                    f"P/L: ${realized_pnl:+.2f} | {reason_label}"
                )

            self.unregister_trade(trade_id)
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.CLOSE,
                success=True,
                details=f"Closed: {reason_label}",
                pnl=realized_pnl,
            )
        else:
            self.logger.error(f"Failed to close trade {trade_id}")
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.CLOSE,
                success=False,
                details="Broker rejected close request",
            )

    def close_all_trades(
        self,
        reason: str = "close_all",
        pairs: Optional[Set[str]] = None,
    ) -> List[TradeManagementResult]:
        results = []
        with self._lock:
            snapshot = [
                trade_id for trade_id, managed in self.managed_trades.items()
                if pairs is None or managed.trade.pair in pairs
            ]
        for trade_id in snapshot:
            result = self.close_trade(trade_id, reason=reason)
            results.append(result)
        return results

    def emergency_close_all(self, reason: str = "emergency") -> List[TradeManagementResult]:
        self.logger.critical(f"EMERGENCY CLOSE ALL: {reason}")

        if self.alert_manager:
            self.alert_manager.send_alert(
                f"EMERGENCY: Closing all XAU/USD positions - {reason}",
                priority='CRITICAL',
            )

        results = []
        positions = self.broker.get_positions()

        for position in positions:
            success = self.broker.close_position(position.pair)
            results.append(TradeManagementResult(
                trade_id=f"position_{position.pair}",
                action=TradeAction.EMERGENCY_CLOSE,
                success=success,
                details=f"Emergency close {position.pair}",
                pnl=position.unrealized_pnl,
            ))
            if success:
                self.logger.info(f"Emergency closed {position.pair}")
            else:
                self.logger.error(f"Failed to emergency close {position.pair}")

        with self._lock:
            self.managed_trades.clear()
        self._save_state()
        return results

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        try:
            data = {}
            for trade_id, managed in self.managed_trades.items():
                data[trade_id] = {
                    "strategy_name": managed.strategy_name,
                    "trailing_stop_active": managed.trailing_stop_active,
                    "trailing_stop_distance": managed.trailing_stop_distance,
                    "highest_price": managed.highest_price,
                    "lowest_price": managed.lowest_price,
                    "initial_sl": managed.initial_sl,
                    "initial_tp": managed.initial_tp,
                    "tp2": managed.tp2,
                    "tp3": managed.tp3,
                    "break_even_triggered": managed.break_even_triggered,
                    "partial_tp_triggered": managed.partial_tp_triggered,
                    "tp2_triggered": managed.tp2_triggered,
                    "atr_value": managed.atr_value,
                }
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(data, indent=2))
            self._persisted_state = data
        except Exception as e:
            self.logger.warning(f"Could not save managed trades state: {e}")

    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                self._persisted_state = json.loads(self._state_file.read_text())
                self.logger.info(f"Loaded persisted state for {len(self._persisted_state)} trade(s)")
        except Exception as e:
            self.logger.warning(f"Could not load managed trades state: {e}")
            self._persisted_state = {}

    def get_managed_trade(self, trade_id: str) -> Optional[ManagedTrade]:
        with self._lock:
            return self.managed_trades.get(trade_id)

    def list_managed_trades(self) -> List[ManagedTrade]:
        with self._lock:
            return list(self.managed_trades.values())
