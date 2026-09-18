"""Unit tests for the daily equity-history persistence (leverage-gate Step 1).

Covers: fields written, idempotency per date (restart-safe), new-date append, last_date.
Run: pytest tools/test_equity_history.py
"""
import json

from src.equity_history import append_daily_equity, last_date


def _lines(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_writes_record_with_fields(tmp_path):
    p = tmp_path / "scanner-live.equity_daily.jsonl"
    wrote = append_daily_equity(str(p), "2026-06-10", "scanner-live", 626.0,
                                closed_trades=34, open_positions=2, realized_pnl_today=12.34)
    assert wrote is True
    rows = _lines(p)
    assert len(rows) == 1
    r = rows[0]
    assert r == {"date": "2026-06-10", "bot": "scanner-live", "equity": 626.0,
                 "closed_trades": 34, "open_positions": 2, "realized_pnl_today": 12.34}


def test_idempotent_same_day(tmp_path):
    p = tmp_path / "e.jsonl"
    assert append_daily_equity(str(p), "2026-06-10", "scanner-live", 626.0) is True
    # a mid-day restart re-hits the daily hook → must NOT double-write
    assert append_daily_equity(str(p), "2026-06-10", "scanner-live", 999.0) is False
    rows = _lines(p)
    assert len(rows) == 1 and rows[0]["equity"] == 626.0  # original kept, no overwrite


def test_new_day_appends(tmp_path):
    p = tmp_path / "e.jsonl"
    append_daily_equity(str(p), "2026-06-10", "scanner-live", 626.0)
    assert append_daily_equity(str(p), "2026-06-11", "scanner-live", 631.0) is True
    rows = _lines(p)
    assert len(rows) == 2
    assert [r["date"] for r in rows] == ["2026-06-10", "2026-06-11"]


def test_last_date_and_missing_file(tmp_path):
    p = tmp_path / "e.jsonl"
    assert last_date(str(p)) is None  # missing file
    append_daily_equity(str(p), "2026-06-10", "scanner-live", 626.0)
    assert last_date(str(p)) == "2026-06-10"


def test_optional_fields_omitted(tmp_path):
    p = tmp_path / "e.jsonl"
    append_daily_equity(str(p), "2026-06-10", "scanner-live", 626.0)
    assert _lines(p)[0] == {"date": "2026-06-10", "bot": "scanner-live", "equity": 626.0}
