"""Standalone tests for src.restart_tracker.

Usage:
  python -m tools.test_restart_tracker
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import restart_tracker as rt  # noqa: E402


class RestartTrackerTests(unittest.TestCase):
    def setUp(self):
        # Patch STATE_DIR into a tempdir so tests don't write into real logs/state
        self.tmp = tempfile.TemporaryDirectory()
        self._patcher = patch.object(rt, "STATE_DIR", self.tmp.name)
        self._patcher.start()
        self.notified = []
        self._notify_patcher = patch.object(
            rt, "notify_error",
            side_effect=lambda bot, msg: self.notified.append((bot, msg)),
        )
        self._notify_patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._notify_patcher.stop()
        self.tmp.cleanup()

    def test_first_start_no_alert(self):
        count = rt.track_restart_and_alert("scanner-paper", threshold=5, window_minutes=10)
        self.assertEqual(count, 1)
        self.assertEqual(self.notified, [])

    def test_below_threshold_no_alert(self):
        t0 = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(4):
            rt.track_restart_and_alert("scanner-paper", threshold=5,
                                       window_minutes=10, now=t0 + timedelta(seconds=i))
        self.assertEqual(self.notified, [])

    def test_threshold_crossing_alerts_once(self):
        t0 = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            rt.track_restart_and_alert("scanner-paper", threshold=5,
                                       window_minutes=10, now=t0 + timedelta(seconds=i))
        self.assertEqual(len(self.notified), 1)
        self.assertIn("CRASH LOOP", self.notified[0][1])
        self.assertEqual(self.notified[0][0], "scanner-paper")

    def test_no_realert_above_threshold(self):
        # Cross threshold then keep restarting — no spam.
        t0 = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            rt.track_restart_and_alert("scanner-paper", threshold=5,
                                       window_minutes=10, now=t0 + timedelta(seconds=i))
        self.assertEqual(len(self.notified), 1,
                         "Alert must not re-fire within the same crash-loop episode")

    def test_pruning_resets_episode(self):
        # 5 starts, alert fires. 11 minutes later one more start — window cleared,
        # count drops to 1, no alert. 4 more quick starts → cross threshold again,
        # alert should fire a second time (new episode).
        t0 = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            rt.track_restart_and_alert("scanner-paper", threshold=5,
                                       window_minutes=10, now=t0 + timedelta(seconds=i))
        self.assertEqual(len(self.notified), 1)

        # 11 minutes later — fresh episode
        t1 = t0 + timedelta(minutes=11)
        rt.track_restart_and_alert("scanner-paper", threshold=5,
                                   window_minutes=10, now=t1)
        # That was a single start in fresh window → count=1, no alert
        self.assertEqual(len(self.notified), 1)

        # 4 more bursts to cross threshold again
        for i in range(4):
            rt.track_restart_and_alert("scanner-paper", threshold=5,
                                       window_minutes=10, now=t1 + timedelta(seconds=i+1))
        # Now should have alerted a second time (new episode)
        self.assertEqual(len(self.notified), 2)

    def test_persists_across_calls(self):
        # First call writes file; second call reads it back.
        t0 = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
        rt.track_restart_and_alert("scanner-paper", threshold=5, window_minutes=10, now=t0)
        # Now read the file directly
        path = Path(self.tmp.name) / "scanner-paper.restarts.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(len(data), 1)

    def test_per_bot_isolation(self):
        # scanner-paper crash-looping should NOT alert for crypto-coinbase
        t0 = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            rt.track_restart_and_alert("scanner-paper", threshold=5,
                                       window_minutes=10, now=t0 + timedelta(seconds=i))
        rt.track_restart_and_alert("crypto-coinbase", threshold=5,
                                   window_minutes=10, now=t0 + timedelta(seconds=10))
        # Exactly one alert, for scanner-paper
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.notified[0][0], "scanner-paper")

    def test_corrupt_state_file_recovers(self):
        # If the state file gets corrupted, the tracker should not crash —
        # just start fresh from this call AND log a warning so the corruption
        # leaves breadcrumbs.
        path = Path(self.tmp.name) / "scanner-paper.restarts.json"
        path.write_text("not valid json{{{")
        with self.assertLogs("src.restart_tracker", level="WARNING") as captured:
            count = rt.track_restart_and_alert("scanner-paper", threshold=5, window_minutes=10)
        self.assertEqual(count, 1)
        self.assertEqual(self.notified, [])
        self.assertTrue(any("corrupt state file" in m for m in captured.output))

    def test_naive_datetime_treated_as_utc(self):
        # If a caller (or stored ISO string) lacks tzinfo, code must not crash
        # on comparisons. The tracker is documented as UTC-only.
        t_naive = datetime(2026, 5, 27, 10, 0, 0)  # no tzinfo
        count = rt.track_restart_and_alert("scanner-paper", threshold=5,
                                            window_minutes=10, now=t_naive)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
