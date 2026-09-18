"""Standalone tests for src.atomic_io.

Usage:
  python -m tools.test_atomic_io
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.atomic_io import write_json_atomic


class Unserializable:
    pass


def _big_doc():
    """Roughly the size of the real crypto state file, so the write spans pages."""
    return {"equity_samples": [[f"2026-09-02T{h:02d}:00:00.000000", 400.0 + h] for h in range(24)] * 40}


def _hammer(path, writer, seconds=1.5):
    """Run `writer` in a loop while a separate thread reads. Returns (torn, reads)."""
    doc = _big_doc()
    writer(path, doc)
    stop = threading.Event()
    counts = {"torn": 0, "reads": 0}

    def read_loop():
        while not stop.is_set():
            counts["reads"] += 1
            try:
                json.loads(Path(path).read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                counts["torn"] += 1

    t = threading.Thread(target=read_loop)
    t.start()
    try:
        end = time.time() + seconds
        n = 0
        while time.time() < end:
            doc["n"] = n
            n += 1
            writer(path, doc)
    finally:
        stop.set()
        t.join()
    return counts["torn"], counts["reads"]


class TestWriteJsonAtomic(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "state.json"

    def test_writes_document(self):
        write_json_atomic(self.path, {"closed_trades": 94, "frozen_until": None})
        self.assertEqual(json.loads(self.path.read_text())["closed_trades"], 94)

    def test_leaves_no_temp_file_behind(self):
        write_json_atomic(self.path, {"a": 1})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])

    def test_replaces_existing_document(self):
        write_json_atomic(self.path, {"v": 1})
        write_json_atomic(self.path, {"v": 2})
        self.assertEqual(json.loads(self.path.read_text())["v"], 2)

    def test_failed_serialization_leaves_previous_document_intact(self):
        """The whole point: a write that dies partway must not destroy what was
        already on disk, the way truncate-then-write does."""
        write_json_atomic(self.path, {"v": "good"})
        with self.assertRaises(TypeError):
            write_json_atomic(self.path, {"v": Unserializable()})
        self.assertEqual(json.loads(self.path.read_text())["v"], "good")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])

    def test_sequential_rewrite_always_parses(self):
        """Baseline: repeated whole-document rewrites leave a parseable file.
        This is NOT a concurrency test — see test_concurrent_reader_never_tears.
        """
        big = _big_doc()
        for i in range(60):
            big["n"] = i
            write_json_atomic(self.path, big)
            json.loads(self.path.read_text())

    def test_concurrent_reader_never_tears(self):
        """The race this module exists to fix: a reader running in a separate
        thread while a writer rewrites the file must never observe a partial
        document. Against Path.write_text the same harness reliably produces
        torn reads (measured ~2.4%), which is what test_control_write_text_does_tear
        asserts, so a zero here is meaningful rather than vacuous.
        """
        torn, reads = _hammer(self.path, write_json_atomic)
        self.assertGreater(reads, 200, "reader did not get enough samples to be meaningful")
        self.assertEqual(torn, 0, f"atomic writer produced {torn} torn reads out of {reads}")

    def test_control_write_text_does_tear(self):
        """Control for the test above. If this ever stops tearing, the concurrency
        harness has gone blind and the zero from the atomic test proves nothing.
        """
        torn, reads = _hammer(self.path, lambda p, o: Path(p).write_text(json.dumps(o)))
        self.assertGreater(torn, 0,
                           f"harness observed no torn reads in {reads} samples — "
                           "it is no longer exercising the race")

    def _assert_intact_and_clean(self):
        self.assertEqual(json.loads(self.path.read_text())["v"], "good")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])

    def test_fsync_failure_leaves_previous_document_intact(self):
        write_json_atomic(self.path, {"v": "good"})
        with mock.patch("src.atomic_io.os.fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                write_json_atomic(self.path, {"v": "new"})
        self._assert_intact_and_clean()

    def test_replace_failure_leaves_previous_document_intact(self):
        write_json_atomic(self.path, {"v": "good"})
        with mock.patch("src.atomic_io.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                write_json_atomic(self.path, {"v": "new"})
        self._assert_intact_and_clean()

    def test_write_failure_after_bytes_land_leaves_previous_document_intact(self):
        """Distinct from a serialization failure: bytes have already reached the
        temp file when the failure occurs."""
        write_json_atomic(self.path, {"v": "good"})
        real_dump = json.dump

        def dump_then_die(obj, fh, **kw):
            fh.write('{"v": "par')  # partial document on disk
            raise OSError("simulated write failure mid-document")

        with mock.patch("src.atomic_io.json.dump", side_effect=dump_then_die):
            with self.assertRaises(OSError):
                write_json_atomic(self.path, {"v": "new"})
        self._assert_intact_and_clean()

    def test_interrupt_cleans_up_temp_and_propagates(self):
        """BaseException path: KeyboardInterrupt during a shutdown save must not
        leave litter, and must not be swallowed."""
        write_json_atomic(self.path, {"v": "good"})
        with mock.patch("src.atomic_io.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                write_json_atomic(self.path, {"v": "new"})
        self._assert_intact_and_clean()

    def test_dumps_kwargs_forwarded(self):
        from datetime import datetime
        write_json_atomic(self.path, {"t": datetime(2026, 9, 2)}, default=str)
        self.assertIn("2026-09-02", json.loads(self.path.read_text())["t"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
