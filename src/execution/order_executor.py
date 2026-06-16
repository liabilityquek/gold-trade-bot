"""Order execution with retry logic and slippage tracking for XAU/USD.

Gold-specific:
  - Slippage tracked in USD/oz points (not pips)
  - max_slippage threshold in USD/oz (e.g. 2.0 = $2/oz)
  - No JPY/non-JPY pip size branching — gold uses fixed $1/oz per point
"""

import logging
import time
import uuid
import threading
from collections import deque
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.broker.base import BaseBroker, OrderSide
from src.monitoring.logger import get_logger
from config.settings import settings


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class ExecutionStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


@dataclass
class OrderRequest:
    pair: str
    side: OrderSide
    units: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy_name: str = ""
    signal_id: str = ""
    expected_price: Optional[float] = None
    max_slippage_points: float = settings.MAX_SLIPPAGE_POINTS    # USD/oz (gold: $2/oz max slippage)

    def __post_init__(self):
        if not self.signal_id:
            self.signal_id = uuid.uuid4().hex


@dataclass
class ExecutionResult:
    success: bool
    trade_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_units: int = 0
    status: ExecutionStatus = ExecutionStatus.PENDING
    slippage_points: float = 0.0       # USD/oz points
    retry_count: int = 0
    error_message: str = ""
    execution_time_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    def __repr__(self) -> str:
        if self.success:
            return (
                f"ExecutionResult(SUCCESS | Trade: {self.trade_id} | "
                f"Price: {self.fill_price:.2f} | Slippage: {self.slippage_points:+.2f} pts)"
            )
        return f"ExecutionResult(FAILED | {self.error_message})"


@dataclass
class SlippageStats:
    total_orders: int = 0
    total_slippage_points: float = 0.0
    positive_slippage_count: int = 0
    negative_slippage_count: int = 0
    max_slippage: float = 0.0
    min_slippage: float = 0.0

    @property
    def average_slippage(self) -> float:
        if self.total_orders == 0:
            return 0.0
        return self.total_slippage_points / self.total_orders


