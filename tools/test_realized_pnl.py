"""Standalone tests for src.fill.realized_pnl_usd.

Usage:
  python -m tools.test_realized_pnl

Both live bots previously booked realized P&L as `last_size_usd * pnl_pct`,
scaling by the CURRENT sizing decision rather than by what was actually invested
in the position being closed. These tests pin the corrected behaviour and
demonstrate the size of the error the old form produced.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fill import realized_pnl_usd


def old_way(entry_price, exit_price, last_size_usd):
    """The previous calculation, kept only so the tests can show the divergence."""
    pnl_pct = (exit_price - entry_price) / entry_price * 100
    return last_size_usd * pnl_pct / 100 if last_size_usd > 0 else 0.0


class TestRealizedPnl(unittest.TestCase):
    def test_loss_is_negative_and_exact(self):
        self.assertAlmostEqual(realized_pnl_usd(100.0, 90.0, 1.5), -15.0)

    def test_gain_is_positive_and_exact(self):
        self.assertAlmostEqual(realized_pnl_usd(100.0, 110.0, 1.5), 15.0)

    def test_flat_close_is_zero(self):
        self.assertEqual(realized_pnl_usd(100.0, 100.0, 1.5), 0.0)

    def test_zero_quantity_is_zero_not_a_phantom_pnl(self):
        self.assertEqual(realized_pnl_usd(100.0, 90.0, 0.0), 0.0)

    def test_fractional_shares(self):
        """Alpaca fills fractional quantities; the real ISRG close was 0.347190801."""
        self.assertAlmostEqual(realized_pnl_usd(375.73, 365.31, 0.347190801), -3.6178, places=3)

    def test_matches_the_real_isrg_close(self):
        """2026-09-04: ISRG closed on a trailing stop, entry 375.73, exit 365.31,
        qty 0.347190801. Here the old form happened to be close, because
        last_size_usd ($130.46) was near the position's actual notional
        ($130.44) — it had barely moved since entry."""
        correct = realized_pnl_usd(375.73, 365.31, 0.347190801)
        approx = old_way(375.73, 365.31, 130.464)
        self.assertLess(abs(correct - approx), 0.01,
                        "for this trade the two agree — the defect is not always visible")

    def test_divergence_when_size_changed_since_entry(self):
        """The case that matters. A position opened under the $50 training-wheel
        cap and closed after sizing rose to $130 books 2.6x the true loss."""
        entry_notional, qty = 50.0, 50.0 / 100.0   # $50 at $100/share
        correct = realized_pnl_usd(100.0, 90.0, qty)
        approx = old_way(100.0, 90.0, 130.0)       # last_size_usd has since grown
        self.assertAlmostEqual(correct, -5.0)
        self.assertAlmostEqual(approx, -13.0)
        self.assertGreater(abs(approx / correct), 2.5,
                           "the old form overstated this loss by more than 2.5x")

    def test_error_is_unbounded_in_the_size_ratio(self):
        """The old form's error is exactly (last_size_usd / entry_notional - 1),
        so it grows without limit as sizing drifts from the position's cost."""
        entry_price, exit_price, qty = 100.0, 95.0, 1.0
        correct = realized_pnl_usd(entry_price, exit_price, qty)
        for multiple in (2, 5, 10):
            approx = old_way(entry_price, exit_price, entry_price * qty * multiple)
            self.assertAlmostEqual(approx / correct, multiple)


if __name__ == "__main__":
    unittest.main(verbosity=2)
