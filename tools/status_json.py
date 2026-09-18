#!/usr/bin/env python3
"""Read-only status reporter for the trading bots. Prints one JSON blob to stdout.

Two tiers, deliberately separated:

  Tier 1 (default)   — pure file reads: state JSON, equity JSONL, log tails,
                       `systemctl is-active`. No broker client is constructed and
                       no API credential is read, so this tier cannot place an
                       order even in principle.
  Tier 2 (--broker)  — additionally asks the brokers for authoritative equity and
                       positions, using the .env files already on this host. Only
                       read-only broker methods are called; the imports live
                       inside the tier-2 function so tier 1 never loads them.

Every timestamp is droplet-local (this host runs UTC). Freshness matters: the
scanner polls every 60s but crypto polls hourly, so a crypto line can be an hour
old and still be perfectly healthy. Rather than make callers guess, each bot
reports its own `poll_seconds`, the `age_seconds` of its newest data, and a
`stale` flag computed against its own cadence.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Run as a script, sys.path[0] is tools/ — put the repo root on it so the
# tier-2 `from src...` imports resolve.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
STATE_DIR = os.path.join(ROOT, "logs", "state")
LOG_DIR = os.path.join(ROOT, "logs", "manual")

# Bot registry. `poll_key` differs because the scanner and crypto loops name
# their cadence differently in YAML.
BOTS = [
    {
        "name": "scanner-live",
        "unit": "trading-bot-scanner-live.service",
        "config": "configs/config.scanner.live.yaml",
        "env": ".env.live",
        "poll_key": "monitor_poll_seconds",
        "kind": "alpaca",
        "paper": False,
    },
    {
        "name": "scanner-paper",
        "unit": "trading-bot-scanner-paper.service",
        "config": "configs/config.scanner.paper.yaml",
        "env": ".env.paper",
        "poll_key": "monitor_poll_seconds",
        "kind": "alpaca",
        "paper": True,
    },
    {
        "name": "crypto-coinbase",
        "unit": "trading-bot-crypto-coinbase.service",
        "config": "configs/config.crypto.coinbase.yaml",
        "env": ".env.coinbase",
        "poll_key": "poll_seconds",
        "kind": "coinbase",
        "paper": False,
    },
]

STALE_MULTIPLIER = 3  # data older than this many poll intervals is flagged stale

# HEARTBEAT is the first statement in each bot's main loop, before any
# market-open check, so it is emitted every poll whether or not anything is
# held. That makes it the liveness signal — position lines are NOT, because a
# flat bot correctly logs none at all.
HEARTBEAT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[\w+\] \[(?P<bot>[\w-]+)\] HEARTBEAT\s*$"
)

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ "
    r"\[\w+\] \[(?P<bot>[\w-]+)\] (?P<sym>[A-Z0-9][A-Z0-9.\-]*): (?P<status>[A-Z]+) \| (?P<body>.*)$"
)


def _f(pattern: str, text: str):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def normalize_held(state: dict) -> dict:
    """The two bot families persist held positions differently — the scanner keeps a
    `positions` dict of per-symbol detail, crypto keeps parallel `entry_prices` /
    `high_water_marks` maps. Flatten both to one shape so the cockpit has a single
    code path."""
    if isinstance(state.get("positions"), dict):
        return {
            sym: {
                "entry_price": d.get("entry_price"),
                "high_water": d.get("high_water"),
                "entry_date": d.get("entry_date"),
                "bounce_target_pct": d.get("bounce_target_pct"),
            }
            for sym, d in state["positions"].items()
        }
    entries = state.get("entry_prices") or {}
    hwms = state.get("high_water_marks") or {}
    return {
        sym: {
            "entry_price": entries.get(sym),
            "high_water": hwms.get(sym),
            "entry_date": None,
            "bounce_target_pct": None,
        }
        for sym in entries
    }


def read_json(path, retries: int = 2, delay_s: float = 0.25):
    """Returns (data, error_string). Never returns a partial document dressed up
    as a valid one.

    The bots persist state with a plain `Path.write_text()` — truncate-then-write,
    not an atomic rename — so a read landing mid-write genuinely sees half a file.
    That is a routine race, not a corruption edge case, so retry briefly before
    reporting it.
    """
    last = None
    for attempt in range(retries + 1):
        try:
            with open(path) as fh:
                return json.load(fh), None
        except FileNotFoundError:
            return None, f"{os.path.basename(path)} not found"
        except (json.JSONDecodeError, OSError) as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(delay_s)
    return None, f"{os.path.basename(path)} unreadable after {retries + 1} attempts ({last})"


def host_clock_warning():
    """Age arithmetic below is naive-local and only correct because this host runs
    UTC. Say so out loud rather than silently reporting wrong ages if that ever
    stops being true."""
    offset = datetime.now().astimezone().utcoffset()
    if offset != timedelta(0):
        return f"host clock is not UTC (offset {offset}) — reported ages will be wrong"
    return None


def tail_bytes(path: str, nbytes: int = 65536) -> list:
    """Last ~nbytes of a file as decoded lines. scanner-live.log is ~27MB, so
    never read the whole thing."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - nbytes))
            chunk = fh.read()
    except OSError:
        return []
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[1:] if size > nbytes and lines else lines


