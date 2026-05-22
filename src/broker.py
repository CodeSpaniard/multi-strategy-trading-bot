import logging
import os
import time
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

log = logging.getLogger(__name__)


def _retry(fn, retries: int = 2, delay_s: float = 2.0, label: str = ""):
    """Call fn() up to retries+1 times. Small backoff between tries.
    Intended for read-only, idempotent calls (clock, positions, bars).
    NEVER wrap order-submission calls — double-fire risk."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < retries:
                log.warning(f"{label} attempt {attempt+1} failed: {e}; retrying in {delay_s}s")
                time.sleep(delay_s)
    raise last_exc


class BrokerError(Exception):
    """Broker operation failed. Wraps underlying API exception with context."""


class Broker:
    def __init__(self, paper: bool = True, asset_class: str = "equity", request_timeout_s: float = 10.0):
        if asset_class not in ("equity", "crypto"):
            raise ValueError(f"asset_class must be 'equity' or 'crypto', got {asset_class!r}")
        api_key = os.environ["ALPACA_API_KEY"]
        secret = os.environ["ALPACA_SECRET_KEY"]
        self.paper = paper
        self.asset_class = asset_class
        self.trading = TradingClient(api_key, secret, paper=paper)
        if asset_class == "crypto":
            self.data = CryptoHistoricalDataClient()
        else:
            self.data = StockHistoricalDataClient(api_key, secret)
        for client in (self.trading, self.data):
            session = getattr(client, "_session", None) or getattr(client, "session", None)
            if session is not None:
                try:
                    session.request = _with_timeout(session.request, request_timeout_s)
                except Exception:
                    pass

    def account_equity(self) -> float:
        try:
            return float(self.trading.get_account().equity)
        except Exception as e:
            raise BrokerError(f"account_equity failed: {e}") from e

    def _trading_symbol(self, symbol: str) -> str:
        """Alpaca trading API uses BTCUSD; data API uses BTC/USD."""
        if self.asset_class == "crypto":
            return symbol.replace("/", "")
        return symbol

    def get_position_qty(self, symbol: str) -> float:
        tsym = self._trading_symbol(symbol)
        try:
            pos = self.trading.get_open_position(tsym)
            return float(pos.qty)
        except Exception as e:
            msg = str(e).lower()
            if "position does not exist" in msg or "404" in msg or "not found" in msg:
                return 0.0
            raise BrokerError(f"get_position_qty({symbol}) failed: {e}") from e

    def get_all_positions(self):
        try:
            return self.trading.get_all_positions()
        except Exception as e:
            raise BrokerError(f"get_all_positions failed: {e}") from e

    def recent_bars(self, symbol: str, minutes: int):
        if self.asset_class == "crypto":
            end = datetime.utcnow()
            start = end - timedelta(minutes=minutes + 30)
            req = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=start, end=end,
            )
            try:
                bars = self.data.get_crypto_bars(req).df
            except Exception as e:
                raise BrokerError(f"recent_bars({symbol}) failed: {e}") from e
        else:
            end = datetime.utcnow() - timedelta(minutes=16)
            start = end - timedelta(minutes=minutes + 30)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=start, end=end, feed=DataFeed.IEX,
            )
            try:
                bars = self.data.get_stock_bars(req).df
            except Exception as e:
                raise BrokerError(f"recent_bars({symbol}) failed: {e}") from e
        if bars.empty:
            return None
        if "symbol" in bars.index.names:
            bars = bars.xs(symbol, level="symbol")
        return bars.tail(minutes)

    def daily_bars(self, symbol: str, days: int):
        # 16-minute offset to stay outside Alpaca's 15-min SIP-delay window on free tier.
        end = datetime.utcnow() - timedelta(minutes=16)
        start = end - timedelta(days=days + 10)
        if self.asset_class == "crypto":
            req = CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                start=start, end=end,
            )
            try:
                bars = self.data.get_crypto_bars(req).df
            except Exception as e:
                raise BrokerError(f"daily_bars({symbol}) failed: {e}") from e
        else:
            # Use IEX (free) feed explicitly — SIP requires paid subscription.
            req = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                start=start, end=end, feed=DataFeed.IEX,
            )
            try:
                bars = self.data.get_stock_bars(req).df
            except Exception as e:
                raise BrokerError(f"daily_bars({symbol}) failed: {e}") from e
        if bars.empty:
            return None
        if "symbol" in bars.index.names:
            bars = bars.xs(symbol, level="symbol")
        return bars.tail(days)

    def buy_notional(self, symbol: str, usd: float):
        tsym = self._trading_symbol(symbol)
        tif = TimeInForce.GTC if self.asset_class == "crypto" else TimeInForce.DAY
        order = MarketOrderRequest(
            symbol=tsym,
            notional=round(usd, 2),
            side=OrderSide.BUY,
            time_in_force=tif,
        )
        try:
            return self.trading.submit_order(order)
        except Exception as e:
            raise BrokerError(f"buy_notional({symbol}, ${usd}) failed: {e}") from e

    def close_position(self, symbol: str):
        tsym = self._trading_symbol(symbol)
        try:
            return self.trading.close_position(tsym)
        except Exception as e:
            raise BrokerError(f"close_position({symbol}) failed: {e}") from e

    def flatten_all(self):
        try:
            self.trading.close_all_positions(cancel_orders=True)
        except Exception as e:
            raise BrokerError(f"flatten_all failed: {e}") from e

    def market_is_open(self) -> bool:
        if self.asset_class == "crypto":
            return True  # crypto trades 24/7
        try:
            return _retry(lambda: self.trading.get_clock().is_open, label="market_is_open")
        except Exception as e:
            raise BrokerError(f"market_is_open failed: {e}") from e

    def get_clock(self):
        """Return raw clock object. Retries on transient failure."""
        try:
            return _retry(lambda: self.trading.get_clock(), label="get_clock")
        except Exception as e:
            raise BrokerError(f"get_clock failed: {e}") from e


def _with_timeout(original_request, timeout_s: float):
    def wrapped(*args, **kwargs):
        kwargs.setdefault("timeout", timeout_s)
        return original_request(*args, **kwargs)
    return wrapped
