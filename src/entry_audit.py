"""Post-close entry-sanity audit for the dip scanner.

Independent check that every live BUY corresponds to a real >=5% daily dip,
recomputed from SETTLED IEX daily bars rather than the live intraday feed. Entry
slippage (fill vs settled close) is reported for visibility but only alerted on
when implausibly large. This catches signal/feed/logic regressions of the
kind that broke the scanner on Day 1 (default SIP feed silently returning no
data): cases where the bot keeps trading but buys names that were not actually
dips, or fills drift away from the price the strategy assumes.

Why this is independent of the live signal: the bot decides at ~15:50 ET on an
intraday snapshot; this re-derives the dip from the official daily close fetched
after the session settles. A genuine bug (flat/positive "dips", wrong universe,
broken feed) shows up here even though the bot's own logs look fine.

Run after market close (settled daily bar is available ~16:05 ET). Alerts via
Pushover on any flag; stays silent on a clean day. Exit code 1 if any flag.

Usage:
    python -m src.entry_audit                 # audit today (ET)
    python -m src.entry_audit 2026-06-04      # audit a specific date
"""
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, Adjustment

from src.logger import DAILY_DIR
from src.scanner import UNIVERSE
from src.notifier import notify
from src.healthcheck import ping_healthcheck

# Threshold the live signal fires at.
DIP_THRESHOLD_PCT = 5.0
# A buy is flagged "not a dip" only if the SETTLED daily close is shallower than
# this. It is intentionally lenient vs the 5% signal: the bot scans 10 min before
# close on an intraday price, so a name down >=5% at 15:50 can settle a touch
# higher (worst legit boundary observed: -4.68%). Anything shallower than -4.0%
# at the close almost certainly was not a real dip when bought -> investigate.
DIP_FLOOR_PCT = -4.0
# Entry slippage = fill price vs the SETTLED close. This intentionally includes
# the ~10 min of intraday drift between the 15:50 scan/fill and the 16:00 close,
# so on a sharply trending day it can legitimately reach ~2%+ with no execution
# problem (observed live on a -10% semiconductor selloff day). Per-trade slippage
# is therefore INFORMATIONAL (always printed); we only ALERT when it is large
# enough to indicate a genuinely bad fill (wrong/stale price, fat finger), not
# normal scan-to-close drift.
# NOTE: 4.0 is a CONSERVATIVE operational tolerance, not a calibrated value — it
# sits above the worst observed normal drift (~2.2% on the 2026-06-05 selloff,
# n=1 volatile day) with margin, below fat-finger territory. Revisit once more
# volatile-day samples accumulate; the more principled long-term check is a drift
# in the *rolling mean* slippage (execution degradation) rather than per-trade.
SLIPPAGE_ALERT_PCT = 4.0


def _et_today() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def parse_buys(date_str: str, bot: str) -> tuple[list[tuple[str, float]], int]:
    """Return (buys, unparsable_count) for that day's log.

    A line is a BUY *event* if it is INFO-level and tagged for this bot. For each
    such line we try to extract (symbol, fill_price). Handles both log shapes:
      BUY NOW $50.00 @ ~$85.19 | target +6.9%
      BUY QCOM $75.00 @ $228.67 (fill_qty=0.32) | target +2.8%

    A BUY event whose fields can't be extracted is COUNTED, not silently dropped:
    if the scanner's log format drifts, the audit must fail loud rather than mistake
    a real trading day for an empty (falsely "clean") one.
    """
    path = f"{DAILY_DIR}/{date_str}.{bot}.log"
    if not os.path.exists(path):
        return [], 0
    marker = f"[{bot}] BUY "
    rx = re.compile(rf"\[{re.escape(bot)}\] BUY ([A-Z.]+) \$[0-9.]+ @ ~?\$([0-9.]+)")
    buys = []
    unparsed = 0
    with open(path) as f:
        for line in f:
            if "[INFO]" not in line or marker not in line:
                continue
            m = rx.search(line)
            if m:
                buys.append((m.group(1), float(m.group(2))))
            else:
                unparsed += 1
    return buys, unparsed


