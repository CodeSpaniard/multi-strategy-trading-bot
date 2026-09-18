"""Daily realized-loss entry brake.

Policy: for each UTC calendar day, track net realized P&L from trades closed
that day. When the bot reaches an entry decision, if that figure is at or below
the configured limit, open nothing further for the rest of the UTC day. Exits,
stop management and everything else continue untouched.

Three things this module exists to get right, all of them lessons from the
legacy `RiskManager` that was never deployed:

  The accumulator is DAILY, not per-process. `RiskManager` captured
  `starting_equity` once at startup and never refreshed it, so its "daily" loss
  limit was really a since-last-restart limit. Here the figure is persisted
  alongside the UTC date it describes, and restored only if that date is still
  today — so restarting the bot can never clear a limit that has been reached.

  The day boundary is EXPLICIT UTC, not `datetime.now().date()`. The latter
  means whatever the host timezone happens to be. It is currently correct only
  because the droplet runs UTC.

  It measures REALIZED P&L, not mark-to-market equity. Unrealized moves on open
  positions are the job of the per-position stops; this brake answers the
  narrower question of how much has actually been lost today.

Pure functions. State is passed in, decisions are returned. No I/O.
"""
from datetime import datetime, timezone
from typing import Optional, Tuple


def utc_today_iso(now: datetime = None) -> str:
    """The day boundary, stated explicitly rather than inherited from the host."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date().isoformat()


def restore_daily(prior: dict, today_iso: str) -> Tuple[float, int, int]:
    """(realized_pnl, closed_trades, wins) for today, from a persisted state dict.

    Returns zeros unless the stored block belongs to today. A state file written
    before this feature existed has no `daily` block and so starts fresh, which
    is correct — the figure was never persisted then either.
    """
    daily = prior.get("daily") or {}
    if daily.get("date") != today_iso:
        return 0.0, 0, 0
    try:
        return (float(daily.get("realized_pnl", 0.0)),
                int(daily.get("closed_trades", 0)),
                int(daily.get("wins", 0)))
    except (TypeError, ValueError):
        return 0.0, 0, 0


def daily_block(today_iso: str, realized_pnl: float, closed_trades: int, wins: int) -> dict:
    """The persisted shape. Grouped with its date because the counters are
    meaningless without it — restoring a P&L against the wrong day is the
    failure this prevents."""
    return {
        "date": today_iso,
        "realized_pnl": round(float(realized_pnl), 4),
        "closed_trades": int(closed_trades),
        "wins": int(wins),
    }


def resolve_limit(raw) -> Tuple[Optional[float], Optional[str]]:
    """Turn the configured value into (effective_limit, error_message_or_None).

    Three states, because "deliberately off" and "cannot be determined" are
    different claims and must not collapse into the same value:

      0.0   -> the operator chose no brake
      float -> enabled at that value
      None  -> UNRESOLVED; the constraint cannot be established

    Configuration policy:

      absent / 0   -> 0.0, disabled. Code capability is not risk-policy
                      activation; the config activates it.
      positive     -> that value.
      negative     -> the magnitude, ACTIVE, plus a diagnostic. A wrong sign must
                      not silently switch off a risk control, and must not
                      silently be treated as if it were intended either.
      non-numeric  -> None, plus a diagnostic. No magnitude can be inferred, and
                      if the constraint cannot be established then we cannot know
                      whether we are inside it — so entries stop. Deliberately
                      not "disabled": a typo must never silently remove a live
                      risk control. Deliberately not "refuse to start": this
                      bot's stops are enforced by the running loop rather than by
                      resting broker orders, so a process that will not start is
                      live positions with no stop management.
    """
    if raw is None or raw == "":
        return 0.0, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, (f"risk.daily_realized_loss_limit_usd is not a number ({raw!r}); "
                      f"the limit is UNRESOLVED — all new entries are blocked until "
                      f"the config is fixed. Exits and stops continue.")
    if value == 0:
        return 0.0, None
    if value < 0:
        return abs(value), (f"risk.daily_realized_loss_limit_usd is negative ({value}); "
                            f"treating magnitude ${abs(value):.2f} as the ACTIVE daily "
                            f"realized-loss limit — fix the config")
    return value, None


def entries_blocked(realized_pnl: float, limit_usd: Optional[float]) -> bool:
    """True when new entries must be refused.

    `limit_usd` must come from resolve_limit — never a raw config value, because
    None is load-bearing here and means something different from 0.

      None -> the constraint could not be established, so we cannot know whether
              we are inside it. Refuse entries rather than trade without the risk
              control we believe we have.
      0    -> deliberately disabled.
      >0   -> block once today's net realized P&L reaches -limit. `<=` not `<`:
              the limit is the maximum permitted loss, so landing exactly on it
              has reached it.
    """
    if limit_usd is None:
        return True
    if not limit_usd:
        return False
    return realized_pnl <= -abs(limit_usd)
