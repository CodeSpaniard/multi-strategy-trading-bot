"""Durable daily equity history (Step 1 of the leverage-maturity gate).

One append-only JSONL record per trading day per bot, capturing end-of-day equity.
This is the SOURCE OF TRUTH for long-horizon performance/risk metrics (the future
gate's 60-day equity-return Sharpe and ≥10%-drawdown-recovery check) — deliberately
SEPARATE from the sizer's short (~14-day) intraday `equity_samples`.

Design per Codex (2026-06-10): equity-based daily returns, flat days included; capture
date / bot / end-of-day equity (+ cheap metadata). Idempotent per date so a mid-day
restart that re-hits the daily hook does not double-write.
"""
import json
import os


def last_date(path) -> str | None:
    """Date string of the last record, or None if the file is missing/empty."""
    if not os.path.exists(path):
        return None
    last = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None
    try:
        return json.loads(last).get("date")
    except (ValueError, AttributeError):
        return None


def append_daily_equity(path, date_str: str, bot: str, equity: float,
                        closed_trades: int = None, open_positions: int = None,
                        realized_pnl_today: float = None) -> bool:
    """Append one end-of-day equity record for `date_str`, idempotently.

    Returns True if written, False if skipped because `date_str` is already the most
    recent record (exactly one mark per trading day, restart-safe).
    """
    if last_date(path) == date_str:
        return False
    rec = {"date": date_str, "bot": bot, "equity": round(float(equity), 2)}
    if closed_trades is not None:
        rec["closed_trades"] = int(closed_trades)
    if open_positions is not None:
        rec["open_positions"] = int(open_positions)
    if realized_pnl_today is not None:
        rec["realized_pnl_today"] = round(float(realized_pnl_today), 2)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return True
