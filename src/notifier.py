"""Silent macOS desktop notifier.

Fires ONLY on:
  - BUY / SELL trades (rare, 0-5/day)
  - Circuit-breaker freezes
  - Critical errors

Never fires on:
  - Polling ticks / no-ops
  - "No dips found"
  - Recoverable broker timeouts

Uses `osascript display notification` with `sound name ""` to suppress audio.
If macOS still beeps, disable per-app in System Settings → Notifications → Script Editor.
"""
import logging
import os
import shlex
import subprocess
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def notify(title: str, message: str, subtitle: str = "") -> bool:
    """Post a notification. Returns True only on confirmed Pushover (HTTPS)
    delivery. The osascript fallback is best-effort local-only and does NOT
    count as remote delivery for the return value — callers that depend on
    durable alert delivery (e.g. one-shot milestone alerts) need that
    distinction to avoid silently consuming the alert when Pushover is down.

    Existing callers that don't check the return value (notify_buy/sell/freeze/error)
    are unaffected — semantics for them remain swallow-and-continue."""
    user_key = os.getenv("PUSHOVER_USER_KEY")
    api_token = os.getenv("PUSHOVER_API_TOKEN")

    if user_key and api_token:
        payload = urllib.parse.urlencode(
            {
                "token": api_token,
                "user": user_key,
                "title": title,
                "message": message if not subtitle else f"{subtitle} | {message}",
            }
        ).encode()

        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.pushover.net/1/messages.json",
                    data=payload,
                    method="POST",
                ),
                timeout=5,
            ).read()
            return True
        except Exception as e:
            log.debug(f"pushover notify failed (non-fatal): {e}")

    # --- macOS osascript fallback (local only; does not signal delivery) ---
    msg = message.replace('"', "'")
    ttl = title.replace('"', "'")
    sub = subtitle.replace('"', "'") if subtitle else ""

    if sub:
        script = f'display notification "{msg}" with title "{ttl}" subtitle "{sub}" sound name ""'
    else:
        script = f'display notification "{msg}" with title "{ttl}" sound name ""'

    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.debug(f"notify failed (non-fatal): {e}")
    return False


def notify_buy(bot: str, symbol: str, usd: float, price: float) -> None:
    notify(
        title=f"[{bot}] BUY",
        message=f"{symbol} ${usd:.2f} @ ${price:.2f}",
    )


def notify_sell(bot: str, symbol: str, price: float, pnl_pct: float, reason: str) -> None:
    sign = "+" if pnl_pct >= 0 else ""
    notify(
        title=f"[{bot}] SELL {symbol}",
        message=f"{sign}{pnl_pct:.2f}% @ ${price:.2f}  ({reason})",
    )


def notify_freeze(bot: str, dd_pct: float, until: str) -> None:
    notify(
        title=f"[{bot}] CIRCUIT BREAKER",
        message=f"DD {dd_pct:.1f}% — sizing frozen until {until}",
    )


def notify_daily_brake(bot: str, realized_pnl: float, limit_usd) -> None:
    # limit_usd is None when the configured limit could not be resolved; the
    # brake is active in that case, so this must not raise on formatting.
    limit_desc = "UNRESOLVED config" if limit_usd is None else f"-{limit_usd:.2f}"
    notify(
        title=f"[{bot}] DAILY LOSS BRAKE",
        message=f"Realized {realized_pnl:+.2f} today (limit {limit_desc}) — "
                f"no new entries. Exits and stops continue.",
    )


def notify_error(bot: str, message: str) -> None:
    notify(
        title=f"[{bot}] ERROR",
        message=message[:120],  # truncate long errors
    )


def notify_milestone(bot: str, label: str, message: str) -> bool:
    """Positive operational event — not a trade, not an error. e.g. a capital
    threshold being reached that unlocks a deferred strategy.

    Returns True only on confirmed remote delivery. Callers that gate state on
    delivery (one-shot milestones) MUST check the return value before recording
    the milestone as fired, or a transient Pushover outage will silently
    consume the alert."""
    return notify(title=f"[{bot}] {label}", message=message[:200])
