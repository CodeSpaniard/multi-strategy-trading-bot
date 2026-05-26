"""Daily summary writer.

Each bot appends one line per day to logs/summary.log with its closing state.
Scanner writes at end-of-day (after final scan). Crypto writes once per day
on the first poll after UTC midnight.

Format keeps columns aligned so `grep` and eye-scanning both work well.
"""
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.emailer import send_email
from src.logger import SUMMARY_LOG

SUMMARY_FILE = Path(SUMMARY_LOG)


def _should_write_today(bot: str, today: date) -> bool:
    """Return True if no line for (bot, today) is already in the summary file."""
    if not SUMMARY_FILE.exists():
        return True
    needle = f"{today.isoformat()} {bot}"
    try:
        with SUMMARY_FILE.open("r") as f:
            for line in f:
                if line.startswith(needle):
                    return False
    except Exception:
        return True
    return True


def write_scanner_summary(bot: str, equity: float, positions: dict,
                          closed_trades_today: int, wins_today: int,
                          scan_count: Optional[int], dips_found: Optional[int],
                          realized_pnl_today: float) -> None:
    today = date.today()
    if not _should_write_today(bot, today):
        return
    SUMMARY_FILE.parent.mkdir(exist_ok=True)
    pos_str = ",".join(f"{s}(${p.get('entry_price', 0):.2f})" for s, p in positions.items()) or "none"
    scan_str = f"scan={scan_count}syms/{dips_found}dips" if scan_count is not None else "scan=—"
    losses = max(0, closed_trades_today - wins_today)
    line = (
        f"{today.isoformat()} {bot:<15} "
        f"equity=${equity:>10,.2f}  "
        f"pnl_today={realized_pnl_today:+7.2f}  "
        f"closed={closed_trades_today}({wins_today}W/{losses}L)  "
        f"{scan_str}  "
        f"positions={pos_str}\n"
    )
    with SUMMARY_FILE.open("a") as f:
        f.write(line)

    send_email(
        subject=f"[trading-bot] {bot} daily summary — {today.isoformat()} — eq ${equity:,.2f}",
        body=line,
    )


def write_crypto_summary(bot: str, equity: float,
                         positions: dict, ma_states: dict,
                         closed_trades_today: int, realized_pnl_today: float) -> None:
    today = date.today()
    if not _should_write_today(bot, today):
        return
    SUMMARY_FILE.parent.mkdir(exist_ok=True)
    pos_str = ",".join(f"{s}@${e:.2f}" for s, e in positions.items()) or "none"
    ma_str = ",".join(f"{s}:{state}" for s, state in ma_states.items()) or "—"
    line = (
        f"{today.isoformat()} {bot:<15} "
        f"equity=${equity:>10,.2f}  "
        f"pnl_today={realized_pnl_today:+7.2f}  "
        f"closed={closed_trades_today}  "
        f"MA={ma_str}  "
        f"positions={pos_str}\n"
    )
    with SUMMARY_FILE.open("a") as f:
        f.write(line)

    send_email(
        subject=f"[trading-bot] {bot} daily summary — {today.isoformat()} — eq ${equity:,.2f}",
        body=line,
    )