def latest_positions(log_path: str, now: datetime, bot_name: str) -> list:
    """Newest status line per symbol, walking the tail backwards. The line's own
    bot tag must match the log being read — cheap guard against a future log
    format accidentally satisfying this grammar."""
    seen, out = set(), []
    for line in reversed(tail_bytes(log_path)):
        m = LINE_RE.match(line)
        if not m or m.group("bot") != bot_name or m.group("sym") in seen:
            continue
        seen.add(m.group("sym"))
        body = m.group("body")
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        out.append({
            "symbol": m.group("sym"),
            "status": m.group("status"),
            "pnl_pct": _f(r"pnl ([+-]?[\d.]+)%", body),
            "hwm": _f(r"hwm ([\d.]+)", body),
            "dd_pct": _f(r"dd ([+-]?[\d.]+)%", body),
            "target_pct": _f(r"target ([+-]?[\d.]+)%", body),
            "price": _f(r"px=([\d.]+)", body),
            "as_of": ts.isoformat(),
            "age_seconds": round((now - ts).total_seconds(), 1),
            "raw": line,
        })
    return sorted(out, key=lambda p: p["symbol"])


def latest_heartbeat_age(log_path: str, now: datetime, bot_name: str):
    """Seconds since this bot's newest HEARTBEAT, or None if none is in the tail."""
    for line in reversed(tail_bytes(log_path)):
        m = HEARTBEAT_RE.match(line)
        if m and m.group("bot") == bot_name:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            return round((now - ts).total_seconds(), 1)
    return None


def unit_active(unit: str) -> str:
    """`systemctl is-active` needs no privilege — verified as the bot user."""
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError) as e:
        return f"query-failed: {e}"


def tier1(bot: dict, now: datetime, warnings: list) -> dict:
    name = bot["name"]
    cfg = {}
    try:
        with open(os.path.join(ROOT, bot["config"])) as fh:
            cfg = yaml.safe_load(fh) or {}
    except OSError as e:
        warnings.append(f"{name}: config unreadable ({e})")

    poll = (cfg.get("loop") or {}).get(bot["poll_key"])
    sizing = cfg.get("sizing") or {}

    state_path = os.path.join(STATE_DIR, f"{name}.state.json")
    state, state_err = read_json(state_path)
    if state_err:
        warnings.append(f"{name}: state unavailable — {state_err}; "
                        "positions suppressed rather than shown from stale log lines")
    state = state or {}
    try:
        state_age = round((now - datetime.fromtimestamp(os.path.getmtime(state_path))).total_seconds(), 1)
    except OSError:
        state_age = None

    # frozen_until is never cleared once set — it simply ages into the past. Report
    # the raw value AND whether it is actually in force, so the cockpit cannot
    # mistake a months-old expired freeze for a live one.
    frozen_until = state.get("frozen_until")
    frozen_active = False
    if frozen_until:
        try:
            frozen_active = datetime.fromisoformat(frozen_until) > now
        except ValueError:
            warnings.append(f"{name}: unparseable frozen_until {frozen_until!r}")

    log_path = os.path.join(LOG_DIR, f"{name}.log")
    positions = latest_positions(log_path, now, name)
    newest = min((p["age_seconds"] for p in positions), default=None)

    # A position line is the last thing said ABOUT a symbol, not proof the symbol
    # is still held — after a SELL it simply stops being updated and ages forever.
    # State is authoritative, so drop anything no longer held.
    #
    # When state is unreadable we present NOTHING rather than falling back to the
    # unfiltered list. The condition that removes the authoritative filter is
    # exactly the condition under which stale historical lines are most
    # misleading, so current-position display fails closed. The raw line age
    # below and state_error still say what was seen and why it was suppressed.
    held = normalize_held(state)
    if state_err is None:
        positions = [p for p in positions if p["symbol"] in held]
    else:
        positions = []

    # Liveness comes from HEARTBEAT, never from position lines: a bot holding
    # nothing is healthy and logs no position lines at all.
    #
    # Three-valued, because "no heartbeat in the tail" is not the same claim as
    # "heartbeat is old". Absence of evidence must not render as healthy, so
    # `stale` is true for anything that is not positively live.
    heartbeat_age = latest_heartbeat_age(log_path, now, name)
    if heartbeat_age is None:
        liveness = "unknown"
    elif poll and heartbeat_age > poll * STALE_MULTIPLIER:
        liveness = "stale"
    else:
        liveness = "live"
    stale = liveness != "live"

    equity_daily = []
    try:
        with open(os.path.join(STATE_DIR, f"{name}.equity_daily.jsonl")) as fh:
            for line in fh.read().splitlines()[-30:]:
                if line.strip():
                    equity_daily.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        pass

    active = unit_active(bot["unit"])
    if active != "active":
        warnings.append(f"{name}: unit is {active!r} — positions are unmanaged while it is down")
    if liveness == "unknown":
        warnings.append(f"{name}: no HEARTBEAT in log tail — liveness UNKNOWN, treating as not-live")
    elif liveness == "stale":
        warnings.append(f"{name}: last HEARTBEAT {heartbeat_age:.0f}s old, > {STALE_MULTIPLIER}x poll interval ({poll}s)")
    if frozen_active:
        warnings.append(f"{name}: sizing freeze IN FORCE until {frozen_until}")
    if sizing and not sizing.get("enabled", True):
        warnings.append(f"{name}: sizing disabled — no new entries will be opened")

    return {
        "unit": bot["unit"],
        "active": active,
        "poll_seconds": poll,
        "liveness": liveness,
        "stale": stale,
        "heartbeat_age_seconds": heartbeat_age,
        "newest_position_line_age_seconds": newest,
        "state_file_age_seconds": state_age,
        "state_available": state_err is None,
        "state_error": state_err,
        "closed_trades": state.get("closed_trades"),
        "last_size_usd": state.get("last_size_usd"),
        "frozen_until": frozen_until,
        "frozen_active": frozen_active,
        "sizing_enabled": sizing.get("enabled"),
        "held": held,
        "state_updated": state.get("updated") or state.get("today"),
        "equity_samples_count": len(state.get("equity_samples", []) or []),
        "positions": positions,
        "equity_daily": equity_daily,
        "restarts": read_json(os.path.join(STATE_DIR, f"{name}.restarts.json"))[0] or [],
    }


