"""One-off execution-fidelity check: live scanner-live entries vs the backtest.

Compares the DETERMINISTIC part (which symbols flagged as >=5% dips on which days,
and at what entry price) between the live bot and what the dip-scanner backtest would
have done over the same window. Exits/sizing legitimately diverge (intraday vs daily
bars, dynamic sizing) so this focuses on entry signal fidelity + entry slippage.
"""
import os
from datetime import datetime
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, Adjustment

import src.scanner as live_scanner
import backtests.backtest_scanner as bt

# Use the LIVE universe (75 names) for the backtest, not its hardcoded old 50.
bt.UNIVERSE = list(live_scanner.UNIVERSE)
UNIVERSE = bt.UNIVERSE

# Free tier rejects SIP; live uses IEX. Patch the backtest's fetch to match live.
def _fetch_iex(client, symbol, start, end):
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                           start=start, end=end, feed=DataFeed.IEX,
                           adjustment=Adjustment.ALL)
    try:
        df = client.get_stock_bars(req).df
    except Exception:
        return None
    if df.empty:
        return None
    if "symbol" in df.index.names:
        df = df.xs(symbol, level="symbol")
    return df
bt.fetch_daily = _fetch_iex

START = datetime(2026, 4, 22)   # first full day live; entries begin next bar (04-23)
END = datetime(2026, 6, 6)

# Live params (configs/config.scanner.live.yaml)
PARAMS = dict(dip_threshold_pct=5.0, bounce_ratio=0.40, min_bounce_pct=2.0,
              hard_stop_pct=8.0, trailing_stop_pct=5.0, max_positions=5)

# Actual live BUYs parsed from logs: (date, symbol, fill_price)
LIVE_BUYS = [
    ("2026-04-23", "NOW", 85.19), ("2026-04-23", "TMO", 465.26),
    ("2026-04-23", "CRM", 173.06), ("2026-04-23", "ADBE", 238.31),
    ("2026-04-23", "ACN", 177.63), ("2026-04-24", "CMCSA", 27.68),
    ("2026-04-28", "AMAT", 384.35), ("2026-04-30", "META", 614.88),
    ("2026-05-04", "UPS", 96.15), ("2026-05-11", "ISRG", 421.08),
    ("2026-05-11", "TGT", 117.91), ("2026-05-12", "QCOM", 210.02),
    ("2026-05-12", "INTC", 120.12), ("2026-05-13", "ACN", 159.48),
    ("2026-05-14", "QCOM", 201.94), ("2026-05-15", "INTC", 109.31),
    ("2026-05-18", "AMAT", 414.37), ("2026-05-21", "WMT", 121.94),
    ("2026-05-21", "DE", 529.72), ("2026-05-27", "QCOM", 231.36),
    ("2026-06-01", "QCOM", 228.67), ("2026-06-01", "INTC", 108.93),
    ("2026-06-02", "NOW", 127.53), ("2026-06-02", "ACN", 185.51),
    ("2026-06-02", "NKE", 43.65), ("2026-06-03", "NOW", 118.07),
    ("2026-06-03", "CMCSA", 23.51), ("2026-06-04", "AVGO", 423.10),
]

load_dotenv(".env.paper", override=True)
client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

# Fetch daily bars myself for the per-buy dip + slippage detail.
print(f"Fetching daily bars for {len(UNIVERSE)} symbols...", flush=True)
closes = {}   # symbol -> {date(): close}
rets = {}     # symbol -> {date(): pct_change vs prev trading day}
for sym in UNIVERSE:
    req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=START, end=END, feed=DataFeed.IEX, adjustment=Adjustment.ALL)
    try:
        df = client.get_stock_bars(req).df
    except Exception:
        continue
    if df.empty:
        continue
    if "symbol" in df.index.names:
        df = df.xs(sym, level="symbol")
    c = df["close"].astype(float)
    pc = c.pct_change() * 100
    closes[sym] = {ts.date(): float(v) for ts, v in c.items()}
    rets[sym] = {ts.date(): (None if v != v else float(v)) for ts, v in pc.items()}

def to_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()

# ---- Part 1: Entry fidelity (was each live BUY a real >=5% dip?) + slippage ----
print("\n" + "=" * 78)
print("PART 1  ENTRY FIDELITY — was each live BUY a real >=5% daily dip? + slippage")
print("=" * 78)
print(f"{'date':<11}{'sym':<7}{'dip% (daily)':>13}{'>=5%?':>7}{'fill':>10}{'close':>10}{'slip%':>8}")
real, boundary, bad = 0, 0, 0
slips = []
for d, sym, fill in LIVE_BUYS:
    dt = to_date(d)
    r = rets.get(sym, {}).get(dt)
    cl = closes.get(sym, {}).get(dt)
    if r is None or cl is None:
        print(f"{d:<11}{sym:<7}{'NO BAR':>13}")
        continue
    slip = (fill - cl) / cl * 100
    slips.append(slip)
    flag = "yes" if r <= -5.0 else ("~" if r <= -4.5 else "NO")
    if flag == "yes":
        real += 1
    elif flag == "~":
        boundary += 1
    else:
        bad += 1
    print(f"{d:<11}{sym:<7}{r:>12.2f}%{flag:>7}{fill:>10.2f}{cl:>10.2f}{slip:>+7.2f}%")
print("-" * 78)
print(f"Confirmed >=5% dips: {real} | boundary (-4.5..-5%): {boundary} | NOT a dip: {bad}  (of {len(LIVE_BUYS)})")
if slips:
    avg = sum(slips) / len(slips)
    print(f"Entry slippage vs daily close: mean {avg:+.2f}%  min {min(slips):+.2f}%  max {max(slips):+.2f}%")

# ---- Part 2: Run the real backtest over the window; compare entry sets ----
print("\n" + "=" * 78)
print("PART 2  BACKTEST'S OWN ENTRIES (live universe, live params) vs live entries")
print("=" * 78)
bt_trades = bt.run_backtest(start=START, end=END, **PARAMS)
bt_entries = {}  # (date, sym) -> entry_price
for t in bt_trades:
    ed = t["entry_date"]
    ed = ed.date() if hasattr(ed, "date") else ed
    bt_entries[(ed, t["symbol"])] = t["entry_price"]

live_set = {(to_date(d), s) for d, s, _ in LIVE_BUYS}
bt_set = set(bt_entries.keys())

both = sorted(live_set & bt_set)
live_only = sorted(live_set - bt_set)
bt_only = sorted(bt_set - live_set)
print(f"Backtest total entries in window: {len(bt_set)} | live entries: {len(live_set)}")
print(f"Matched (same symbol+day): {len(both)}")
print(f"Live-only (live bought, backtest didn't): {len(live_only)}")
print(f"Backtest-only (backtest bought, live didn't): {len(bt_only)}")
print("\nLive-only entries:")
for d, s in live_only:
    r = rets.get(s, {}).get(d)
    print(f"  {d} {s:<6} daily dip {r:+.2f}%" if r is not None else f"  {d} {s}")
print("\nBacktest-only entries (live missed — usually slot-cap/timing):")
for d, s in bt_only:
    r = rets.get(s, {}).get(d)
    print(f"  {d} {s:<6} daily dip {r:+.2f}%" if r is not None else f"  {d} {s}")
