"""
oanda_client.py — Oanda v20 REST API wrapper for XAU_USD trading.
Environment (practice/live) read from OANDA_ENVIRONMENT in .env (defaults to practice).
"""

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
import oandapyV20
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.positions as positions

load_dotenv()

INSTRUMENT = "XAU_USD"


class OandaClient:
    def __init__(self):
        self.api_key    = os.environ["OANDA_API_KEY"]
        self.account_id = os.environ["OANDA_ACCOUNT_ID"]
        env = os.getenv("OANDA_ENVIRONMENT", "practice")
        self.client = oandapyV20.API(access_token=self.api_key, environment=env)

    # ── Account ──────────────────────────────────────────────────────────────

    def get_account(self):
        r = accounts.AccountDetails(self.account_id)
        self.client.request(r)
        return r.response["account"]

    def get_balance(self):
        acct = self.get_account()
        return float(acct["balance"])

    def get_nav(self):
        acct = self.get_account()
        return float(acct["NAV"])

    # ── Market data ──────────────────────────────────────────────────────────

    def get_candles(self, granularity="H1", count=100, instrument=INSTRUMENT):
        """
        Fetch OHLCV candles.
        granularity: M1, M5, M15, M30, H1, H4, D
        Returns list of dicts with open/high/low/close/volume/time.
        """
        params = {
            "granularity": granularity,
            "count": count,
            "price": "M",  # midpoint prices
        }
        r = instruments.InstrumentsCandles(instrument, params=params)
        self.client.request(r)
        candles = []
        for c in r.response["candles"]:
            if not c["complete"]:
                continue
            candles.append({
                "time":   c["time"],
                "open":   float(c["mid"]["o"]),
                "high":   float(c["mid"]["h"]),
                "low":    float(c["mid"]["l"]),
                "close":  float(c["mid"]["c"]),
                "volume": int(c["volume"]),
            })
        return candles

    def get_current_price(self, instrument=INSTRUMENT):
        candles = self.get_candles(granularity="M1", count=2, instrument=instrument)
        if candles:
            return candles[-1]["close"]
        return None

    def get_spread(self, instrument=INSTRUMENT):
        """Fetch current bid/ask spread in price units."""
        params = {"granularity": "S5", "count": 1, "price": "BA"}
        r = instruments.InstrumentsCandles(instrument, params=params)
        try:
            self.client.request(r)
            c = r.response["candles"][-1]
            bid = float(c["bid"]["c"])
            ask = float(c["ask"]["c"])
            return round(ask - bid, 4)
        except Exception:
            return None

    # ── Orders ───────────────────────────────────────────────────────────────

    def place_market_order(self, direction, units, sl_price, tp1_price,
                           tp2_price=None, tp3_price=None, comment="agent"):
        """
        Place a market order with SL and TP1 set on Oanda.
        TP2 and TP3 are tracked externally (Oanda supports one TP per order).
        units: positive for BUY, negative for SELL.
        """
        signed_units = str(int(units)) if direction == "BUY" else str(-int(units))

        order_data = {
            "order": {
                "type": "MARKET",
                "instrument": INSTRUMENT,
                "units": signed_units,
                "timeInForce": "FOK",
                "stopLossOnFill": {
                    "price": str(round(sl_price, 2)),
                    "timeInForce": "GTC",
                },
                "takeProfitOnFill": {
                    "price": str(round(tp1_price, 2)),
                    "timeInForce": "GTC",
                },
                "clientExtensions": {
                    "comment": comment[:128],
                },
            }
        }

        r = orders.OrderCreate(self.account_id, data=order_data)
        self.client.request(r)
        resp = r.response

        trade_id = None
        if "orderFillTransaction" in resp:
            trade_id = resp["orderFillTransaction"].get("tradeOpened", {}).get("tradeID")
        elif "relatedTransactionIDs" in resp:
            trade_id = resp["relatedTransactionIDs"][0] if resp["relatedTransactionIDs"] else None

        return {
            "trade_id":   trade_id,
            "fill_price": float(resp.get("orderFillTransaction", {}).get("price", 0)) or None,
            "response":   resp,
            "tp2":        tp2_price,
            "tp3":        tp3_price,
        }

    def close_trade(self, trade_id):
        r = trades.TradeClose(self.account_id, trade_id)
        self.client.request(r)
        return r.response

    def get_open_trades(self):
        r = trades.OpenTrades(self.account_id)
        self.client.request(r)
        return r.response.get("trades", [])

    def modify_trade_sl(self, trade_id, new_sl):
        data = {"stopLoss": {"price": str(round(new_sl, 2)), "timeInForce": "GTC"}}
        r = trades.TradeCRCDO(self.account_id, trade_id, data=data)
        self.client.request(r)
        return r.response

    def modify_trade_tp(self, trade_id, new_tp):
        data = {"takeProfit": {"price": str(round(new_tp, 2)), "timeInForce": "GTC"}}
        r = trades.TradeCRCDO(self.account_id, trade_id, data=data)
        self.client.request(r)
        return r.response
