"""OANDA broker implementation for XAU/USD gold trading.

Gold-specific:
- _fmt_price() uses 2 decimal places (3285.42 not 3285.42000)
- Single instrument XAU_USD — no PAIR_INFO dependency
- Pip/point proximity threshold uses $1/oz (not pip_size)
- All price formatting passes through fmt_price()
"""

import logging
import time
from typing import List, Optional, Dict, Callable, Any
from datetime import datetime

from oandapyV20 import API
from oandapyV20.exceptions import V20Error
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.instruments as instruments

from config.settings import settings
from .base import (
    BaseBroker, Trade, Position, AccountInfo,
    OrderSide, OrderStatus, TradeCloseResult,
)


def _fmt_price(price: float) -> str:
    """Format gold price to 2 decimal places for OANDA API."""
    return f"{price:.2f}"


class OandaBroker(BaseBroker):
    """OANDA broker implementation for XAU/USD."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('oanda_broker')
        self.api = None
        self.account_id = settings.OANDA_ACCOUNT_ID
        self.connected = False

    def _with_retry(self, fn: Callable[[], Any], retries: int = 3, base_delay: float = 2.0) -> Any:
        """Execute fn() with exponential-backoff retry on transient errors."""
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return fn()
            except V20Error as exc:
                if getattr(exc, 'code', None) == 429:
                    last_exc = exc
                    if attempt < retries - 1:
                        delay = base_delay * (2 ** attempt)
                        self.logger.warning(
                            f"OANDA rate limit (429), attempt {attempt + 1}/{retries} "
                            f"— retrying in {delay:.0f}s"
                        )
                        time.sleep(delay)
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    delay = base_delay * (2 ** attempt)
                    self.logger.warning(
                        f"Broker call failed (attempt {attempt + 1}/{retries}): {exc} "
                        f"— retrying in {delay:.0f}s"
                    )
                    time.sleep(delay)
        raise last_exc

    def connect(self) -> bool:
        """Establish connection to OANDA API."""
        try:
            self.api = API(
                access_token=settings.OANDA_API_KEY,
                environment=settings.OANDA_ENVIRONMENT,
                request_params={"timeout": 15},
            )

            account_info = self.get_account_info()

            if account_info:
                self.connected = True
                self.logger.info(
                    f"Connected to OANDA ({settings.OANDA_ENVIRONMENT}) — "
                    f"Balance: ${account_info.balance:,.2f}"
                )
                return True

            return False

        except Exception as e:
            self.logger.error(f"Failed to connect to OANDA: {e}")
            return False

    def get_account_info(self) -> Optional[AccountInfo]:
        try:
            endpoint = accounts.AccountDetails(accountID=self.account_id)
            response = self._with_retry(lambda: self.api.request(endpoint))

            account_data = response['account']

            return AccountInfo(
                account_id=account_data['id'],
                balance=float(account_data['balance']),
                nav=float(account_data['NAV']),
                margin_used=float(account_data.get('marginUsed', 0)),
                margin_available=float(account_data.get('marginAvailable', 0)),
                unrealized_pnl=float(account_data.get('unrealizedPL', 0)),
                open_trade_count=int(account_data.get('openTradeCount', 0)),
                currency=account_data.get('currency', 'USD'),
            )

        except V20Error as e:
            self.logger.error(f"OANDA API error getting account info: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error getting account info: {e}")
            return None

    def get_current_price(self, pair: str = 'XAU_USD') -> Optional[Dict[str, float]]:
        """Get current bid/ask for XAU_USD."""
        try:
            params = {"instruments": pair}
            endpoint = pricing.PricingInfo(accountID=self.account_id, params=params)
            response = self._with_retry(lambda: self.api.request(endpoint))

            if response['prices']:
                price_data = response['prices'][0]
                bid = float(price_data['bids'][0]['price'])
                ask = float(price_data['asks'][0]['price'])
                return {
                    'bid': bid,
                    'ask': ask,
                    'mid': (bid + ask) / 2,
                    'spread': ask - bid,
                    'time': price_data['time'],
                }

            return None

        except V20Error as e:
            self.logger.error(f"OANDA API error getting price for {pair}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error getting price for {pair}: {e}")
            return None

    def _get_current_prices(self, instrument_list: List[str]) -> Dict[str, float]:
        """Fetch mid prices for a list of instruments."""
        try:
            params = {"instruments": ",".join(instrument_list)}
            endpoint = pricing.PricingInfo(accountID=self.account_id, params=params)
            response = self._with_retry(lambda: self.api.request(endpoint))
            result = {}
            for price_data in response.get('prices', []):
                bid = float(price_data['bids'][0]['price'])
                ask = float(price_data['asks'][0]['price'])
                result[price_data['instrument']] = (bid + ask) / 2
            return result
        except Exception as e:
            self.logger.warning(f"Could not fetch current prices: {e}")
            return {}

    def get_open_trades(self) -> List[Trade]:
        try:
            endpoint = trades.OpenTrades(accountID=self.account_id)
            response = self._with_retry(lambda: self.api.request(endpoint))

            trade_list_raw = response.get('trades', [])
            if not trade_list_raw:
                return []

            instruments_set = {t['instrument'] for t in trade_list_raw}
            current_prices = self._get_current_prices(list(instruments_set))

            trade_list = []
            for trade_data in trade_list_raw:
                instrument = trade_data['instrument']
                trade = Trade(
                    trade_id=trade_data['id'],
                    pair=instrument,
                    side=OrderSide.BUY if float(trade_data['currentUnits']) > 0 else OrderSide.SELL,
                    units=abs(int(float(trade_data['currentUnits']))),
                    entry_price=float(trade_data['price']),
                    current_price=current_prices.get(instrument, float(trade_data['price'])),
                    stop_loss=float(trade_data.get('stopLossOrder', {}).get('price', 0)) or None,
                    take_profit=float(trade_data.get('takeProfitOrder', {}).get('price', 0)) or None,
                    unrealized_pnl=float(trade_data.get('unrealizedPL', 0)),
                    open_time=datetime.fromisoformat(trade_data['openTime'].replace('Z', '+00:00')),
                )
                trade_list.append(trade)

            return trade_list

        except V20Error as e:
            self.logger.error(f"OANDA API error getting open trades: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error getting open trades: {e}")
            return []

    def get_positions(self) -> List[Position]:
        try:
            endpoint = positions.OpenPositions(accountID=self.account_id)
            response = self._with_retry(lambda: self.api.request(endpoint))

            position_list = []
            all_trades = self.get_open_trades()

            for pos_data in response.get('positions', []):
                long_units = int(float(pos_data.get('long', {}).get('units', 0)))
                short_units = int(float(pos_data.get('short', {}).get('units', 0)))
                net_units = long_units + short_units

                if net_units == 0:
                    continue

                position_trades = [t for t in all_trades if t.pair == pos_data['instrument']]

                if long_units != 0:
                    avg_price = float(pos_data['long'].get('averagePrice', 0))
                else:
                    avg_price = float(pos_data['short'].get('averagePrice', 0))

                position = Position(
                    pair=pos_data['instrument'],
                    net_units=net_units,
                    average_price=avg_price,
                    unrealized_pnl=float(pos_data.get('unrealizedPL', 0)),
                    trades=position_trades,
                )
                position_list.append(position)

            return position_list

        except V20Error as e:
            self.logger.error(f"OANDA API error getting positions: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error getting positions: {e}")
            return []

    def get_position(self, pair: str) -> Optional[Position]:
        positions_list = self.get_positions()
        for position in positions_list:
            if position.pair == pair:
                return position
        return None

    def place_market_order(
        self,
        pair: str,
        side: OrderSide,
        units: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Optional[str]:
        """Place a market order for XAU_USD."""
        try:
            oanda_units = units if side == OrderSide.BUY else -units

            order_spec = {
                "order": {
                    "type": "MARKET",
                    "instrument": pair,
                    "units": str(oanda_units),
                    "timeInForce": "FOK",
                    "positionFill": "DEFAULT",
                }
            }

            if stop_loss:
                order_spec["order"]["stopLossOnFill"] = {
                    "price": _fmt_price(stop_loss),
                }

            if take_profit:
                order_spec["order"]["takeProfitOnFill"] = {
                    "price": _fmt_price(take_profit),
                }

            endpoint = orders.OrderCreate(accountID=self.account_id, data=order_spec)
            response = self._with_retry(lambda: self.api.request(endpoint))

            if 'orderFillTransaction' in response:
                trade_opened = response['orderFillTransaction'].get('tradeOpened')
                if trade_opened:
                    trade_id = trade_opened['tradeID']
                    self.logger.info(
                        f"Order placed: XAU_USD {side.value.upper()} {units} oz | "
                        f"Trade ID: {trade_id}"
                    )
                    return trade_id

            self.logger.warning(f"Order placed but no trade ID returned: {response}")
            return None

        except V20Error as e:
            self.logger.error(f"OANDA API error placing order: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error placing order: {e}")
            return None

    def close_trade(self, trade_id: str) -> TradeCloseResult:
        """Close a specific trade."""
        try:
            endpoint = trades.TradeClose(accountID=self.account_id, tradeID=trade_id)
            response = self._with_retry(lambda: self.api.request(endpoint))

            if 'orderFillTransaction' in response:
                fill = response['orderFillTransaction']
                realized_pnl = float(fill.get('pl', 0))
                close_price = float(fill.get('price', 0))
                self.logger.info(
                    f"Trade {trade_id} closed | Price: {close_price:.2f} | P/L: ${realized_pnl:+.2f}"
                )
                return TradeCloseResult(
                    success=True, realized_pnl=realized_pnl, close_price=close_price
                )

            return TradeCloseResult(success=False)

        except V20Error as e:
            self.logger.error(f"OANDA API error closing trade {trade_id}: {e}")
            return TradeCloseResult(success=False)
        except Exception as e:
            self.logger.error(f"Error closing trade {trade_id}: {e}")
            return TradeCloseResult(success=False)

    def get_closed_trade_info(self, trade_id: str) -> dict:
        """Fetch close details for a broker-auto-closed trade (SL/TP hit).

        Gold-specific: proximity threshold uses $1/oz point (not pip_size).
        """
        def _parse_trade_data(trade_data: dict) -> dict:
            close_price = float(trade_data.get('averageClosePrice', 0))
            realized_pnl = float(trade_data.get('realizedPL', 0))
            sl_price = float((trade_data.get('stopLossOrder') or {}).get('price', 0))
            tp_price = float((trade_data.get('takeProfitOrder') or {}).get('price', 0))
            # Gold: $1/oz proximity threshold (1 point)
            proximity = 1.0
            if sl_price and abs(close_price - sl_price) <= proximity:
                reason = 'stop_loss'
            elif tp_price and abs(close_price - tp_price) <= proximity:
                reason = 'take_profit'
            else:
                reason = 'user'
            return {'close_price': close_price, 'realized_pnl': realized_pnl, 'reason': reason}

        # Primary: TradeDetails
        try:
            ep = trades.TradeDetails(accountID=self.account_id, tradeID=trade_id)
            response = self._with_retry(lambda: self.api.request(ep))
            trade_data = response.get('trade', {})
            if trade_data.get('averageClosePrice'):
                return _parse_trade_data(trade_data)
        except V20Error as e:
            if e.code != 404:
                raise
        except Exception as e:
            self.logger.warning(f"Error fetching closed trade info for {trade_id}: {e}")

        # Fallback: closed trades list
        try:
            ep = trades.TradesList(
                accountID=self.account_id,
                params={'state': 'CLOSED', 'ids': trade_id},
            )
            response = self._with_retry(lambda: self.api.request(ep))
            trade_list = response.get('trades', [])
            if trade_list:
                return _parse_trade_data(trade_list[0])
        except Exception as e:
            self.logger.warning(f"Could not fetch closed trade info for {trade_id}: {e}")

        # Retry once after 2s
        time.sleep(2)
        try:
            ep = trades.TradesList(
                accountID=self.account_id,
                params={'state': 'CLOSED', 'ids': trade_id},
            )
            response = self._with_retry(lambda: self.api.request(ep))
            trade_list = response.get('trades', [])
            if trade_list:
                return _parse_trade_data(trade_list[0])
            self.logger.warning(f"No closed trade record for {trade_id} after retry")
        except Exception as e:
            self.logger.warning(f"Retry failed for {trade_id}: {e}")

        return {}

    def close_position(self, pair: str) -> bool:
        """Close entire XAU_USD position."""
        try:
            closed_something = False

            for direction, data in [("long", {"longUnits": "ALL"}), ("short", {"shortUnits": "ALL"})]:
                try:
                    ep = positions.PositionClose(
                        accountID=self.account_id, instrument=pair, data=data
                    )
                    self.api.request(ep)
                    closed_something = True
                except V20Error as e:
                    if 'NO_UNITS_TO_CLOSE' in str(e) or 'closeoutPosition' in str(e):
                        closed_something = True
                    else:
                        self.logger.warning(
                            f"Unexpected error closing {direction} position for {pair}: {e}"
                        )

            if not closed_something:
                self.logger.error(f"close_position failed for {pair}: both directions errored")
                return False

            self.logger.info(f"Position closed: {pair}")
            return True

        except Exception as e:
            self.logger.error(f"Error closing position {pair}: {e}")
            return False

    def modify_trade(
        self,
        trade_id: str,
        pair: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        try:
            if stop_loss is not None:
                sl_spec = {
                    "stopLoss": {
                        "price": _fmt_price(stop_loss),
                        "timeInForce": "GTC",
                    }
                }
                ep_sl = trades.TradeCRCDO(
                    accountID=self.account_id, tradeID=trade_id, data=sl_spec
                )
                self._with_retry(lambda: self.api.request(ep_sl))

            if take_profit is not None:
                tp_spec = {
                    "takeProfit": {
                        "price": _fmt_price(take_profit),
                        "timeInForce": "GTC",
                    }
                }
                ep_tp = trades.TradeCRCDO(
                    accountID=self.account_id, tradeID=trade_id, data=tp_spec
                )
                self._with_retry(lambda: self.api.request(ep_tp))

            self.logger.info(f"Trade {trade_id} modified")
            return True

        except V20Error as e:
            self.logger.error(f"OANDA API error modifying trade {trade_id}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error modifying trade {trade_id}: {e}")
            return False

    def partial_close_trade(self, trade_id: str, units: int) -> bool:
        try:
            ep = trades.TradeClose(
                self.account_id,
                trade_id,
                data={"units": str(abs(units))},
            )
            self._with_retry(lambda: self.api.request(ep))
            self.logger.info(f"Partial close: trade {trade_id} reduced by {units} oz")
            return True
        except V20Error as e:
            self.logger.error(f"OANDA error partial-closing {trade_id}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error partial-closing {trade_id}: {e}")
            return False

    def get_historical_candles(
        self,
        pair: str = 'XAU_USD',
        granularity: str = 'H1',
        count: int = 100,
    ) -> List[Dict]:
        """Fetch historical OHLC candles for XAU_USD."""
        try:
            params = {
                'granularity': granularity,
                'count': count,
                'price': 'M',  # Mid prices
            }
            endpoint = instruments.InstrumentsCandles(instrument=pair, params=params)
            response = self._with_retry(lambda: self.api.request(endpoint))

            candles = []
            for candle in response.get('candles', []):
                if not candle.get('complete', False):
                    continue
                mid = candle.get('mid', {})
                candles.append({
                    'time': candle['time'],
                    'open': float(mid.get('o', 0)),
                    'high': float(mid.get('h', 0)),
                    'low': float(mid.get('l', 0)),
                    'close': float(mid.get('c', 0)),
                    'volume': int(candle.get('volume', 0)),
                })

            return candles

        except V20Error as e:
            self.logger.error(f"OANDA API error fetching candles for {pair}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error fetching candles for {pair}: {e}")
            return []