def tier2(bot: dict, warnings: list) -> dict:
    """Authoritative broker read. Only read-only methods are called."""
    from dotenv import load_dotenv  # imported here so tier 1 never loads it

    env_path = os.path.join(ROOT, bot["env"])
    if not os.path.exists(env_path):
        return {"error": f"{bot['env']} not found on host"}
    load_dotenv(env_path, override=True)

    try:
        if bot["kind"] == "alpaca":
            from src.broker import Broker
            b = Broker(paper=bot["paper"])
            positions = []
            for p in b.get_all_positions():
                positions.append({
                    "symbol": getattr(p, "symbol", None),
                    "qty": float(getattr(p, "qty", 0) or 0),
                    "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                    "current_price": float(getattr(p, "current_price", 0) or 0),
                    "market_value": float(getattr(p, "market_value", 0) or 0),
                    "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                    "unrealized_plpc": float(getattr(p, "unrealized_plpc", 0) or 0),
                })
            return {
                "equity": b.account_equity(),
                "market_open": b.market_is_open(),
                "positions": positions,
            }

        from src.coinbase_broker import CoinbaseBroker
        with open(os.path.join(ROOT, bot["config"])) as fh:
            symbols = (yaml.safe_load(fh) or {}).get("symbols", [])
        cb = CoinbaseBroker()
        positions = []
        for sym in symbols:  # no get_all_positions on this broker — iterate config
            qty = cb.get_position_qty(sym)
            price = cb.get_current_price(sym)
            positions.append({
                "symbol": sym,
                "qty": qty,
                "current_price": price,
                "market_value": round(qty * price, 2) if qty and price else 0.0,
            })
        return {"equity": cb.account_equity(), "positions": positions}

    except Exception as e:  # one broker failing must not sink the whole payload
        warnings.append(f"{bot['name']}: broker read failed ({type(e).__name__}: {e})")
        return {"error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only bot status as JSON.")
    ap.add_argument("--broker", action="store_true",
                    help="also query brokers for authoritative equity/positions (tier 2)")
    ap.add_argument("--bot", action="append",
                    help="limit to this bot name (repeatable)")
    args = ap.parse_args()

    now = datetime.now()
    warnings: list = []
    clock_warn = host_clock_warning()
    if clock_warn:
        warnings.append(clock_warn)
    selected = [b for b in BOTS if not args.bot or b["name"] in args.bot]

    bots = {}
    for bot in selected:
        bots[bot["name"]] = tier1(bot, now, warnings)
        if args.broker:
            bots[bot["name"]]["broker"] = tier2(bot, warnings)

    json.dump({
        "schema": 1,
        "tier": "broker" if args.broker else "file",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clock": "droplet local time is UTC; per-bot timestamps are naive local",
        "bots": bots,
        "warnings": warnings,
    }, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
