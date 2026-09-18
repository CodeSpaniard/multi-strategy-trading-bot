"""Unit tests for src/entry_audit — log parsing and flag logic (no network)."""
import os
import tempfile
import unittest

from src import entry_audit
from src.logger import DAILY_DIR


class FakeClient:
    """Returns canned (dip_pct, close) via monkeypatched daily_dips instead."""


class TestParseBuys(unittest.TestCase):
    def _write(self, name, lines):
        os.makedirs(DAILY_DIR, exist_ok=True)
        path = f"{DAILY_DIR}/{name}"
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_parses_both_log_shapes(self):
        self._write("2099-01-02.scanner-live.log", [
            "2099-01-02 19:50:44 [INFO] [scanner-live] BUY NOW $50.00 @ ~$85.19 | target +6.9%",
            "2099-01-02 19:50:45 [INFO] [scanner-live] BUY QCOM $75.00 @ $228.67 (fill_qty=0.32) | target +2.8%",
        ])
        buys, unparsed = entry_audit.parse_buys("2099-01-02", "scanner-live")
        self.assertEqual(buys, [("NOW", 85.19), ("QCOM", 228.67)])
        self.assertEqual(unparsed, 0)

    def test_ignores_sells_errors_and_other_bots(self):
        self._write("2099-01-03.scanner-live.log", [
            "2099-01-03 14:00:00 [INFO] [scanner-live] SELL NOW @ ~$91.62",
            "2099-01-03 14:01:00 [ERROR] [scanner-live] SELL INTC FAILED: position not found",
            "2099-01-03 19:50:00 [INFO] [scanner-paper] BUY AAPL $50.00 @ ~$200.00",
            "2099-01-03 19:50:01 [INFO] [scanner-live] BUY DE $50.00 @ ~$529.72 | target +2.2%",
        ])
        buys, unparsed = entry_audit.parse_buys("2099-01-03", "scanner-live")
        self.assertEqual(buys, [("DE", 529.72)])
        self.assertEqual(unparsed, 0)

    def test_unparsable_buy_event_is_counted(self):
        # A BUY event for this bot whose fields don't match the expected shape
        # must be counted, not silently dropped (log-format-drift guard).
        self._write("2099-01-04.scanner-live.log", [
            "2099-01-04 19:50:00 [INFO] [scanner-live] BUY DE $50.00 @ ~$529.72 | target +2.2%",
            "2099-01-04 19:50:01 [INFO] [scanner-live] BUY WMT 0.41 shares for 50usd",
        ])
        buys, unparsed = entry_audit.parse_buys("2099-01-04", "scanner-live")
        self.assertEqual(buys, [("DE", 529.72)])
        self.assertEqual(unparsed, 1)

    def test_missing_log_returns_empty(self):
        self.assertEqual(entry_audit.parse_buys("1900-01-01", "scanner-live"), ([], 0))


class TestAuditFlags(unittest.TestCase):
    def _audit_with(self, buys, dips, unparsed=0):
        orig_parse = entry_audit.parse_buys
        orig_dips = entry_audit.daily_dips
        entry_audit.parse_buys = lambda d, b: (buys, unparsed)
        entry_audit.daily_dips = lambda syms, d, c: dips
        self.addCleanup(setattr, entry_audit, "parse_buys", orig_parse)
        self.addCleanup(setattr, entry_audit, "daily_dips", orig_dips)
        return entry_audit.audit("2099-01-02", "scanner-live", client=None)

    def test_clean_day(self):
        r = self._audit_with([("QCOM", 210.02)], {"QCOM": (-11.45, 210.36)})
        self.assertTrue(r["clean"])
        self.assertEqual(r["flags"], [])

    def test_boundary_dip_is_clean(self):
        # -4.68% settled close (down >=5% intraday at scan) must NOT flag.
        r = self._audit_with([("INTC", 108.93)], {"INTC": (-4.68, 109.30)})
        self.assertTrue(r["clean"])

    def test_not_a_dip_flags(self):
        r = self._audit_with([("AAPL", 200.0)], {"AAPL": (-1.2, 200.5)})
        self.assertFalse(r["clean"])
        self.assertTrue(any("NOT a dip" in f for f in r["flags"]))

    def test_moderate_slippage_is_informational(self):
        # ~+2.2% (normal scan-to-close drift on a volatile day) must NOT flag.
        r = self._audit_with([("QCOM", 215.0)], {"QCOM": (-11.45, 210.36)})
        self.assertTrue(r["clean"])
        self.assertEqual(r["flags"], [])

    def test_implausible_slippage_flags(self):
        # ~+5% indicates a genuinely bad fill, not drift -> flag.
        r = self._audit_with([("QCOM", 221.0)], {"QCOM": (-11.45, 210.36)})
        self.assertFalse(r["clean"])
        self.assertTrue(any("slippage" in f for f in r["flags"]))

    def test_missing_bar_flags(self):
        r = self._audit_with([("QCOM", 210.02)], {"QCOM": None})
        self.assertFalse(r["clean"])
        self.assertTrue(any("could not verify" in f for f in r["flags"]))

    def test_outside_universe_flags(self):
        r = self._audit_with([("ZZZZ", 10.0)], {"ZZZZ": (-9.0, 10.0)})
        self.assertFalse(r["clean"])
        self.assertTrue(any("OUTSIDE universe" in f for f in r["flags"]))

    def test_no_buys_is_clean(self):
        r = self._audit_with([], {})
        self.assertTrue(r["clean"])
        self.assertTrue(r["no_buys"])

    def test_unparsable_buys_with_no_parsed_buys_fails_loud(self):
        # The key regression Codex flagged: format drift => 0 parsed buys but
        # BUY events present. Must NOT be reported as a clean no-buy day.
        r = self._audit_with([], {}, unparsed=2)
        self.assertFalse(r["clean"])
        self.assertFalse(r["no_buys"])
        self.assertTrue(any("unparsable" in f for f in r["flags"]))

    def test_unparsable_alongside_parsed_buys_flags(self):
        r = self._audit_with([("QCOM", 210.02)], {"QCOM": (-11.45, 210.36)}, unparsed=1)
        self.assertFalse(r["clean"])
        self.assertTrue(any("unparsable" in f for f in r["flags"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