def daily_dips(symbols: list[str], date_str: str, client) -> dict[str, tuple]:
    """For each symbol return (dip_pct, settled_close) on date_str, or None if no bar."""
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = target - timedelta(days=8)
    end = target + timedelta(days=1)
    out: dict[str, tuple] = {}
    for sym in symbols:
        req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                               start=start, end=end, feed=DataFeed.IEX,
                               adjustment=Adjustment.ALL)  # match the live bot's adjusted-bar policy
        try:
            df = client.get_stock_bars(req).df
        except Exception:
            out[sym] = None
            continue
        if df.empty:
            out[sym] = None
            continue
        if "symbol" in df.index.names:
            df = df.xs(sym, level="symbol")
        closes = {ts.date(): float(v) for ts, v in df["close"].astype(float).items()}
        if target not in closes:
            out[sym] = None
            continue
        prior = [d for d in sorted(closes) if d < target]
        if not prior:
            out[sym] = None
            continue
        prev_close = closes[prior[-1]]
        close = closes[target]
        dip_pct = (close - prev_close) / prev_close * 100
        out[sym] = (dip_pct, close)
    return out


def audit(date_str: str, bot: str, client) -> dict:
    """Return audit result: {date, bot, buys:[...], flags:[...], clean:bool}."""
    buys, unparsed = parse_buys(date_str, bot)
    flags: list[str] = []
    rows = []
    if unparsed:
        flags.append(f"{unparsed} BUY line(s) present but unparsable — scanner log format may have changed")
    if not buys:
        # Only a genuine no-buy day if there were also no unparsable BUY events.
        return {"date": date_str, "bot": bot, "buys": [], "flags": flags,
                "clean": not flags, "no_buys": not unparsed}

    dips = daily_dips([s for s, _ in buys], date_str, client)
    for sym, fill in buys:
        info = dips.get(sym)
        if sym not in UNIVERSE:
            flags.append(f"{sym}: bought OUTSIDE universe")
        if info is None:
            flags.append(f"{sym}: no settled bar — could not verify dip")
            rows.append((sym, fill, None, None, None))
            continue
        dip_pct, close = info
        slip = (fill - close) / close * 100
        rows.append((sym, fill, dip_pct, close, slip))
        if dip_pct > DIP_FLOOR_PCT:
            flags.append(f"{sym}: NOT a dip — settled {dip_pct:+.2f}% (floor {DIP_FLOOR_PCT:.1f}%)")
        if abs(slip) > SLIPPAGE_ALERT_PCT:
            flags.append(f"{sym}: implausible entry slippage {slip:+.2f}% "
                         f"(alert ≥ ±{SLIPPAGE_ALERT_PCT:.1f}% — likely a bad fill, not drift)")

    return {"date": date_str, "bot": bot, "buys": rows, "flags": flags,
            "clean": not flags, "no_buys": False}


def _print(result: dict) -> None:
    print(f"Entry audit {result['bot']} {result['date']}")
    if result.get("no_buys"):
        print("  no buys logged — nothing to audit")
        return
    if result["buys"]:
        print(f"  {'sym':<7}{'fill':>10}{'dip%':>9}{'close':>10}{'slip%':>8}")
        for sym, fill, dip, close, slip in result["buys"]:
            if dip is None:
                print(f"  {sym:<7}{fill:>10.2f}{'  NO BAR':>9}")
            else:
                print(f"  {sym:<7}{fill:>10.2f}{dip:>+8.2f}%{close:>10.2f}{slip:>+7.2f}%")
    if result["clean"]:
        print(f"  CLEAN — {len(result['buys'])} buys, all real dips, slippage in band")
    else:
        print(f"  {len(result['flags'])} FLAG(S):")
        for fl in result["flags"]:
            print(f"    - {fl}")


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else _et_today()
    bot = sys.argv[2] if len(sys.argv) > 2 else "scanner-live"
    # Load the same env file the audited bot runs with, so Alpaca + Pushover
    # creds resolve. scanner-live runs under systemd with `--env .env.live`, so
    # default to that. Local testing can pass a different env file as arg 3
    # (e.g. .env.paper, which carries the same key names).
    env_file = sys.argv[3] if len(sys.argv) > 3 else ".env.live"
    load_dotenv(env_file, override=True)

    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    result = audit(date_str, bot, client)
    _print(result)

    audit_url = os.getenv("HEALTHCHECKS_AUDIT_PING_URL")
    if not result["clean"]:
        notify(
            title=f"[{bot}] ENTRY AUDIT",
            message=f"{result['date']}: {len(result['flags'])} flag(s) — " + "; ".join(result["flags"])[:300],
        )
        if audit_url:
            try:
                import urllib.request
                urllib.request.urlopen(audit_url + "/fail", timeout=5).read()
            except Exception:
                pass
        return 1

    # Clean: stay silent on Pushover; ping healthcheck so a missed run is noticed.
    if audit_url:
        os.environ["HEALTHCHECKS_PING_URL"] = audit_url
        ping_healthcheck()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
