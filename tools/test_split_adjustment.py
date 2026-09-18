"""Regression test: the dip scanner must not be fooled by stock splits.

A forward split (e.g. 10:1) makes RAW (unadjusted) day-over-day price look like a
~-90% crash. The scanner buys dips, so on raw bars it would buy a phantom "dip" that
is really just a split. `broker.daily_bars` now requests split/dividend-ADJUSTED bars
(adjustment=Adjustment.ALL), which removes the artifact at the source.

This locks the invariant Codex asked us to prove:
  - raw split bars -> scanner registers a (phantom) dip   [the bug it would hit]
  - adjusted bars  -> scanner registers nothing           [why the fix works]
  - a genuine dip  -> scanner still fires                  [no false negative]

Pure unit test: a fake broker supplies synthetic 2-bar series; no network/creds.
Run: pytest tools/test_split_adjustment.py
"""
import pandas as pd

import src.scanner as scanner
from src.scanner import scan_for_dips


class FakeBroker:
    """Returns a fixed 2-bar close series for any symbol (mimics broker.daily_bars)."""

    def __init__(self, prev_close, today_close):
        self._prev, self._today = prev_close, today_close

    def daily_bars(self, symbol, days):
        return pd.DataFrame({"close": [self._prev, self._today]})


def _scan(monkeypatch, prev_close, today_close, threshold=5.0):
    monkeypatch.setattr(scanner, "UNIVERSE", ["TEST"])  # one synthetic symbol
    return scan_for_dips(FakeBroker(prev_close, today_close), dip_threshold_pct=threshold)


def test_raw_split_creates_phantom_dip(monkeypatch):
    # 10:1 split in RAW bars: 100.0 -> 10.0 reads as -90%. This is the bug:
    # if unadjusted bars reach the scanner, it would buy a split as if it were a crash.
    cands = _scan(monkeypatch, 100.0, 10.0)
    assert len(cands) == 1, "raw split bars should (wrongly) register as a dip"
    assert cands[0].dip_pct > 80, f"phantom dip should be ~90%, got {cands[0].dip_pct:.1f}"


def test_adjusted_split_is_not_a_dip(monkeypatch):
    # ADJUSTED bars back-adjust the pre-split close, so a 10:1 split shows no real
    # move (10.0 -> 10.0). adjustment=Adjustment.ALL in broker.daily_bars guarantees this.
    cands = _scan(monkeypatch, 10.0, 10.0)
    assert cands == [], "split-adjusted bars must not register as a dip"


def test_genuine_dip_still_detected(monkeypatch):
    # The fix must not suppress real dips: a genuine -6% selloff still fires.
    cands = _scan(monkeypatch, 100.0, 94.0)
    assert len(cands) == 1
    assert 5.9 < cands[0].dip_pct < 6.1, f"expected ~6% dip, got {cands[0].dip_pct:.2f}"
