"""Standalone tests for Broker._wait_for_fill and CoinbaseBroker._wait_for_fill.

Usage:
  python -m tools.test_fill_wait

Exits 0 on pass, 1 on failure. Uses fake order objects + monkey-patched
trading clients so it runs offline with no API credentials.
"""
import os
import sys
import time
import types
import unittest
from dataclasses import dataclass
from typing import List

# Stub out env so module-level imports that read keys (e.g. alpaca init paths) don't blow up.
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("COINBASE_API_KEY", "test")
os.environ.setdefault("COINBASE_API_SECRET", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------- Alpaca tests ----------

from alpaca.trading.enums import OrderStatus  # noqa: E402

from src import broker as broker_mod  # noqa: E402


@dataclass
class FakeAlpacaOrder:
    id: str
    status: object
    filled_avg_price: float = 0.0
    filled_qty: float = 0.0


class FakeAlpacaTrading:
    """Returns a scripted sequence of order states on successive get_order_by_id calls."""

    def __init__(self, states: List[FakeAlpacaOrder]):
        self.states = states
        self.idx = 0

    def get_order_by_id(self, order_id):
        i = min(self.idx, len(self.states) - 1)
        self.idx += 1
        return self.states[i]


def _make_alpaca_broker(states: List[FakeAlpacaOrder], fill_timeout_s: float = 2.0):
    """Build a Broker without actually constructing its real Alpaca clients."""
    b = broker_mod.Broker.__new__(broker_mod.Broker)
    b.paper = True
    b.asset_class = "equity"
    b.fill_timeout_s = fill_timeout_s
    b.trading = FakeAlpacaTrading(states)
    return b


class AlpacaWaitForFillTests(unittest.TestCase):
    def test_immediate_fill(self):
        first = FakeAlpacaOrder(id="a1", status=OrderStatus.FILLED,
                                filled_avg_price=101.25, filled_qty=3.0)
        b = _make_alpaca_broker([first])
        fill = b._wait_for_fill(first, "BUY", "AAPL")
        self.assertTrue(fill.fully_filled)
        self.assertEqual(fill.fill_price, 101.25)
        self.assertEqual(fill.filled_qty, 3.0)
        self.assertEqual(fill.order_id, "a1")

    def test_pending_then_filled(self):
        pending = FakeAlpacaOrder(id="a2", status=OrderStatus.NEW)
        filled = FakeAlpacaOrder(id="a2", status=OrderStatus.FILLED,
                                 filled_avg_price=50.0, filled_qty=10.0)
        b = _make_alpaca_broker([pending, filled])
        # Initial submit returns pending; poll then sees filled.
        fill = b._wait_for_fill(pending, "BUY", "MSFT")
        self.assertTrue(fill.fully_filled)
        self.assertEqual(fill.fill_price, 50.0)

    def test_rejected_raises(self):
        rej = FakeAlpacaOrder(id="a3", status=OrderStatus.REJECTED)
        b = _make_alpaca_broker([rej])
        with self.assertRaises(broker_mod.BrokerError):
            b._wait_for_fill(rej, "BUY", "FOO")

    def test_partial_at_timeout(self):
        partial = FakeAlpacaOrder(id="a4", status=OrderStatus.PARTIALLY_FILLED,
                                  filled_avg_price=20.0, filled_qty=2.5)
        # Same state returned forever — will hit timeout
        b = _make_alpaca_broker([partial], fill_timeout_s=0.5)
        start = time.monotonic()
        fill = b._wait_for_fill(partial, "BUY", "BAR")
        elapsed = time.monotonic() - start
        self.assertFalse(fill.fully_filled)
        self.assertEqual(fill.filled_qty, 2.5)
        self.assertEqual(fill.fill_price, 20.0)
        self.assertGreaterEqual(elapsed, 0.5)

    def test_pending_at_timeout_raises(self):
        pending = FakeAlpacaOrder(id="a5", status=OrderStatus.NEW)
        b = _make_alpaca_broker([pending], fill_timeout_s=0.3)
        with self.assertRaises(broker_mod.BrokerError):
            b._wait_for_fill(pending, "BUY", "BAZ")

    def test_extra_terminal_statuses_raise_fast(self):
        # Each of these should raise BrokerError immediately rather than
        # waiting through the timeout. Use a long timeout to detect bugs.
        for st in (OrderStatus.DONE_FOR_DAY, OrderStatus.REPLACED,
                   OrderStatus.STOPPED, OrderStatus.SUSPENDED,
                   OrderStatus.CALCULATED):
            with self.subTest(status=st):
                o = FakeAlpacaOrder(id=f"a-{st}", status=st)
                b = _make_alpaca_broker([o], fill_timeout_s=30.0)
                start = time.monotonic()
                with self.assertRaises(broker_mod.BrokerError):
                    b._wait_for_fill(o, "BUY", "X")
                self.assertLess(time.monotonic() - start, 1.0,
                                f"{st} should be terminal, not waited out")


# ---------- Coinbase tests ----------

from src import coinbase_broker as cb_mod  # noqa: E402


class FakeCoinbaseClient:
    """Returns scripted dicts on successive get_order calls."""
    def __init__(self, orders: List[dict]):
        self.orders = orders
        self.idx = 0

    def get_order(self, order_id):
        i = min(self.idx, len(self.orders) - 1)
        self.idx += 1
        return {"order": self.orders[i]}


def _make_cb_broker(orders: List[dict], fill_timeout_s: float = 2.0):
    b = cb_mod.CoinbaseBroker.__new__(cb_mod.CoinbaseBroker)
    b.fill_timeout_s = fill_timeout_s
    b.client = FakeCoinbaseClient(orders)
    b._product_cache = {}
    return b


class CoinbaseWaitForFillTests(unittest.TestCase):
    def _envelope(self, order_id="c1"):
        # Mimics market_order_buy response envelope (success path).
        return {"success_response": {"order_id": order_id}}

    def test_immediate_fill(self):
        b = _make_cb_broker([{"status": "FILLED", "filled_size": "0.5", "average_filled_price": "30000.00"}])
        fill = b._wait_for_fill(self._envelope("c1"), "BUY", "BTC-USDC")
        self.assertTrue(fill.fully_filled)
        self.assertEqual(fill.fill_price, 30000.0)
        self.assertEqual(fill.filled_qty, 0.5)
        self.assertEqual(fill.order_id, "c1")

    def test_pending_then_filled(self):
        b = _make_cb_broker([
            {"status": "OPEN", "filled_size": "0", "average_filled_price": "0"},
            {"status": "FILLED", "filled_size": "1.0", "average_filled_price": "2500.0"},
        ])
        fill = b._wait_for_fill(self._envelope("c2"), "BUY", "ETH-USDC")
        self.assertTrue(fill.fully_filled)
        self.assertEqual(fill.fill_price, 2500.0)

    def test_cancelled_raises(self):
        b = _make_cb_broker([{"status": "CANCELLED", "filled_size": "0", "average_filled_price": "0"}])
        with self.assertRaises(cb_mod.CoinbaseBrokerError):
            b._wait_for_fill(self._envelope("c3"), "BUY", "ETH-USDC")

    def test_partial_at_timeout(self):
        b = _make_cb_broker(
            [{"status": "OPEN", "filled_size": "0.3", "average_filled_price": "100.0"}],
            fill_timeout_s=0.5,
        )
        fill = b._wait_for_fill(self._envelope("c4"), "BUY", "BTC-USDC")
        self.assertFalse(fill.fully_filled)
        self.assertEqual(fill.filled_qty, 0.3)
        self.assertEqual(fill.fill_price, 100.0)

    def test_no_envelope_raises(self):
        b = _make_cb_broker([])
        with self.assertRaises(cb_mod.CoinbaseBrokerError):
            b._wait_for_fill({"not_a_real_envelope": True}, "BUY", "BTC-USDC")


if __name__ == "__main__":
    unittest.main(verbosity=2)
