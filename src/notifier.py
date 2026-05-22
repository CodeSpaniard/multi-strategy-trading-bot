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


def notify(title: str, message: str, subtitle: str = "") -> None:
    """Post a notification. Prefers Pushover if credentials are set, else macOS osascript.
    Failures are logged, not raised."""
    # --- Pushover branch ---
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
            return
        except Exception as e:
            log.debug(f"pushover notify failed (non-fatal): {e}")

    # --- macOS osascript fallback ---
    # AppleScript expects double-quoted strings with internal quotes escaped.
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


def notify_error(bot: str, message: str) -> None:
    notify(
        title=f"[{bot}] ERROR",
        message=message[:120],  # truncate long errors
    )
