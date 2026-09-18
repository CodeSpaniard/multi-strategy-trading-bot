"""Standalone tests for the reconcile_positions functions in scanner_main and
crypto_coinbase_main.

Usage:
  python -m tools.test_reconcile
"""
import os
import sys
import unittest
from dataclasses import dataclass

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("COINBASE_API_KEY", "test")
os.environ.setdefault("COINBASE_API_SECRET", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------- Scanner reconcile (Alpaca) ----------

from src import scanner_main as sm  # noqa: E402
from src.broker import BrokerError  # noqa: E402


@dataclass
class FakeAlpacaPos:
    symbol: str
    avg_entry_price: float
    current_price: float


class FakeAlpacaBrokerForRecon:
    def __init__(self, positions, fail=False):
        self._positions = positions
        self._fail = fail

    def get_all_positions(self):
        if self._fail:
            raise BrokerError("simulated")
        return self._positions


class FakeLog:
    def __init__(self):
        self.records = []
    def warning(self, m): self.records.append(("WARN", m))
    def info(self, m): self.records.append(("INFO", m))
    def error(self, m): self.records.append(("ERR", m))


class ScannerReconcileTests(unittest.TestCase):
    def test_clean_no_changes(self):
        broker = FakeAlpacaBrokerForRecon([FakeAlpacaPos("AAPL", 150.0, 155.0)])
        positions = {"AAPL": {"entry_price": 150.0, "high_water": 155.0,
                              "bounce_target_pct": 2.0, "entry_date": "2026-05-26"}}
        log = FakeLog()
        changes = sm.reconcile_positions(broker, positions, log, "scanner-test")
        self.assertEqual(changes, [])
        self.assertEqual(len(positions), 1)

    def test_adopt_broker_only(self):
        broker = FakeAlpacaBrokerForRecon([
            FakeAlpacaPos("AAPL", 150.0, 155.0),
            FakeAlpacaPos("MSFT", 300.0, 305.0),
        ])
        positions = {}
        log = FakeLog()
        changes = sm.reconcile_positions(broker, positions, log, "scanner-test")
        self.assertEqual(len(changes), 2)
        self.assertIn("AAPL", positions)
        self.assertIn("MSFT", positions)
        self.assertEqual(positions["AAPL"]["entry_price"], 150.0)
        self.assertEqual(positions["AAPL"]["high_water"], 155.0)

    def test_drop_stale_in_memory(self):
        broker = FakeAlpacaBrokerForRecon([])
        positions = {"AAPL": {"entry_price": 150.0, "high_water": 150.0,
                              "bounce_target_pct": 2.0, "entry_date": "x"}}
        log = FakeLog()
        changes = sm.reconcile_positions(broker, positions, log, "scanner-test")
        self.assertEqual(len(changes), 1)
        self.assertIn("dropped stale AAPL", changes[0])
        self.assertNotIn("AAPL", positions)

    def test_both_directions(self):
        broker = FakeAlpacaBrokerForRecon([FakeAlpacaPos("MSFT", 300.0, 305.0)])
        positions = {"AAPL": {"entry_price": 150.0, "high_water": 150.0,
                              "bounce_target_pct": 2.0, "entry_date": "x"}}
        log = FakeLog()
        changes = sm.reconcile_positions(broker, positions, log, "scanner-test")
        self.assertEqual(len(changes), 2)
        self.assertIn("MSFT", positions)
        self.assertNotIn("AAPL", positions)

    def test_broker_error_returns_empty(self):
        broker = FakeAlpacaBrokerForRecon([], fail=True)
        positions = {"AAPL": {"entry_price": 150.0, "high_water": 150.0,
                              "bounce_target_pct": 2.0, "entry_date": "x"}}
        log = FakeLog()
        changes = sm.reconcile_positions(broker, positions, log, "scanner-test")
        self.assertEqual(changes, [])
        # positions untouched on error
        self.assertIn("AAPL", positions)


# ---------- Crypto reconcile (Coinbase) ----------

from src import crypto_coinbase_main as ccm  # noqa: E402
from src.coinbase_broker import CoinbaseBrokerError  # noqa: E402


class FakeCBBrokerForRecon:
    def __init__(self, qtys: dict, prices: dict, fail_qty: bool = False):
        self.qtys = qtys
        self.prices = prices
        self.fail_qty = fail_qty

    def get_position_qty(self, symbol):
        if self.fail_qty:
            raise CoinbaseBrokerError("simulated")
        return self.qtys.get(symbol, 0.0)

    def get_current_price(self, symbol):
        return self.prices.get(symbol, 0.0)


class CryptoReconcileTests(unittest.TestCase):
    SYMS = ["BTC-USDC", "ETH-USDC"]

    def test_clean_no_changes(self):
        broker = FakeCBBrokerForRecon(
            qtys={"BTC-USDC": 0.001, "ETH-USDC": 0.01},
            prices={"BTC-USDC": 70000.0, "ETH-USDC": 2500.0},
        )
        entry_prices = {"BTC-USDC": 65000.0, "ETH-USDC": 2400.0}
        hwm = {"BTC-USDC": 70000.0, "ETH-USDC": 2500.0}
        oids = {"BTC-USDC": "btc-1", "ETH-USDC": "eth-1"}
        changes = ccm.reconcile_positions(broker, self.SYMS, entry_prices, hwm, oids, FakeLog(), "crypto-test")
        self.assertEqual(changes, [])
        # All sync state untouched on a clean reconcile
        self.assertEqual(oids, {"BTC-USDC": "btc-1", "ETH-USDC": "eth-1"})

    def test_adopt_broker_only(self):
        # Held on Coinbase, missing in-memory
        broker = FakeCBBrokerForRecon(
            qtys={"BTC-USDC": 0.001, "ETH-USDC": 0.0},
            prices={"BTC-USDC": 70000.0, "ETH-USDC": 2500.0},
        )
        entry_prices = {}
        hwm = {}
        oids = {}
        changes = ccm.reconcile_positions(broker, self.SYMS, entry_prices, hwm, oids, FakeLog(), "crypto-test")
        self.assertEqual(len(changes), 1)
        self.assertIn("BTC-USDC", entry_prices)
        self.assertEqual(entry_prices["BTC-USDC"], 70000.0)
        # Adopted symbol has no known order_id
        self.assertNotIn("BTC-USDC", oids)

    def test_adopt_clears_stale_order_id(self):
        # Stale entry_order_ids residue from a prior trade lifecycle.
        # Adoption must not silently reuse the old order_id.
        broker = FakeCBBrokerForRecon(
            qtys={"BTC-USDC": 0.001, "ETH-USDC": 0.0},
            prices={"BTC-USDC": 70000.0, "ETH-USDC": 2500.0},
        )
        entry_prices = {}
        hwm = {}
        oids = {"BTC-USDC": "stale-order-id-from-prior-trade"}
        changes = ccm.reconcile_positions(broker, self.SYMS, entry_prices, hwm, oids, FakeLog(), "crypto-test")
        self.assertEqual(len(changes), 1)
        self.assertIn("BTC-USDC", entry_prices)
        self.assertNotIn("BTC-USDC", oids,
                         "Adopt must remove stale entry_order_ids residue")

    def test_drop_stale(self):
        broker = FakeCBBrokerForRecon(
            qtys={"BTC-USDC": 0.0, "ETH-USDC": 0.0},
            prices={"BTC-USDC": 70000.0, "ETH-USDC": 2500.0},
        )
        entry_prices = {"BTC-USDC": 65000.0}
        hwm = {"BTC-USDC": 70000.0}
        oids = {"BTC-USDC": "btc-1"}
        changes = ccm.reconcile_positions(broker, self.SYMS, entry_prices, hwm, oids, FakeLog(), "crypto-test")
        self.assertEqual(len(changes), 1)
        self.assertNotIn("BTC-USDC", entry_prices)
        self.assertNotIn("BTC-USDC", oids,
                         "Drop must also clear entry_order_ids for the dropped symbol")

    def test_dust_not_adopted(self):
        # qty is tiny → position_usd < DUST_THRESHOLD_USD → don't adopt
        broker = FakeCBBrokerForRecon(
            qtys={"BTC-USDC": 0.0000001, "ETH-USDC": 0.0},
            prices={"BTC-USDC": 70000.0, "ETH-USDC": 2500.0},
        )
        entry_prices = {}
        hwm = {}
        oids = {}
        changes = ccm.reconcile_positions(broker, self.SYMS, entry_prices, hwm, oids, FakeLog(), "crypto-test")
        self.assertEqual(changes, [])
        self.assertEqual(entry_prices, {})

    def test_qty_fetch_failure_skips_symbol(self):
        broker = FakeCBBrokerForRecon(qtys={}, prices={}, fail_qty=True)
        entry_prices = {"BTC-USDC": 65000.0}
        hwm = {"BTC-USDC": 70000.0}
        oids = {"BTC-USDC": "btc-1"}
        log = FakeLog()
        changes = ccm.reconcile_positions(broker, self.SYMS, entry_prices, hwm, oids, log, "crypto-test")
        # No changes because qty fetch failed for all symbols — we don't act on unknown state
        self.assertEqual(changes, [])
        # in-memory state untouched, including the order_id
        self.assertIn("BTC-USDC", entry_prices)
        self.assertEqual(oids["BTC-USDC"], "btc-1")
        # an error was logged
        self.assertTrue(any(level == "ERR" for level, _ in log.records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
