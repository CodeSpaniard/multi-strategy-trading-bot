"""Standalone tests for src.daily_loss — the daily realized-loss entry brake.

Usage:
  python -m tools.test_daily_loss_brake

The failure this whole mechanism is designed against is the one the legacy
RiskManager had: a "daily" limit that is really per-process, so restarting the
bot silently clears it. test_restart_does_not_clear_a_reached_limit is the
test that matters most here.
"""
import ast
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.daily_loss import (daily_block, entries_blocked, resolve_limit,
                            restore_daily, utc_today_iso)

LIMIT = 15.0


class TestBoundary(unittest.TestCase):
    def test_just_inside_the_limit_allows_entries(self):
        self.assertFalse(entries_blocked(-14.99, LIMIT))

    def test_exactly_at_the_limit_blocks(self):
        """`<=`, not `<`: $15 is the maximum permitted loss, so reaching it
        exactly has reached it."""
        self.assertTrue(entries_blocked(-15.00, LIMIT))

    def test_beyond_the_limit_blocks(self):
        self.assertTrue(entries_blocked(-15.01, LIMIT))

    def test_profit_never_blocks(self):
        self.assertFalse(entries_blocked(+40.0, LIMIT))

    def test_flat_day_never_blocks(self):
        self.assertFalse(entries_blocked(0.0, LIMIT))

    def test_limit_sign_is_not_load_bearing(self):
        """A limit configured as -15 must not invert the policy."""
        self.assertTrue(entries_blocked(-16.0, -15.0))


class TestDisabled(unittest.TestCase):
    """Code capability is not risk-policy activation — the config activates it."""

    def test_zero_limit_disables(self):
        self.assertFalse(entries_blocked(-1000.0, 0))

    def test_absent_config_disables_via_the_resolver(self):
        """An absent key resolves to 0.0 (deliberately off). Note this must go
        through resolve_limit: passing a raw None straight to entries_blocked now
        means "unresolved" and blocks, which is why the gate is never allowed to
        see a raw config value."""
        limit, err = resolve_limit(None)
        self.assertEqual(limit, 0.0)
        self.assertIsNone(err)
        self.assertFalse(entries_blocked(-1000.0, limit))

    def test_paper_config_value_disables(self):
        """scanner-paper ships 0 deliberately; $15 is calibrated to the live
        account and would trip constantly on the paper account's far larger equity."""
        self.assertFalse(entries_blocked(-500.0, 0))


class TestLimitResolution(unittest.TestCase):
    """A configuration mistake must not silently disable the risk control, and
    must not silently be treated as if it were intended."""

    def test_absent_is_disabled_without_complaint(self):
        self.assertEqual(resolve_limit(None), (0.0, None))

    def test_zero_is_disabled_without_complaint(self):
        """scanner-paper ships 0 deliberately — that is a policy choice, not an
        error, so it must not raise an alert every startup."""
        self.assertEqual(resolve_limit(0), (0.0, None))

    def test_positive_is_enabled_quietly(self):
        self.assertEqual(resolve_limit(15), (15.0, None))

    def test_negative_stays_active_at_magnitude_and_reports(self):
        limit, err = resolve_limit(-15)
        self.assertEqual(limit, 15.0, "a wrong sign must not switch off the brake")
        self.assertIsNotNone(err, "a wrong sign must not be applied silently")
        self.assertIn("negative", err)
        self.assertIn("ACTIVE", err, "the alert must say the brake is still on")

    def test_negative_limit_still_blocks_entries(self):
        """End to end: -15 configured, -16 realized, entries refused."""
        limit, err = resolve_limit(-15)
        self.assertTrue(entries_blocked(-16.0, limit))
        self.assertTrue(err)

    def test_non_numeric_is_unresolved_not_disabled(self):
        """The distinction that matters: `0` means the operator chose no brake,
        None means we could not establish the constraint at all."""
        limit, err = resolve_limit("abc")
        self.assertIsNone(limit, "a malformed limit must not collapse into 'disabled'")
        self.assertIn("UNRESOLVED", err)
        self.assertIn("blocked", err, "the alert must say entries stop")

    def test_unresolved_blocks_entries_even_on_a_profitable_day(self):
        """If the constraint cannot be established we cannot know we are inside
        it, so entries stop regardless of today's P&L."""
        limit, _ = resolve_limit("abc")
        self.assertTrue(entries_blocked(-16.0, limit))
        self.assertTrue(entries_blocked(0.0, limit))
        self.assertTrue(entries_blocked(+50.0, limit))

    def test_disabled_and_unresolved_are_not_the_same_state(self):
        self.assertFalse(entries_blocked(-999.0, 0.0))   # deliberately off
        self.assertTrue(entries_blocked(+999.0, None))   # cannot be determined

    def test_string_number_is_accepted(self):
        self.assertEqual(resolve_limit("20"), (20.0, None))