class OrderExecutor:
    """Execute XAU/USD orders with retry logic and slippage tracking."""

    def __init__(
        self,
        broker: BaseBroker,
        logger: Optional[logging.Logger] = None,
        max_retries: int = 3,
        initial_retry_delay: float = 1.0,
        max_retry_delay: float = 30.0,
        backoff_multiplier: float = 2.0
    ):
        self.broker = broker
        self.logger = logger or get_logger('order_executor')
        self.max_retries = max_retries
        self.initial_retry_delay = initial_retry_delay
        self.max_retry_delay = max_retry_delay
        self.backoff_multiplier = backoff_multiplier

        self.slippage_stats = SlippageStats()
        self.execution_history: deque = deque(maxlen=500)

        self._rate_limit_lock = threading.Lock()
        self._order_timestamps: deque = deque()
        self._max_orders_per_minute: int = settings.MAX_ORDERS_PER_MINUTE

        self._circuit_open: bool = False
        self._circuit_open_time: Optional[datetime] = None
        self._consecutive_failures: int = 0
        self._circuit_failure_threshold: int = settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD
        self._circuit_cooldown_seconds: float = settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS

        self._submitted_signal_ids: set = set()

    def _check_rate_limit(self) -> bool:
        with self._rate_limit_lock:
            now = time.time()
            while self._order_timestamps and now - self._order_timestamps[0] > 60.0:
                self._order_timestamps.popleft()
            if len(self._order_timestamps) >= self._max_orders_per_minute:
                return False
            return True

    def _record_order_attempt(self, success: bool):
        with self._rate_limit_lock:
            self._order_timestamps.append(time.time())
            if success:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._circuit_failure_threshold:
                    self._circuit_open = True
                    self._circuit_open_time = datetime.now()
                    self.logger.error(
                        f"Circuit breaker opened after {self._consecutive_failures} consecutive failures."
                    )

    def reset_circuit_breaker(self):
        with self._rate_limit_lock:
            self._circuit_open = False
            self._circuit_open_time = None
            self._consecutive_failures = 0
        self.logger.info("Circuit breaker reset.")

    def execute_market_order(self, request: OrderRequest) -> ExecutionResult:
        """Execute a market order with retry logic."""
        with self._rate_limit_lock:
            circuit_open = self._circuit_open
            circuit_open_time = self._circuit_open_time

        if circuit_open and circuit_open_time is not None:
            elapsed = (datetime.now() - circuit_open_time).total_seconds()
            if elapsed < self._circuit_cooldown_seconds:
                msg = f"Circuit breaker is open. Retry in {self._circuit_cooldown_seconds - elapsed:.0f}s."
                self.logger.warning(msg)
                return ExecutionResult(success=False, status=ExecutionStatus.FAILED, error_message=msg)
            else:
                self.reset_circuit_breaker()

        if not self._check_rate_limit():
            msg = "Order rate limit exceeded. Too many orders per minute."
            self.logger.warning(msg)
            return ExecutionResult(success=False, status=ExecutionStatus.FAILED, error_message=msg)

        if request.signal_id and request.signal_id in self._submitted_signal_ids:
            msg = f"Duplicate signal_id {request.signal_id} — order already submitted, skipping"
            self.logger.warning(msg)
            return ExecutionResult(success=False, status=ExecutionStatus.FAILED, error_message=msg)

        start_time = time.time()
        retry_count = 0
        current_delay = self.initial_retry_delay
        last_error = ""

        self.logger.info(
            f"Executing market order: {request.side.value.upper()} "
            f"{request.units:,} oz {request.pair}"
        )

        while retry_count <= self.max_retries:
            try:
                if request.expected_price is None:
                    price_data = self.broker.get_current_price(request.pair)
                    if price_data:
                        if request.side == OrderSide.BUY:
                            request.expected_price = price_data['ask']
                        else:
                            request.expected_price = price_data['bid']

                trade_id = self.broker.place_market_order(
                    pair=request.pair,
                    side=request.side,
                    units=request.units,
                    stop_loss=request.stop_loss,
                    take_profit=request.take_profit,
                )

                if trade_id:
                    fill_price = self._get_fill_price(request.pair, trade_id)
                    slippage = self._calculate_slippage(request.expected_price, fill_price, request.side)

                    execution_time = (time.time() - start_time) * 1000

                    result = ExecutionResult(
                        success=True,
                        trade_id=trade_id,
                        fill_price=fill_price,
                        filled_units=request.units,
                        status=ExecutionStatus.FILLED,
                        slippage_points=slippage,
                        retry_count=retry_count,
                        execution_time_ms=execution_time,
                    )

                    self._update_slippage_stats(slippage)
                    self.logger.info(
                        f"Order filled: {request.pair} {request.side.value.upper()} "
                        f"{request.units:,} oz @ {fill_price:.2f} | "
                        f"Slippage: {slippage:+.2f} pts | "
                        f"Time: {execution_time:.0f}ms"
                    )

                    self._record_order_attempt(True)
                    self.execution_history.append(result)
                    if request.signal_id:
                        self._submitted_signal_ids.add(request.signal_id)
                    return result

                last_error = "Order rejected by broker (no trade ID returned)"
                self.logger.warning(f"Order attempt {retry_count + 1} failed: {last_error}")
                self._record_order_attempt(False)

            except Exception as e:
                last_error = str(e)
                self.logger.error(f"Order attempt {retry_count + 1} error: {e}")
                self._record_order_attempt(False)

            retry_count += 1
            if retry_count <= self.max_retries:
                self.logger.info(
                    f"Retrying in {current_delay:.1f}s (attempt {retry_count + 1}/{self.max_retries + 1})"
                )
                time.sleep(current_delay)
                current_delay = min(current_delay * self.backoff_multiplier, self.max_retry_delay)

        execution_time = (time.time() - start_time) * 1000
        result = ExecutionResult(
            success=False,
            status=ExecutionStatus.FAILED,
            retry_count=retry_count - 1,
            error_message=last_error,
            execution_time_ms=execution_time,
        )

        self.logger.error(
            f"Order failed after {retry_count} attempts: {request.pair} "
            f"{request.side.value.upper()} {request.units:,} oz | Error: {last_error}"
        )

        self.execution_history.append(result)
        return result

    def execute_limit_order(self, request: OrderRequest) -> ExecutionResult:
        if request.limit_price is None:
            return ExecutionResult(success=False, status=ExecutionStatus.FAILED, error_message="Limit price required")

        start_time = time.time()
        self.logger.info(
            f"Placing limit order: {request.side.value.upper()} "
            f"{request.units:,} oz {request.pair} @ {request.limit_price:.2f}"
        )

        try:
            order_id = self.broker.place_limit_order(
                pair=request.pair,
                side=request.side,
                units=request.units,
                price=request.limit_price,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.FAILED,
                error_message=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )

        execution_time = (time.time() - start_time) * 1000

        if order_id:
            self._record_order_attempt(True)
            result = ExecutionResult(
                success=True,
                trade_id=order_id,
                status=ExecutionStatus.PENDING,
                execution_time_ms=execution_time,
            )
            self.execution_history.append(result)
            self.logger.info(f"Limit order accepted: {request.pair} | Order ID: {order_id}")
            return result

        self._record_order_attempt(False)
        result = ExecutionResult(
            success=False,
            status=ExecutionStatus.FAILED,
            error_message="Broker rejected limit order (no order ID returned)",
            execution_time_ms=execution_time,
        )
        self.execution_history.append(result)
        return result

    def execute(self, request: OrderRequest) -> ExecutionResult:
        if request.order_type == OrderType.MARKET:
            return self.execute_market_order(request)
        elif request.order_type == OrderType.LIMIT:
            return self.execute_limit_order(request)
        else:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.FAILED,
                error_message=f"Unknown order type: {request.order_type}",
            )

    def _get_fill_price(self, pair: str, trade_id: str) -> Optional[float]:
        try:
            trades = self.broker.get_open_trades()
            for trade in trades:
                if trade.trade_id == trade_id:
                    return trade.entry_price
            price_data = self.broker.get_current_price(pair)
            if price_data:
                return (price_data['bid'] + price_data['ask']) / 2
        except Exception as e:
            self.logger.warning(f"Could not get fill price: {e}")
        return None

    def _calculate_slippage(
        self,
        expected_price: Optional[float],
        fill_price: Optional[float],
        side: OrderSide,
    ) -> float:
        """Calculate slippage in USD/oz points. Positive = in our favour."""
        if expected_price is None or fill_price is None:
            return 0.0

        # Gold: 1 point = $1/oz — no pip size conversion needed
        price_diff = fill_price - expected_price
        slippage_points = -price_diff if side == OrderSide.BUY else price_diff
        return slippage_points

    def _update_slippage_stats(self, slippage: float):
        self.slippage_stats.total_orders += 1
        self.slippage_stats.total_slippage_points += slippage

        if slippage > 0:
            self.slippage_stats.positive_slippage_count += 1
        elif slippage < 0:
            self.slippage_stats.negative_slippage_count += 1

        if self.slippage_stats.total_orders == 1:
            self.slippage_stats.max_slippage = slippage
            self.slippage_stats.min_slippage = slippage
        else:
            self.slippage_stats.max_slippage = max(self.slippage_stats.max_slippage, slippage)
            self.slippage_stats.min_slippage = min(self.slippage_stats.min_slippage, slippage)

    def get_slippage_report(self) -> Dict:
        stats = self.slippage_stats
        return {
            'total_orders': stats.total_orders,
            'average_slippage_points': stats.average_slippage,
            'total_slippage_points': stats.total_slippage_points,
            'positive_slippage_count': stats.positive_slippage_count,
            'negative_slippage_count': stats.negative_slippage_count,
            'max_slippage_points': stats.max_slippage,
            'min_slippage_points': stats.min_slippage,
        }

    def get_execution_history(self, limit: int = 50, success_only: bool = False) -> List[ExecutionResult]:
        history = list(self.execution_history)
        if success_only:
            history = [r for r in history if r.success]
        return history[-limit:]

    def reset_stats(self):
        self.slippage_stats = SlippageStats()
        self.execution_history.clear()
        self.logger.info("Execution statistics reset")
