"""Coinbase Advanced Trade broker adapter.
Same interface pattern as Broker class — TrendStrategy doesn't care which broker it talks to.
"""
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from coinbase.rest import RESTClient

from src.fill import FillResult

log = logging.getLogger(__name__)

DEFAULT_BASE_INCREMENT = "0.00000001"
DEFAULT_QUOTE_INCREMENT = "0.01"

# Coinbase order status terminal-fail values (per Advanced Trade API)
_TERMINAL_FAIL_STATUSES = ("CANCELLED", "EXPIRED", "FAILED")


class CoinbaseBrokerError(Exception):
    """Coinbase operation failed."""


class CoinbaseBroker:
    def __init__(self, fill_timeout_s: float = 10.0):
        api_key = os.environ["COINBASE_API_KEY"]
        api_secret = os.environ["COINBASE_API_SECRET"]
        self.client = RESTClient(api_key=api_key, api_secret=api_secret)
        self.fill_timeout_s = fill_timeout_s
        self._product_cache: dict = {}

    def _product_increments(self, product_id: str) -> tuple:
        cached = self._product_cache.get(product_id)
        if cached:
            return cached
        try:
            p = self.client.get_product(product_id)
            base_inc = getattr(p, "base_increment", None) or DEFAULT_BASE_INCREMENT
            quote_inc = getattr(p, "quote_increment", None) or DEFAULT_QUOTE_INCREMENT
        except Exception as e:
            log.warning(f"product_increments({product_id}) fetch failed, using defaults: {e}")
            base_inc, quote_inc = DEFAULT_BASE_INCREMENT, DEFAULT_QUOTE_INCREMENT
        self._product_cache[product_id] = (base_inc, quote_inc)
        return base_inc, quote_inc

    @staticmethod
    def _truncate_to_increment(value: float, increment_str: str) -> str:
        inc = Decimal(increment_str)
        d = Decimal(str(value))
        return str(d.quantize(inc, rounding=ROUND_DOWN))

    @staticmethod
    def _check_order_success(order, action: str, product_id: str):
        if getattr(order, "success", True):
            return
        err = getattr(order, "error_response", None) or getattr(order, "failure_reason", "<no detail>")
        raise CoinbaseBrokerError(f"{action} {product_id} rejected by Coinbase: {err}")

    @staticmethod
    def _extract_order_id(order) -> str:
        """Pull order_id from the success_response envelope, duck-typing dict or attr forms."""
        succ = getattr(order, "success_response", None)
        if succ is None and isinstance(order, dict):
            succ = order.get("success_response")
        if succ is None:
            return ""
        if isinstance(succ, dict):
            return str(succ.get("order_id", ""))
        return str(getattr(succ, "order_id", "") or "")

    @staticmethod
    def _field(obj, name, default=None):
        """Read `name` from obj whether obj is a dict or has attrs."""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    def _wait_for_fill(self, order, action: str, product_id: str) -> FillResult:
        """Poll order status until terminal or fill_timeout_s elapses.

        Terminal outcomes:
          FILLED                                  → FillResult(fully_filled=True)
          CANCELLED / EXPIRED / FAILED            → CoinbaseBrokerError
          Partial fill present at timeout         → FillResult(fully_filled=False) + WARN
          Still pending with no fill at timeout   → CoinbaseBrokerError("fill timeout")
        """
        order_id = self._extract_order_id(order)
        if not order_id:
            raise CoinbaseBrokerError(f"{action} {product_id}: no order_id in success_response; cannot verify fill")

        poll_interval_s = 1.0
        deadline = time.monotonic() + self.fill_timeout_s

        status = None
        filled_size = 0.0
        avg_price = 0.0
        while True:
            try:
                resp = self.client.get_order(order_id=order_id)
            except Exception as e:
                raise CoinbaseBrokerError(f"{action} {product_id} poll get_order failed: {e}") from e
            o = self._field(resp, "order", resp)
            status = self._field(o, "status")
            filled_size = float(self._field(o, "filled_size", 0) or 0)
            avg_price = float(self._field(o, "average_filled_price", 0) or 0)

            if status == "FILLED":
                return FillResult(fill_price=avg_price, filled_qty=filled_size,
                                  fully_filled=True, order_id=order_id)
            if status in _TERMINAL_FAIL_STATUSES:
                raise CoinbaseBrokerError(
                    f"{action} {product_id} ended in status={status} (order_id={order_id})"
                )
            if time.monotonic() >= deadline:
                if filled_size > 0:
                    log.warning(
                        f"{action} {product_id} PARTIAL FILL at timeout: "
                        f"filled_size={filled_size} px={avg_price} status={status} (order_id={order_id})"
                    )
                    return FillResult(fill_price=avg_price, filled_qty=filled_size,
                                      fully_filled=False, order_id=order_id)
                raise CoinbaseBrokerError(
                    f"{action} {product_id} fill timeout after {self.fill_timeout_s}s "
                    f"(status={status}, order_id={order_id})"
                )
            time.sleep(poll_interval_s)

    def account_equity(self) -> float:
        """Get total portfolio value in USD."""
        try:
            accounts = self.client.get_accounts()
            total = 0.0
            for acct in accounts.accounts:
                bal = acct.available_balance if hasattr(acct, 'available_balance') else acct['available_balance']
                value = float(bal['value'] if isinstance(bal, dict) else bal.value)
                currency = bal['currency'] if isinstance(bal, dict) else bal.currency
                if currency in ("USD", "USDC", "USDT"):
                    total += value  # stablecoins treated as $1
                elif value > 0:
                    try:
                        price = self.get_current_price(f"{currency}/USD")
                        total += value * price
                    except Exception:
                        pass
            return total
        except CoinbaseBrokerError:
            raise
        except Exception as e:
            raise CoinbaseBrokerError(f"account_equity failed: {e}") from e

    def get_position_qty(self, symbol: str) -> float:
        """Get quantity held. symbol: 'BTC/USD' or 'BTC-USD'."""
        currency = self._to_currency(symbol)
        try:
            accounts = self.client.get_accounts()
            for acct in accounts.accounts:
                bal = acct.available_balance if hasattr(acct, 'available_balance') else acct['available_balance']
                cur = bal['currency'] if isinstance(bal, dict) else bal.currency
                val = float(bal['value'] if isinstance(bal, dict) else bal.value)
                if cur == currency:
                    return val
            return 0.0
        except Exception as e:
            raise CoinbaseBrokerError(f"get_position_qty({symbol}) failed: {e}") from e

    def get_current_price(self, symbol: str) -> float:
        product_id = self._to_product_id(symbol)
        try:
            ticker = self.client.get_product(product_id)
            price_str = ticker['price'] if isinstance(ticker, dict) else ticker.price
            return float(price_str)
        except Exception as e:
            raise CoinbaseBrokerError(f"get_current_price({symbol}) failed: {e}") from e

    def daily_bars(self, symbol: str, days: int):
        """Fetch daily OHLC bars. Returns pandas DataFrame or None."""
        import pandas as pd
        product_id = self._to_product_id(symbol)
        end = datetime.utcnow()
        start = end - timedelta(days=days + 5)
        try:
            candles = self.client.get_candles(
                product_id=product_id,
                start=str(int(start.timestamp())),
                end=str(int(end.timestamp())),
                granularity="ONE_DAY",
            )
            candle_list = candles.candles if hasattr(candles, 'candles') else candles['candles']
            if not candle_list:
                return None
            rows = []
            for c in candle_list:
                if isinstance(c, dict):
                    rows.append({
                        "timestamp": datetime.utcfromtimestamp(int(c['start'])),
                        "open": float(c['open']), "high": float(c['high']),
                        "low": float(c['low']), "close": float(c['close']),
                        "volume": float(c['volume']),
                    })
                else:
                    rows.append({
                        "timestamp": datetime.utcfromtimestamp(int(c.start)),
                        "open": float(c.open), "high": float(c.high),
                        "low": float(c.low), "close": float(c.close),
                        "volume": float(c.volume),
                    })
            df = pd.DataFrame(rows).set_index("timestamp").sort_index()
            return df.tail(days)
        except Exception as e:
            raise CoinbaseBrokerError(f"daily_bars({symbol}) failed: {e}") from e

    def buy_notional(self, symbol: str, usd: float) -> FillResult:
        product_id = self._to_product_id(symbol)
        _, quote_inc = self._product_increments(product_id)
        quote_size = self._truncate_to_increment(usd, quote_inc)
        try:
            order = self.client.market_order_buy(
                client_order_id=str(uuid.uuid4()),
                product_id=product_id,
                quote_size=quote_size,
            )
        except Exception as e:
            raise CoinbaseBrokerError(f"buy_notional({symbol}, ${usd}) failed: {e}") from e
        self._check_order_success(order, "BUY", product_id)
        log.info(f"Coinbase BUY order accepted: {product_id} quote_size={quote_size}")
        return self._wait_for_fill(order, "BUY", product_id)

    def sell_all(self, symbol: str) -> FillResult | None:
        qty = self.get_position_qty(symbol)
        if qty <= 0:
            log.warning(f"sell_all({symbol}): no position to sell")
            return None
        product_id = self._to_product_id(symbol)
        base_inc, _ = self._product_increments(product_id)
        base_size = self._truncate_to_increment(qty, base_inc)
        if Decimal(base_size) <= 0:
            raise CoinbaseBrokerError(
                f"sell_all({symbol}): qty {qty} truncates to 0 at increment {base_inc}"
            )
        try:
            order = self.client.market_order_sell(
                client_order_id=str(uuid.uuid4()),
                product_id=product_id,
                base_size=base_size,
            )
        except Exception as e:
            raise CoinbaseBrokerError(f"sell_all({symbol}) failed: {e}") from e
        self._check_order_success(order, "SELL", product_id)
        log.info(f"Coinbase SELL order accepted: {product_id} base_size={base_size} (raw qty {qty})")
        return self._wait_for_fill(order, "SELL", product_id)

    def market_is_open(self) -> bool:
        return True

    def _to_product_id(self, symbol: str) -> str:
        return symbol.replace("/", "-")

    def _to_currency(self, symbol: str) -> str:
        return symbol.replace("-", "/").split("/")[0]