class TestFullPipeline(unittest.TestCase):
    """Raw config value -> resolve_limit -> entries_blocked, end to end."""

    CASES = [
        # raw,     resolved, blocks at -16, expects a diagnostic
        (None,     0.0,      False, False),
        (0,        0.0,      False, False),
        (15,       15.0,     True,  False),
        (-15,      15.0,     True,  True),
        ("15",     15.0,     True,  False),
        ("abc",    None,     True,  True),
    ]

    def test_matrix(self):
        for raw, expected_limit, expected_block, expects_error in self.CASES:
            with self.subTest(raw=raw):
                limit, err = resolve_limit(raw)
                self.assertEqual(limit, expected_limit)
                self.assertEqual(entries_blocked(-16.0, limit), expected_block)
                self.assertEqual(bool(err), expects_error)

    def test_notification_formats_every_resolved_state(self):
        """None reaching a `:.2f` format spec would raise inside the trading
        loop, at exactly the moment the brake is trying to fire."""
        import src.notifier as notifier
        captured = []
        original, notifier.notify = notifier.notify, lambda **kw: captured.append(kw)
        try:
            for raw, *_ in self.CASES:
                limit, _ = resolve_limit(raw)
                notifier.notify_daily_brake("scanner-live", -16.0, limit)
        finally:
            notifier.notify = original
        self.assertEqual(len(captured), len(self.CASES))
        self.assertTrue(any("UNRESOLVED" in c["message"] for c in captured))


class TestNetAccounting(unittest.TestCase):
    def test_winner_offsets_later_losers(self):
        """+20, -10, -6, -5 -> net -1 -> still allowed."""
        net = sum([+20.0, -10.0, -6.0, -5.0])
        self.assertAlmostEqual(net, -1.0)
        self.assertFalse(entries_blocked(net, LIMIT))

    def test_losses_without_an_offsetting_winner_block(self):
        """-10, -6 -> net -16 -> blocked."""
        net = sum([-10.0, -6.0])
        self.assertAlmostEqual(net, -16.0)
        self.assertTrue(entries_blocked(net, LIMIT))


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.today = utc_today_iso()
        self.yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()

    def test_restores_todays_book(self):
        state = {"daily": daily_block(self.today, -16.42, 3, 1)}
        self.assertEqual(restore_daily(state, self.today), (-16.42, 3, 1))

    def test_restart_does_not_clear_a_reached_limit(self):
        """THE test. Lose $16, restart, and entries must still be refused."""
        persisted = {"daily": daily_block(self.today, -16.0, 2, 0)}
        pnl, _, _ = restore_daily(persisted, self.today)          # process restarts
        self.assertTrue(entries_blocked(pnl, LIMIT),
                        "a restart must never hand back a fresh loss budget")

    def test_yesterdays_book_is_not_restored(self):
        state = {"daily": daily_block(self.yesterday, -16.0, 2, 0)}
        self.assertEqual(restore_daily(state, self.today), (0.0, 0, 0))

    def test_new_day_after_a_blocked_day_allows_entries_again(self):
        state = {"daily": daily_block(self.yesterday, -99.0, 5, 0)}
        pnl, _, _ = restore_daily(state, self.today)
        self.assertFalse(entries_blocked(pnl, LIMIT))

    def test_state_without_a_daily_block_starts_fresh(self):
        """Backward compatibility: state files written before this existed."""
        legacy = {"positions": {}, "closed_trades": 97, "today": self.today}
        self.assertEqual(restore_daily(legacy, self.today), (0.0, 0, 0))

    def test_corrupt_daily_block_starts_fresh_rather_than_raising(self):
        for bad in ({"date": None}, {"date": "x"}, {"realized_pnl": "abc"}):
            bad = dict(bad); bad.setdefault("date", self.today)
            self.assertEqual(restore_daily({"daily": bad}, self.today), (0.0, 0, 0))

    def test_block_carries_its_date(self):
        b = daily_block(self.today, -16.4239, 3, 1)
        self.assertEqual(b["date"], self.today)
        self.assertEqual(b["realized_pnl"], -16.4239)
        self.assertEqual((b["closed_trades"], b["wins"]), (3, 1))


