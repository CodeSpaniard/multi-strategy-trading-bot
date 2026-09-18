"""Standalone tests for tools.status_json.

Usage:
  python -m tools.test_status_json

This module shipped two production bugs in its first three days — a phantom
position for a symbol sold two hours earlier, and permanent false staleness on
any bot holding nothing. Both came from treating position log lines as
authority they do not carry. These tests pin the semantics that replaced them:

  systemd active  -> the process exists
  HEARTBEAT       -> the poll loop is executing        (the liveness signal)
  state `held`    -> what is held right now            (authoritative)
  position lines  -> detail/history for a symbol       (neither of the above)

and the two fail-closed rules: no heartbeat is not healthy, and unreadable
state shows nothing rather than stale history.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import status_json as S

NOW = datetime(2026, 9, 4, 17, 0, 0)


def at(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def hold_line(seconds_ago, bot, sym, price="108.50", status="HOLD"):
    return (f"{at(seconds_ago)},123 [INFO] [{bot}] {sym}: {status} | "
            f"pnl -3.62%, hwm 113.76, dd -4.62%, target +3.68% | px={price}")


def heartbeat(seconds_ago, bot):
    return f"{at(seconds_ago)},123 [INFO] [{bot}] HEARTBEAT"


class World:
    """A throwaway repo layout: configs/, logs/state/, logs/manual/."""

    def __init__(self, name="scanner-live", poll_key="monitor_poll_seconds", poll=60):
        self.root = Path(tempfile.mkdtemp())
        self.name = name
        (self.root / "configs").mkdir()
        (self.root / "logs" / "state").mkdir(parents=True)
        (self.root / "logs" / "manual").mkdir(parents=True)
        (self.root / "configs" / "c.yaml").write_text(
            f"loop:\n  {poll_key}: {poll}\nsizing:\n  enabled: true\n")
        self.bot = {"name": name, "unit": "u.service", "config": "configs/c.yaml",
                    "env": ".env", "poll_key": poll_key, "kind": "alpaca", "paper": False}

    def log(self, lines):
        (self.root / "logs" / "manual" / f"{self.name}.log").write_text("\n".join(lines) + "\n")

    def state(self, obj_or_text):
        p = self.root / "logs" / "state" / f"{self.name}.state.json"
        p.write_text(obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text))

    def tier1(self):
        warnings = []
        with mock.patch.object(S, "ROOT", str(self.root)), \
             mock.patch.object(S, "STATE_DIR", str(self.root / "logs" / "state")), \
             mock.patch.object(S, "LOG_DIR", str(self.root / "logs" / "manual")), \
             mock.patch.object(S, "unit_active", return_value="active"):
            out = S.tier1(self.bot, NOW, warnings)
        return out, warnings


class TestParsing(unittest.TestCase):
    def test_newest_line_per_symbol_wins(self):
        w = World(); w.log([hold_line(300, "scanner-live", "CSCO", "100.00"),
                            hold_line(30, "scanner-live", "CSCO", "109.75")])
        pos = S.latest_positions(str(w.root / "logs/manual/scanner-live.log"), NOW, "scanner-live")
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["price"], 109.75)
        self.assertEqual(pos[0]["age_seconds"], 30.0)

    def test_other_bots_lines_are_ignored(self):
        w = World(); w.log([hold_line(30, "scanner-paper", "CSCO"),
                            hold_line(30, "scanner-live", "WMT")])
        pos = S.latest_positions(str(w.root / "logs/manual/scanner-live.log"), NOW, "scanner-live")
        self.assertEqual([p["symbol"] for p in pos], ["WMT"])

    def test_non_position_lines_never_become_positions(self):
        """The false-positive class flagged in review: nothing else in the log
        may satisfy LINE_RE."""
        w = World(); w.log([
            heartbeat(10, "scanner-live"),
            f"{at(20)},123 [INFO] [scanner-live] SIZER: $125.23 | DD_breach dd=-20.04% threshold=-10.0%",
            f"{at(30)},123 [WARNING] [scanner-live] FREEZE triggered. Sizing locked until 2026-07-07.",
            f"{at(40)},123 [INFO] [scanner-live] SELL ISRG @ $365.31 (fill_qty=0.347190801)",
            f"{at(50)},123 [INFO] [scanner-live] Market closed. Sleeping 60s.",
            f"{at(60)},123 [INFO] [scanner-live] Restored 3 positions, 94 closed trades",
        ])
        pos = S.latest_positions(str(w.root / "logs/manual/scanner-live.log"), NOW, "scanner-live")
        self.assertEqual(pos, [], f"parsed a phantom position: {pos}")

    def test_heartbeat_age_and_bot_scoping(self):
        w = World(); w.log([heartbeat(300, "scanner-live"),
                            heartbeat(5, "scanner-paper"),
                            heartbeat(45, "scanner-live")])
        p = str(w.root / "logs/manual/scanner-live.log")
        self.assertEqual(S.latest_heartbeat_age(p, NOW, "scanner-live"), 45.0)
        self.assertIsNone(S.latest_heartbeat_age(p, NOW, "crypto-coinbase"))

    def test_tail_drops_partial_first_line(self):
        w = World()
        p = w.root / "logs/manual/scanner-live.log"
        p.write_text("x" * 70000 + "\ncomplete line\n")
        lines = S.tail_bytes(str(p))
        self.assertNotIn("x" * 10, "".join(lines))
        self.assertIn("complete line", lines)


class TestNormalizeHeld(unittest.TestCase):
    def test_scanner_schema(self):
        held = S.normalize_held({"positions": {"CSCO": {"entry_price": 112.5, "high_water": 113.7,
                                                        "entry_date": "d", "bounce_target_pct": 3.6}}})
        self.assertEqual(held["CSCO"]["entry_price"], 112.5)

    def test_crypto_schema(self):
        held = S.normalize_held({"entry_prices": {"BTC-USDC": 77729.01},
                                 "high_water_marks": {"BTC-USDC": 80681.6}})
        self.assertEqual(held["BTC-USDC"]["high_water"], 80681.6)
        self.assertIsNone(held["BTC-USDC"]["entry_date"])

    def test_empty_state_is_flat_not_an_error(self):
        self.assertEqual(S.normalize_held({}), {})


class TestReadJson(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_good(self):
        p = self.d / "s.json"; p.write_text('{"a": 1}')
        self.assertEqual(S.read_json(str(p)), ({"a": 1}, None))

    def test_missing_reports_error_not_empty_dict(self):
        data, err = S.read_json(str(self.d / "nope.json"))
        self.assertIsNone(data); self.assertIn("not found", err)

    def test_torn_reports_error_and_never_a_partial_document(self):
        p = self.d / "s.json"; p.write_text('{"a": 1, "b": [1,2')
        data, err = S.read_json(str(p), retries=0)
        self.assertIsNone(data); self.assertIn("unreadable", err)


class TestLivenessAndPositions(unittest.TestCase):
    """The two production bugs and the two fail-closed rules."""

    def test_flat_bot_is_live_with_no_positions(self):
        """Bug 2: a bot holding nothing logs no position lines. That is healthy."""
        w = World()
        w.state({"positions": {}, "closed_trades": 97})
        w.log([hold_line(7300, "scanner-live", "ISRG", status="SELL"), heartbeat(20, "scanner-live")])
        out, warnings = w.tier1()
        self.assertEqual(out["liveness"], "live")
        self.assertFalse(out["stale"])
        self.assertEqual(warnings, [])

    def test_sold_symbol_is_not_reported_as_held(self):
        """Bug 1: ISRG sold two hours ago must not appear as a position."""
        w = World()
        w.state({"positions": {}, "closed_trades": 97})
        w.log([hold_line(7300, "scanner-live", "ISRG", status="SELL"), heartbeat(20, "scanner-live")])
        out, _ = w.tier1()
        self.assertEqual(out["positions"], [])
        self.assertEqual(out["held"], {})
        self.assertEqual(out["newest_position_line_age_seconds"], 7300.0,
                         "the raw line age should still be reported for diagnostics")

    def test_held_symbol_is_reported(self):
        w = World()
        w.state({"positions": {"CSCO": {"entry_price": 112.5, "high_water": 113.7}}})
        w.log([hold_line(30, "scanner-live", "CSCO"), hold_line(30, "scanner-live", "WMT"),
               heartbeat(20, "scanner-live")])
        out, _ = w.tier1()
        self.assertEqual([p["symbol"] for p in out["positions"]], ["CSCO"],
                         "WMT is in the log but not held — it must be filtered out")

    def test_missing_heartbeat_is_unknown_and_not_healthy(self):
        """Fail closed: absence of evidence of liveness must not read as healthy."""
        w = World()
        w.state({"positions": {}})
        w.log([hold_line(60, "scanner-live", "CSCO")])  # no heartbeat at all
        out, warnings = w.tier1()
        self.assertEqual(out["liveness"], "unknown")
        self.assertTrue(out["stale"], "missing heartbeat must not report stale=False")
        self.assertIsNone(out["heartbeat_age_seconds"])
        self.assertTrue(any("liveness UNKNOWN" in x for x in warnings))

    def test_old_heartbeat_is_stale(self):
        w = World()
        w.state({"positions": {}})
        w.log([heartbeat(400, "scanner-live")])  # > 60 * 3
        out, warnings = w.tier1()
        self.assertEqual(out["liveness"], "stale")
        self.assertTrue(out["stale"])
        self.assertTrue(any("last HEARTBEAT" in x for x in warnings))

    def test_staleness_is_relative_to_each_bots_cadence(self):
        """25 minutes is healthy for the hourly crypto bot and broken for the
        60s scanner. Same age, opposite verdicts."""
        age = 1500
        fast = World("scanner-live", "monitor_poll_seconds", 60)
        fast.state({"positions": {}}); fast.log([heartbeat(age, "scanner-live")])
        slow = World("crypto-coinbase", "poll_seconds", 3600)
        slow.state({"entry_prices": {}}); slow.log([heartbeat(age, "crypto-coinbase")])
        self.assertEqual(fast.tier1()[0]["liveness"], "stale")
        self.assertEqual(slow.tier1()[0]["liveness"], "live")

    def test_unreadable_state_suppresses_positions(self):
        """Fail closed: the condition that removes the authoritative filter is
        exactly when stale log lines mislead most."""
        w = World()
        w.state('{"positions": {"CSCO": {"entry_pri')  # torn
        w.log([hold_line(30, "scanner-live", "CSCO"), heartbeat(20, "scanner-live")])
        out, warnings = w.tier1()
        self.assertFalse(out["state_available"])
        self.assertEqual(out["positions"], [], "must not fall back to unfiltered log lines")
        self.assertIsNone(out["closed_trades"], "must be None, not a fabricated 0")
        self.assertTrue(any("positions suppressed" in x for x in warnings))


class TestFreeze(unittest.TestCase):
    def test_expired_freeze_is_not_active(self):
        w = World()
        w.state({"positions": {}, "frozen_until": "2026-07-07T19:50:35"})
        w.log([heartbeat(20, "scanner-live")])
        out, warnings = w.tier1()
        self.assertEqual(out["frozen_until"], "2026-07-07T19:50:35")
        self.assertFalse(out["frozen_active"], "a months-old freeze must not read as in force")
        self.assertFalse(any("freeze IN FORCE" in x for x in warnings))

    def test_future_freeze_is_active_and_warns(self):
        w = World()
        w.state({"positions": {}, "frozen_until": (NOW + timedelta(days=3)).isoformat()})
        w.log([heartbeat(20, "scanner-live")])
        out, warnings = w.tier1()
        self.assertTrue(out["frozen_active"])
        self.assertTrue(any("freeze IN FORCE" in x for x in warnings))


class TestHostClock(unittest.TestCase):
    """The age arithmetic is naive-local and only correct because the droplet
    runs UTC. This host does not, which is precisely why the warning exists —
    so drive the real code path rather than assuming the runner's timezone."""

    def setUp(self):
        self._orig = os.environ.get("TZ")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig
        time.tzset()

    def _set_tz(self, tz):
        os.environ["TZ"] = tz
        time.tzset()

    def test_utc_host_produces_no_warning(self):
        self._set_tz("UTC")
        self.assertIsNone(S.host_clock_warning())

    def test_non_utc_host_warns(self):
        self._set_tz("America/New_York")
        warning = S.host_clock_warning()
        self.assertIsNotNone(warning, "a non-UTC host must not pass silently")
        self.assertIn("not UTC", warning)


if __name__ == "__main__":
    unittest.main(verbosity=2)
