"""Crash-loop detection via startup-timestamp tracking.

Each bot calls track_restart_and_alert(bot_name) early in main(). The function
appends the current UTC timestamp to a small JSON file, prunes entries older
than the window, and fires a single Pushover alert when the recent-startup
count crosses the threshold.

The "exactly once per episode" semantic comes from alerting only when count
EQUALS threshold. Subsequent restarts in the same crash-loop episode push
count above threshold, so the alert doesn't re-fire and spam Pushover. The
alert can fire again only after the window clears (count drops below
threshold), which by definition means the crash loop has stopped.

Storage: logs/state/{bot_name}.restarts.json — a JSON list of ISO timestamps.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from src.atomic_io import write_json_atomic

from src.logger import STATE_DIR
from src.notifier import notify_error

log = logging.getLogger(__name__)

# Defaults chosen to tolerate normal deploy patterns (2-3 manual restarts
# back-to-back) while still catching genuine crash loops where systemd
# restarts the unit every few seconds.
DEFAULT_THRESHOLD = 5
DEFAULT_WINDOW_MIN = 10


def _state_file(bot_name: str) -> Path:
    return Path(STATE_DIR) / f"{bot_name}.restarts.json"


def _load(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log.warning(f"restart_tracker: corrupt state file {path} ({e}); starting fresh")
        return []
    if not isinstance(data, list):
        log.warning(f"restart_tracker: state file {path} not a list (got {type(data).__name__}); starting fresh")
        return []
    return data


def _save(path: Path, timestamps: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, timestamps)


def _parse_iso_utc(ts_str: str) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(ts_str)
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def track_restart_and_alert(
    bot_name: str,
    threshold: int = DEFAULT_THRESHOLD,
    window_minutes: int = DEFAULT_WINDOW_MIN,
    now: Optional[datetime] = None,
) -> int:
    """Record a startup; fire Pushover if recent-startup count crosses threshold.

    Returns the count of recent startups (including this one).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    path = _state_file(bot_name)
    prior_count = 0
    cutoff = now - timedelta(minutes=window_minutes)
    recent: list = []
    for ts_str in _load(path):
        ts = _parse_iso_utc(ts_str)
        if ts is not None and ts >= cutoff:
            recent.append(ts)
    prior_count = len(recent)

    recent.append(now)
    _save(path, [ts.isoformat() for ts in recent])

    count = len(recent)

    # Alert only on the threshold crossing — prior count below, current at/above.
    # This gives one alert per crash-loop episode rather than one per restart.
    if prior_count < threshold <= count:
        msg = f"CRASH LOOP: {count} restarts in {window_minutes}min — check logs/journal"
        log.warning(f"[{bot_name}] {msg}")
        notify_error(bot_name, msg)

    return count
