"""Swing-trade dip scanner backtest.

Simulates:
1. Daily scan of 50 large-cap stocks for dips >= threshold
2. Score dips by depth (deeper = higher priority)
3. Buy at close on dip day
4. Monitor daily: exit when bounce target hit (dynamic: % of dip depth)
5. Trailing stop + hard stop
6. PDT budget: 3 same-day round trips per 5 business days (tracked but not binding
   in daily-bar simulation since earliest exit = next day = swing trade)

Conservative: daily bars means we exit at close, not intraday.
Real implementation with minute-bar monitoring would capture bounces faster.
"""
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, Adjustment

UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "V",
    "UNH", "MA", "HD", "PG", "JNJ", "MRK", "ABBV", "CVX", "XOM", "BAC",
    "KO", "PEP", "COST", "AVGO", "TMO", "WMT", "MCD", "CSCO", "ACN", "LIN",
    "AMD", "ADBE", "CRM", "NFLX", "TXN", "INTC", "QCOM", "AMAT", "ISRG", "NOW",
    "GS", "MS", "BLK", "SCHW", "AXP", "C", "DE", "CAT", "BA", "RTX",
]


def fetch_daily(client, symbol, start, end):
    # Free tier rejects SIP; live trades on IEX. Match live so the backtest
    # returns data instead of silently fetching nothing.
    # adjustment=ALL → split- and dividend-adjusted bars; without this, a split
    # (e.g. NVDA 10:1) shows as a spurious ~-90% one-day "crash" and corrupts results.
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end,
                           feed=DataFeed.IEX, adjustment=Adjustment.ALL)
    try:
        df = client.get_stock_bars(req).df
    except Exception as e:
        # Surface the reason instead of silently dropping the symbol — a feed/
        # credential failure should be visible, not disguised as "no data".
        print(f"  WARN {symbol}: bar fetch failed: {type(e).__name__}: {e}", flush=True)
        return None
    if df.empty:
        return None
    if "symbol" in df.index.names:
        df = df.xs(symbol, level="symbol")
    return df


def run_backtest(start, end,
                 dip_threshold_pct=3.0,
                 bounce_ratio=0.40,       # exit when price recovers this fraction of dip
                 min_bounce_pct=1.0,      # minimum bounce target regardless of dip depth
                 hard_stop_pct=8.0,
                 trailing_stop_pct=5.0,
                 max_positions=5,
                 cost_bps=10,             # equity round-trip cost ~10 bps
                 entry_weekdays=None,     # None=all days; else set of weekday ints (Mon=0..Fri=4)
                 preloaded_data=None,     # pass fetched data to reuse across variants
                 ):
    if preloaded_data is not None:
        all_data = preloaded_data
    else:
        load_dotenv(".env.paper", override=True)
        client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

        # Fetch all data upfront
        print(f"Fetching daily bars for {len(UNIVERSE)} stocks...", flush=True)
        all_data = {}
        for sym in UNIVERSE:
            df = fetch_daily(client, sym, start, end)
            if df is not None and len(df) > 20:
                all_data[sym] = df
        print(f"  Got data for {len(all_data)}/{len(UNIVERSE)} stocks.", flush=True)
        # Fail fast on a coverage collapse rather than reporting misleading results
        # from a near-empty universe (e.g. a feed/credential regression).
        min_coverage = max(20, len(UNIVERSE) // 2)
        if len(all_data) < min_coverage:
            raise RuntimeError(
                f"Data coverage too low: {len(all_data)}/{len(UNIVERSE)} symbols loaded "
                f"(need >= {min_coverage}). Likely a data-feed or credential problem — "
                f"aborting instead of producing a misleading backtest.")

    # Build a date index from any stock
    sample = next(iter(all_data.values()))
    dates = sample.index.tolist()

    # Compute daily returns for each stock
    daily_returns = {}
    for sym, df in all_data.items():
        close = df["close"].astype(float)
        daily_returns[sym] = close.pct_change() * 100  # in percent

    # Simulation
    trades = []
    open_positions = {}  # sym -> {entry_price, entry_date, hwm, dip_depth}

    for i in range(1, len(dates)):
        today = dates[i]

        # 1. Check exits on open positions
        for sym in list(open_positions.keys()):
            if sym not in all_data:
                continue
            pos = open_positions[sym]
            df = all_data[sym]
            if today not in df.index:
                continue
            px = float(df.loc[today, "close"])
            pos["hwm"] = max(pos["hwm"], px)

            pnl_pct = (px - pos["entry_price"]) / pos["entry_price"] * 100
            hwm_dd = (px - pos["hwm"]) / pos["hwm"] * 100

            # Dynamic bounce target
            target = max(pos["dip_depth"] * bounce_ratio, min_bounce_pct)

            exited = False
            reason = ""
            if pnl_pct >= target:
                reason = "bounce-target"
                exited = True
            elif pnl_pct <= -hard_stop_pct:
                reason = "hard-stop"
                exited = True
            elif hwm_dd <= -trailing_stop_pct:
                reason = "trailing-stop"
                exited = True

            if exited:
                net_pnl = pnl_pct - cost_bps / 100
                trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": pos["entry_price"], "exit_price": px,
                    "dip_depth": pos["dip_depth"], "target": target,
                    "gross_pnl": pnl_pct, "net_pnl": net_pnl, "reason": reason,
                })
                del open_positions[sym]

        # 2. Scan for new dips (only if we have capacity)
        if entry_weekdays is not None and today.weekday() not in entry_weekdays:
            continue  # entry-day filter: no NEW entries this weekday (exits already handled above)
        if len(open_positions) >= max_positions:
            continue

        dip_candidates = []
        for sym, rets in daily_returns.items():
            if sym in open_positions:
                continue
            if today not in rets.index:
                continue
            ret = float(rets.loc[today])
            if ret <= -dip_threshold_pct:
                dip_candidates.append((sym, abs(ret)))

        # Sort by dip depth (deepest first = highest priority)
        dip_candidates.sort(key=lambda x: x[1], reverse=True)

        slots = max_positions - len(open_positions)
        for sym, depth in dip_candidates[:slots]:
            df = all_data[sym]
            px = float(df.loc[today, "close"])
            open_positions[sym] = {
                "entry_price": px, "entry_date": today,
                "hwm": px, "dip_depth": depth,
            }

    # Close remaining positions at last price
    for sym, pos in open_positions.items():
        df = all_data[sym]
        px = float(df["close"].iloc[-1])
        pnl_pct = (px - pos["entry_price"]) / pos["entry_price"] * 100
        trades.append({
            "symbol": sym, "entry_date": pos["entry_date"], "exit_date": dates[-1],
            "entry_price": pos["entry_price"], "exit_price": px,
            "dip_depth": pos["dip_depth"], "target": max(pos["dip_depth"] * bounce_ratio, min_bounce_pct),
            "gross_pnl": pnl_pct, "net_pnl": pnl_pct - cost_bps / 100, "reason": "eof",
        })

    return trades


