"""Symmetric mirror of the dip scanner: buy stocks up ≥N% today, ride continuation.

Philosophy: the dip scanner assumes mean reversion. This tests the opposite —
does a stock up 5% today tend to continue up over the next N days? If yes,
strong up-days would be complementary signals to dips (they occur on different
days) and could increase total scanner activity.

Same universe (75 S&P large-caps), same 18-month window, same cost model as
backtest_scanner.py for direct comparison.

Exits tested (differ from dip scanner because we're NOT mean-reverting):
  - Take-profit at +take_profit_pct from entry
  - Trailing stop at -trail_pct from high-water mark
  - Hard stop at -hard_stop_pct from entry
  - Timeout: close at T+N days if no other exit hit (avoids holding forever)
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# Same universe as the (expanded) dip scanner
UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "V",
    "UNH", "MA", "HD", "PG", "JNJ", "MRK", "ABBV", "CVX", "XOM", "BAC",
    "KO", "PEP", "COST", "AVGO", "TMO", "WMT", "MCD", "CSCO", "ACN", "LIN",
    "AMD", "ADBE", "CRM", "NFLX", "TXN", "INTC", "QCOM", "AMAT", "ISRG", "NOW",
    "GS", "MS", "BLK", "SCHW", "AXP", "C", "DE", "CAT", "BA", "RTX",
    "NEE", "SO", "DUK", "AEP", "D",
    "PLD", "AMT", "CCI",
    "HON", "UNP", "GE", "LMT", "MMM", "UPS",
    "COP", "SLB", "EOG",
    "NKE", "SBUX", "DIS", "TGT",
    "T", "VZ", "CMCSA", "MO",
]


def fetch_daily(client, symbol, start, end):
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
    try:
        df = client.get_stock_bars(req).df
    except Exception:
        return None
    if df.empty:
        return None
    if "symbol" in df.index.names:
        df = df.xs(symbol, level="symbol")
    return df


def run_backtest(start, end,
                 surge_threshold_pct=5.0,       # min today-gain to consider entry
                 take_profit_pct=10.0,          # exit if +this% from entry
                 hard_stop_pct=5.0,             # exit if -this% from entry
                 trail_pct=4.0,                 # exit if drawdown from hwm exceeds this
                 timeout_days=10,               # force-close stale positions
                 max_positions=5,
                 cost_bps=10):
    load_dotenv(".env.paper", override=True)
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

    print(f"Fetching daily bars for {len(UNIVERSE)} stocks...", flush=True)
    all_data = {}
    for sym in UNIVERSE:
        df = fetch_daily(client, sym, start, end)
        if df is not None and len(df) > 20:
            all_data[sym] = df
    print(f"  Got data for {len(all_data)} stocks.", flush=True)

    sample = next(iter(all_data.values()))
    dates = sample.index.tolist()

    daily_returns = {sym: df["close"].astype(float).pct_change() * 100 for sym, df in all_data.items()}

    trades = []
    open_positions = {}  # sym -> {entry_price, entry_date, hwm}

    for i in range(1, len(dates)):
        today = dates[i]

        # 1. Exit checks on open positions
        for sym in list(open_positions.keys()):
            df = all_data.get(sym)
            if df is None or today not in df.index:
                continue
            pos = open_positions[sym]
            px = float(df.loc[today, "close"])
            pos["hwm"] = max(pos["hwm"], px)

            pnl_pct = (px - pos["entry_price"]) / pos["entry_price"] * 100
            hwm_dd = (px - pos["hwm"]) / pos["hwm"] * 100
            age = (today - pos["entry_date"]).days if hasattr(today - pos["entry_date"], "days") else 0

            exit_reason = None
            if pnl_pct >= take_profit_pct:
                exit_reason = "take-profit"
            elif pnl_pct <= -hard_stop_pct:
                exit_reason = "hard-stop"
            elif hwm_dd <= -trail_pct:
                exit_reason = "trailing-stop"
            elif age >= timeout_days:
                exit_reason = "timeout"

            if exit_reason:
                net = pnl_pct - cost_bps / 100
                trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": pos["entry_price"], "exit_price": px,
                    "surge_pct": pos["surge_pct"],
                    "gross_pnl": pnl_pct, "net_pnl": net, "reason": exit_reason,
                })
                del open_positions[sym]

        # 2. Find today's surges (only if we have capacity)
        if len(open_positions) >= max_positions:
            continue

        surge_candidates = []
        for sym, rets in daily_returns.items():
            if sym in open_positions:
                continue
            if today not in rets.index:
                continue
            ret = float(rets.loc[today])
            if ret >= surge_threshold_pct:
                surge_candidates.append((sym, ret))

        surge_candidates.sort(key=lambda x: x[1], reverse=True)
        slots = max_positions - len(open_positions)
        for sym, surge in surge_candidates[:slots]:
            df = all_data[sym]
            px = float(df.loc[today, "close"])
            open_positions[sym] = {
                "entry_price": px, "entry_date": today,
                "hwm": px, "surge_pct": surge,
            }

    # Close any remaining at end
    for sym, pos in open_positions.items():
        df = all_data[sym]
        px = float(df["close"].iloc[-1])
        pnl_pct = (px - pos["entry_price"]) / pos["entry_price"] * 100
        trades.append({
            "symbol": sym, "entry_date": pos["entry_date"], "exit_date": dates[-1],
            "entry_price": pos["entry_price"], "exit_price": px,
            "surge_pct": pos["surge_pct"],
            "gross_pnl": pnl_pct, "net_pnl": pnl_pct - cost_bps / 100, "reason": "eof",
        })

    return trades


def summarize(trades, label):
    if not trades:
        print(f"[{label}] No trades.")
        return
    net = [t["net_pnl"] for t in trades]
    wins = [p for p in net if p > 0]
    cum = 0; peak = 0; max_dd = 0
    for p in net:
        cum += p; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1

    hold_days = []
    for t in trades:
        delta = t["exit_date"] - t["entry_date"]
        hold_days.append(delta.days if hasattr(delta, "days") else 1)

    print(f"\n  [{label}]")
    print(f"  Trades:           {len(trades)}")
    print(f"  Win rate:         {len(wins)/len(trades)*100:.1f}%  ({len(wins)}W / {len(trades)-len(wins)}L)")
    print(f"  Total net P&L:    {sum(net):+.2f}%")
    print(f"  Avg per trade:    {sum(net)/len(trades):+.3f}%")
    print(f"  Best trade:       {max(net):+.2f}%")
    print(f"  Worst trade:      {min(net):+.2f}%")
    print(f"  Max DD (seq):     {max_dd:+.2f}%")
    print(f"  Avg hold:         {sum(hold_days)/len(hold_days):.1f} days")
    print(f"  Exits: {dict(reasons)}")


def main():
    print("=" * 72)
    print("MOMENTUM BREAKOUT SCANNER  —  sweep across parameter sets")
    print("Period: 2024-01-01 → 2025-06-30 (18 months)")
    print("=" * 72, flush=True)

    configs = [
        # (surge%, TP%, hard_stop%, trail%, timeout_d, label)
        (5.0, 10.0, 5.0, 4.0, 10, "5% surge, TP=10%, SL=5%, trail=4%, timeout=10d"),
        (5.0,  8.0, 4.0, 3.0,  7, "5% surge, TP=8%, SL=4%, trail=3%, timeout=7d"),
        (3.0,  6.0, 3.0, 2.5,  5, "3% surge (looser), TP=6%, SL=3%, trail=2.5%, timeout=5d"),
        (7.0, 15.0, 7.0, 5.0, 15, "7% surge (stricter), TP=15%, SL=7%, trail=5%, timeout=15d"),
    ]

    for surge, tp, sl, trail, timeout, label in configs:
        trades = run_backtest(
            start=datetime(2024, 1, 1), end=datetime(2025, 6, 30),
            surge_threshold_pct=surge, take_profit_pct=tp,
            hard_stop_pct=sl, trail_pct=trail, timeout_days=timeout,
        )
        summarize(trades, label)


if __name__ == "__main__":
    main()