class TestUtcBoundary(unittest.TestCase):
    """The day boundary must be UTC regardless of host timezone — the invariant
    the legacy code got right only by accident."""

    def setUp(self):
        self._orig = os.environ.get("TZ")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig
        time.tzset()

    def test_same_utc_date_under_any_host_timezone(self):
        results = set()
        for tz in ("UTC", "America/New_York", "Asia/Tokyo", "Pacific/Kiritimati"):
            os.environ["TZ"] = tz
            time.tzset()
            results.add(utc_today_iso())
        self.assertEqual(len(results), 1, f"host timezone changed the day boundary: {results}")

    def test_explicit_instant_maps_to_its_utc_date(self):
        """22:00 US/Eastern on the 4th is already the 5th in UTC."""
        instant = datetime(2026, 9, 5, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(utc_today_iso(instant), "2026-09-05")


class TestWiring(unittest.TestCase):
    """Structural guards, not behaviour. `snapshot_state` is a closure inside
    main() and cannot be called from a test, so mutation-testing found that
    deleting persistence from it left the suite green. These assert the wiring
    directly, so that mutation fails."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.src = open(os.path.join(root, "src", "scanner_main.py")).read()
        cls.tree = ast.parse(cls.src)

    def _func(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"{name} not found in scanner_main")

    def test_snapshot_state_persists_the_daily_block(self):
        """Drop this and a restart silently hands back a fresh loss budget."""
        keys = [k.value for node in ast.walk(self._func("snapshot_state"))
                if isinstance(node, ast.Dict)
                for k in node.keys if isinstance(k, ast.Constant)]
        self.assertIn("daily", keys,
                      "snapshot_state must persist the daily block in the same atomic write "
                      "as the trading state")

    def test_the_gate_exists_exactly_once(self):
        calls = [n for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "entries_blocked"]
        self.assertEqual(len(calls), 1, "the brake must be applied at exactly one site")

    def test_there_is_still_only_one_entry_site(self):
        """If a second buy path appears, the single gate no longer covers entries."""
        buys = [n for n in ast.walk(self.tree)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "buy_notional"]
        self.assertEqual(len(buys), 1, "a new entry site would bypass the brake")

    def test_raw_config_never_reaches_the_gate(self):
        """None is load-bearing in entries_blocked, so the gate must only ever
        see a value produced by resolve_limit."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "entries_blocked":
                arg = node.args[1]
                self.assertIsInstance(arg, ast.Name,
                                      "the limit argument must be the resolved variable")
                self.assertEqual(arg.id, "daily_loss_limit_usd")
        self.assertNotIn('entries_blocked(realized_pnl_today, cfg[', self.src)

    def test_the_config_error_is_reported_not_swallowed(self):
        """resolve_limit's diagnostic must reach the operator, not just a log."""
        self.assertIn("resolve_limit(", self.src)
        self.assertIn("notify_error(log_name, limit_cfg_error)", self.src,
                      "an invalid risk-limit config must raise a Pushover alert")

    def test_the_gate_is_not_in_the_exit_path(self):
        """The brake blocks BUYs only; SELLs and stop management must be untouched."""
        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.If) and any(
                    isinstance(c, ast.Call) and getattr(c.func, "id", None) == "entries_blocked"
                    for c in ast.walk(node.test))):
                continue
            guarded = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            self.assertNotIn("close_position", guarded)
            self.assertNotIn("realized_pnl_usd", guarded)
            return
        self.fail("no if-statement guarded by entries_blocked found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