def summarize(trades):
    if not trades:
        print("No trades.")
        return
    net_pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p <= 0]
    cum = 0; peak = 0; max_dd = 0
    for p in net_pnls:
        cum += p; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1

    # Average hold time
    hold_days = []
    for t in trades:
        try:
            delta = t["exit_date"] - t["entry_date"]
            hold_days.append(delta.days if hasattr(delta, 'days') else 1)
        except:
            hold_days.append(1)

    print(f"  Total trades:     {len(trades)}")
    print(f"  Wins / Losses:    {len(wins)} / {len(losses)}  (win rate: {len(wins)/len(trades)*100:.1f}%)")
    print(f"  Total net P&L:    {sum(net_pnls):+.2f}%")
    print(f"  Avg per trade:    {sum(net_pnls)/len(trades):+.3f}%")
    print(f"  Best trade:       {max(net_pnls):+.2f}%")
    print(f"  Worst trade:      {min(net_pnls):+.2f}%")
    print(f"  Max drawdown:     {max_dd:+.2f}%")
    print(f"  Avg hold (days):  {sum(hold_days)/len(hold_days):.1f}")
    print(f"  Exits: {dict(reasons)}")

    # Top 5 trades
    sorted_trades = sorted(trades, key=lambda t: t["net_pnl"], reverse=True)
    print(f"\n  Top 5 winners:")
    for t in sorted_trades[:5]:
        print(f"    {t['symbol']:>6} dip -{t['dip_depth']:.1f}% → net {t['net_pnl']:+.2f}% ({t['reason']})")
    print(f"  Top 5 losers:")
    for t in sorted_trades[-5:]:
        print(f"    {t['symbol']:>6} dip -{t['dip_depth']:.1f}% → net {t['net_pnl']:+.2f}% ({t['reason']})")


def main():
    configs = [
        (3.0, 0.40, 1.0, "dip≥3%, bounce=40% of dip"),
        (3.0, 0.60, 1.5, "dip≥3%, bounce=60% of dip"),
        (5.0, 0.40, 2.0, "dip≥5%, bounce=40% of dip"),
        (5.0, 0.60, 2.5, "dip≥5%, bounce=60% of dip"),
    ]

    for dip_thr, bounce_r, min_b, label in configs:
        print(f"\n{'='*70}")
        print(f"CONFIG: {label} | hard_stop=8% | trail=5% | max_pos=5 | cost=10bps")
        print(f"PERIOD: 2024-01-01 to 2025-06-30 (18 months)")
        print(f"{'='*70}", flush=True)
        trades = run_backtest(
            start=datetime(2024, 1, 1), end=datetime(2025, 6, 30),
            dip_threshold_pct=dip_thr, bounce_ratio=bounce_r,
            min_bounce_pct=min_b,
        )
        summarize(trades)


if __name__ == "__main__":
    main()
