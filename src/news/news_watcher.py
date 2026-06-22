"""NewsWatcher — background daemon that closes XAU/USD trades ahead of VERY_HIGH events.

Rule 3 (Agent 3): deterministic conviction tiers based on R-multiple.
  < 1R profit  → close immediately
  >= 1R profit → partial close 50% + move SL to break-even

Gold-specific:
- R-multiple uses USD/oz points directly (sl_distance in USD, not pips)
- Break-even buffer in USD/oz points (not pip_size)
- No pip_size conversion needed (XAU_USD = $1/oz per point)
"""

import logging
import threading
from typing import Callable, Optional, Set

from config.settings import settings
from .event_monitor import EventImpact
from src.broker.base import OrderSide


class NewsWatcher:
    """Background daemon: evaluate and close gold trades before VERY_HIGH events."""

    def __init__(
        self,
        event_monitor,
        broker,
        alert_manager,
        get_trades_snapshot_fn: Callable,
        on_trade_closed_fn: Callable,
        logger: Optional[logging.Logger] = None,
    ):
        self.event_monitor = event_monitor
        self.broker = broker
        self.alert_manager = alert_manager
        self.get_trades_snapshot_fn = get_trades_snapshot_fn
        self.on_trade_closed_fn = on_trade_closed_fn
        self.logger = logger or logging.getLogger("NewsWatcher")

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._evaluated: Set[str] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="NewsWatcher", daemon=True
        )
        self._thread.start()
        self.logger.info("NewsWatcher started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.logger.info("NewsWatcher stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check()
            except Exception as exc:
                self.logger.error(f"NewsWatcher check error: {exc}")
            self._stop_event.wait(settings.NEWS_RISK_POLL_INTERVAL_SECONDS)

    def _check(self) -> None:
        self._evaluated.clear()

        imminent = self.event_monitor.get_imminent_events(
            minutes=settings.NEWS_RISK_MINUTES_BEFORE,
            min_impact=EventImpact.VERY_HIGH,
        )
        if not imminent:
            return

        trades = self.get_trades_snapshot_fn()
        if not trades:
            return

        for event in imminent:
            for trade_id, trade in trades.items():
                # XAU_USD — always affected by gold-relevant events
                if 'XAU' not in trade.pair and 'GOLD' not in trade.pair:
                    continue
                if trade_id in self._evaluated:
                    continue
                self._evaluated.add(trade_id)

                r_multiple = self._compute_r_multiple(trade)

                self.logger.info(
                    f"NewsWatcher: {trade.pair} {trade_id} "
                    f"R={r_multiple:.2f} | event={event.event_name} "
                    f"in {event.minutes_until:.0f}min"
                )

                if r_multiple < 1.0:
                    self._close_and_alert(
                        trade, event,
                        reason=f"<1R profit (R={r_multiple:.2f}) before high-impact news",
                    )
                else:
                    self._partial_close_and_protect(trade, event, r_multiple)

    def _compute_r_multiple(self, trade) -> float:
        """Return profit as R-multiple. Uses USD/oz points directly for gold."""
        try:
            if trade.stop_loss is None:
                return 0.0

            # Gold: SL distance is in USD/oz (1 point = $1/oz)
            sl_distance = abs(trade.entry_price - trade.stop_loss)
            if sl_distance <= 0:
                return 0.0

            if trade.side == OrderSide.BUY:
                profit = trade.current_price - trade.entry_price
            else:
                profit = trade.entry_price - trade.current_price

            return profit / sl_distance
        except Exception as exc:
            self.logger.warning(f"NewsWatcher: R-multiple calc failed: {exc}")
            return 0.0

    def _close_and_alert(self, trade, event, reason: str) -> None:
        try:
            success = self.broker.close_trade(trade.trade_id)
        except Exception as exc:
            self.logger.error(f"NewsWatcher: failed to close {trade.trade_id}: {exc}")
            return

        if success.success:
            self.on_trade_closed_fn(trade.trade_id)
            pnl = success.realized_pnl
            close_price = success.close_price
            pnl_str = f"${pnl:+.2f}" if pnl else "N/A"
            close_str = f"{close_price:.2f}" if close_price else "N/A"

            msg = (
                f"NEWS RISK CLOSE -- XAU/USD\n"
                f"Event: {event.event_name} in {event.minutes_until:.0f}min\n"
                f"Reason: {reason}\n"
                f"Close: {close_str} | P/L: {pnl_str}"
            )
            try:
                self.alert_manager._send_telegram(msg, parse_mode='')
            except Exception as exc:
                self.logger.warning(f"NewsWatcher: Telegram alert failed: {exc}")
        else:
            self.logger.warning(
                f"NewsWatcher: broker.close_trade returned falsy for {trade.trade_id}"
            )

    def _partial_close_and_protect(self, trade, event, r_multiple: float) -> None:
        """Partial close 50% and move SL to break-even (gold: in USD/oz points)."""
        partial_units = round(trade.units * 0.5)
        partial_ok = False

        try:
            partial_ok = self.broker.partial_close_trade(trade.trade_id, partial_units)
        except Exception as exc:
            self.logger.error(f"NewsWatcher: partial close failed for {trade.trade_id}: {exc}")

        if not partial_ok:
            self.logger.warning(
                f"NewsWatcher: partial close failed — falling back to full close for {trade.trade_id}"
            )
            self._close_and_alert(
                trade, event,
                reason=f"partial close failed, fell back to full close (R={r_multiple:.2f})",
            )
            return

        # Move SL to break-even — gold: fixed buffer in USD/oz points.
        # News path stays fixed (deliberate risk-off; no managed ATR here), unlike
        # the ATR-scaled break-even in TradeManager.
        try:
            buf = settings.BREAK_EVEN_BUFFER_POINTS
            if trade.side == OrderSide.BUY:
                be_sl = trade.entry_price + buf
            else:
                be_sl = trade.entry_price - buf

            self.broker.modify_trade(
                trade_id=trade.trade_id,
                pair=trade.pair,
                stop_loss=be_sl,
            )
        except Exception as exc:
            self.logger.warning(
                f"NewsWatcher: SL move to break-even failed for {trade.trade_id}: {exc}"
            )

        msg = (
            f"NEWS RISK PROTECT -- XAU/USD\n"
            f"Event: {event.event_name} in {event.minutes_until:.0f}min\n"
            f"Action: Partial close {partial_units} oz + SL moved to break-even\n"
            f"R at action: {r_multiple:.2f}"
        )
        try:
            self.alert_manager._send_telegram(msg, parse_mode='')
        except Exception as exc:
            self.logger.warning(f"NewsWatcher: Telegram alert failed: {exc}")
