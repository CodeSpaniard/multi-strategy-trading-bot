"""Standalone tests for src.sizer.

Usage:
  python -m tools.test_sizer

This module decides how much money each trade risks, and its drawdown-freeze
path has fired twice in production (scanner-live 2026-06-30 at dd -20.04%,
scanner-paper 2026-07-07 at dd -19.32%) while never having a single test.

The most important thing pinned here is the one that surprised us when reading
the June logs: a freeze is a GROWTH CAP, NOT A BRAKE. While frozen, the bot
keeps buying at its prior size — scanner-live kept opening $125.23 positions
through the whole week it was frozen. Whether that is the desired policy is a
separate question; these tests record what the code actually does so a change
to it is deliberate rather than accidental.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.sizer import SizerConfig, SizingDecision, compute_drawdown_pct, decide_size

NOW = datetime(2026, 9, 4, 12, 0, 0)


def cfg(**over):
    base = dict(divisor=5, training_wheel_cap_usd=50.0, training_wheel_trades=20,
                max_growth_ratio=1.5, dd_threshold_pct=10.0, dd_lookback_days=7,
                freeze_days=7, enabled=True)
    base.update(over)
    return SizerConfig(**base)


def series(*values, start_days_ago=6, step_hours=6):
    """Equity samples oldest->newest, all inside the default 7-day window."""
    t0 = NOW - timedelta(days=start_days_ago)
    return [(t0 + timedelta(hours=step_hours * i), v) for i, v in enumerate(values)]


class TestComputeDrawdown(unittest.TestCase):
    def test_too_few_samples_is_flat(self):
        self.assertEqual(compute_drawdown_pct([]), 0.0)
        self.assertEqual(compute_drawdown_pct([(NOW, 100.0)]), 0.0)

    def test_monotonic_rise_has_no_drawdown(self):
        self.assertEqual(compute_drawdown_pct(series(100, 110, 120)), 0.0)

    def test_drop_from_peak_is_negative(self):
        self.assertAlmostEqual(compute_drawdown_pct(series(100, 80)), -20.0)

    def test_measured_from_running_peak_not_from_the_start(self):
        """A dip after a new high is measured against that high."""
        self.assertAlmostEqual(compute_drawdown_pct(series(100, 200, 150)), -25.0)

    def test_worst_trough_wins_even_after_recovery(self):
        self.assertAlmostEqual(compute_drawdown_pct(series(100, 50, 100)), -50.0)

    def test_samples_are_sorted_by_time_not_taken_as_given(self):
        t = NOW
        unsorted = [(t, 100.0), (t - timedelta(hours=2), 200.0)]
        self.assertAlmostEqual(compute_drawdown_pct(unsorted), -50.0)


class TestGates(unittest.TestCase):
    def test_disabled_returns_zero(self):
        d = decide_size(cfg(enabled=False), 1000.0, 50, [], 100.0, now=NOW)
        self.assertEqual(d.per_trade_usd, 0.0)
        self.assertEqual(d.reason, "scaling_disabled_by_config")

    def test_zero_or_negative_equity_returns_zero(self):
        for equity in (0.0, -5.0):
            d = decide_size(cfg(), equity, 50, [], 100.0, now=NOW)
            self.assertEqual(d.per_trade_usd, 0.0)
            self.assertIn("nothing to size against", d.reason)

    def test_disabled_wins_over_everything_else(self):
        """The config kill switch is checked before drawdown, so a disabled
        sizer cannot accidentally arm a freeze."""
        d = decide_size(cfg(enabled=False), 1000.0, 50, series(1000, 500), 100.0, now=NOW)
        self.assertEqual(d.per_trade_usd, 0.0)
        self.assertIsNone(d.new_frozen_until)


class TestFreeze(unittest.TestCase):
    def test_freeze_holds_size_it_does_not_reduce_or_block(self):
        """The surprising one. Frozen means "stop growing", not "stop trading":
        the bot keeps buying at prior_size. Recorded deliberately."""
        d = decide_size(cfg(), 1000.0, 50, [], prior_size=125.23,
                        frozen_until=NOW + timedelta(days=3), now=NOW)
        self.assertEqual(d.per_trade_usd, 125.23)
        self.assertIn("frozen_until=", d.reason)
        self.assertIsNone(d.new_frozen_until, "an in-force freeze must not re-arm itself")

    def test_freeze_with_no_prior_size_falls_back_to_proportional(self):
        d = decide_size(cfg(divisor=5), 1000.0, 50, [], prior_size=0.0,
                        frozen_until=NOW + timedelta(days=3), now=NOW)
        self.assertEqual(d.per_trade_usd, 200.0)

    def test_expired_freeze_no_longer_applies(self):
        """scanner-live still carries a 2026-07-07 frozen_until. It must not
        keep suppressing growth two months later."""
        d = decide_size(cfg(), 1000.0, 50, series(1000, 1010), prior_size=125.23,
                        frozen_until=NOW - timedelta(days=59), now=NOW)
        self.assertNotIn("frozen_until=", d.reason)
        self.assertEqual(d.per_trade_usd, 125.23 * 1.5, "growth cap applies once thawed")

    def test_breach_arms_a_freeze_and_reports_it(self):
        d = decide_size(cfg(), 1000.0, 50, series(1000, 800), prior_size=125.23, now=NOW)
        self.assertIsNotNone(d.new_frozen_until)
        self.assertEqual(datetime.fromisoformat(d.new_frozen_until), NOW + timedelta(days=7))
        self.assertEqual(d.per_trade_usd, 125.23, "breach holds size, it does not cut it")
        self.assertIn("DD_breach", d.reason)

    def test_reproduces_the_2026_06_30_production_freeze(self):
        """dd -20.04% against a -10% threshold, freezing 7 days, size held at
        $125.23 — the scanner-live event."""
        d = decide_size(cfg(), 652.0, 58, series(1000.0, 799.6), prior_size=125.23, now=NOW)
        self.assertIn("threshold=-10.0%", d.reason)
        self.assertAlmostEqual(float(d.reason.split("dd=")[1].split("%")[0]), -20.04, places=2)
        self.assertEqual(d.per_trade_usd, 125.23)

    def test_drawdown_just_inside_threshold_does_not_freeze(self):
        d = decide_size(cfg(dd_threshold_pct=10.0), 1000.0, 50, series(1000, 900),
                        prior_size=100.0, now=NOW)
        self.assertIsNone(d.new_frozen_until, "-10.0% is not worse than -10.0%")

    def test_old_drawdown_outside_the_lookback_is_ignored(self):
        """A crash last month must not freeze sizing today."""
        old = [(NOW - timedelta(days=30), 1000.0), (NOW - timedelta(days=29), 500.0)]
        recent = [(NOW - timedelta(days=1), 1000.0), (NOW, 1005.0)]
        d = decide_size(cfg(dd_lookback_days=7), 1000.0, 50, old + recent,
                        prior_size=100.0, now=NOW)
        self.assertIsNone(d.new_frozen_until)


class TestCaps(unittest.TestCase):
    def test_base_size_is_equity_over_divisor(self):
        d = decide_size(cfg(divisor=5, training_wheel_trades=0), 1000.0, 50, [], 0.0, now=NOW)
        self.assertEqual(d.per_trade_usd, 200.0)

    def test_training_wheels_cap_applies_below_the_trade_count(self):
        d = decide_size(cfg(divisor=5, training_wheel_cap_usd=50.0, training_wheel_trades=20),
                        1000.0, 19, [], 0.0, now=NOW)
        self.assertEqual(d.per_trade_usd, 50.0)
        self.assertIn("tw_cap=$50(19/20trades)", d.reason)

    def test_training_wheels_lift_exactly_at_the_trade_count(self):
        d = decide_size(cfg(divisor=5, training_wheel_cap_usd=50.0, training_wheel_trades=20),
                        1000.0, 20, [], 0.0, now=NOW)
        self.assertEqual(d.per_trade_usd, 200.0)
        self.assertNotIn("tw_cap", d.reason)

    def test_growth_cap_limits_one_step(self):
        """One hot trade cannot double the next position."""
        d = decide_size(cfg(divisor=1, training_wheel_trades=0, max_growth_ratio=1.5),
                        10000.0, 50, [], prior_size=100.0, now=NOW)
        self.assertEqual(d.per_trade_usd, 150.0)

    def test_the_tightest_cap_wins(self):
        d = decide_size(cfg(divisor=1, training_wheel_cap_usd=50.0, training_wheel_trades=20,
                            max_growth_ratio=1.5), 10000.0, 5, [], prior_size=100.0, now=NOW)
        self.assertEqual(d.per_trade_usd, 50.0, "training wheels are tighter than the growth cap")


class TestConfig(unittest.TestCase):
    def test_from_dict_requires_the_two_load_bearing_keys(self):
        for missing in ("divisor", "training_wheel_cap_usd"):
            d = {"divisor": 5, "training_wheel_cap_usd": 50.0}
            d.pop(missing)
            with self.assertRaises(KeyError):
                SizerConfig.from_dict(d)

    def test_from_dict_defaults_match_the_documented_policy(self):
        c = SizerConfig.from_dict({"divisor": 5, "training_wheel_cap_usd": 50.0})
        self.assertEqual((c.training_wheel_trades, c.max_growth_ratio), (20, 1.5))
        self.assertEqual((c.dd_threshold_pct, c.dd_lookback_days, c.freeze_days), (10.0, 7, 7))
        self.assertTrue(c.enabled)

    def test_live_configs_parse(self):
        """The shipped YAML must actually construct a SizerConfig."""
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("config.scanner.live.yaml", "config.crypto.coinbase.yaml"):
            with open(os.path.join(root, "configs", name)) as fh:
                sizing = yaml.safe_load(fh)["sizing"]
            c = SizerConfig.from_dict(sizing)
            self.assertGreater(c.divisor, 0, name)
            self.assertGreater(c.dd_threshold_pct, 0, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
