"""Benchmark: the active crypto trend strategy vs passive alternatives, same window.

Compares, over the identical period:
  - Buy & hold BTC, buy & hold ETH        (Coinbase daily)
  - The live trend strategy on each        (from backtest_crypto_exit_study baseline)
  - Buy & hold S&P 500 via SPY             (Alpaca IEX; price-return, ex-dividends)
  - A high-yield savings account (HYSA)    (illustrative, at assumed APYs)

This sets the BAR: does active crypto trading earn its complexity/risk vs just
holding, vs stocks, vs a risk-free account? Diagnostic only; saves JSON.
Usage: python3 backtest_benchmark.py [--days 1100] [--hysa-rates 4 4.5 5]
"""
import argparse
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from coinbase.rest import RESTClient

from backtest_crypto_trend import fetch_daily_coinbase

# Active trend-strategy baseline (from backtest_crypto_exit_study.py, 2026-06-09).
# NOTE: this is SUM of per-trade unit-notional net P&L — the strategy is in cash
# much of the time, so it is NOT a fully-invested figure like buy-and-hold.
ACTIVE = {
    "BTC-USD": {"net_pct": 50.01, "maxdd_pct": -37.30},
    "ETH-USD": {"net_pct": 53.99, "maxdd_pct": -28.81},
}


def buyhold(close):
    ret = (float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100
    peak = close.cummax()
    dd = float(((close - peak) / peak * 100).min())
    return round(ret, 2), round(dd, 2)


def fetch_spy(start, end):
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed, Adjustment
        load_dotenv(".env.paper", override=True)
        client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
        req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
                               start=start, end=end, feed=DataFeed.IEX,
                               adjustment=Adjustment.ALL)
        df = client.get_stock_bars(req).df
        if df.empty:
            return None
        if "symbol" in df.index.names:
            df = df.xs("SPY", level="symbol")
        return df["close"].astype(float)
    except Exception as e:
        print(f"  WARN SPY fetch failed: {type(e).__name__}: {e}", flush=True)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--hysa-rates", nargs="+", type=float, default=[4.0, 4.5, 5.0])
    args = ap.parse_args()

    load_dotenv(".env.coinbase", override=True)
    cb = RESTClient(api_key=os.environ["COINBASE_API_KEY"], api_secret=os.environ["COINBASE_API_SECRET"])

    rows = []
    win_start = win_end = None
    for sym in ["BTC-USD", "ETH-USD"]:
        print(f"Fetching {sym}...", flush=True)
        df = fetch_daily_coinbase(cb, sym, args.days)
        close = df["close"].astype(float)
        win_start, win_end = df.index[0], df.index[-1]
        bh_ret, bh_dd = buyhold(close)
        a = ACTIVE[sym]
        rows.append({"name": f"Buy & hold {sym}", "ret_pct": bh_ret, "maxdd_pct": bh_dd})
        rows.append({"name": f"  Trend strategy {sym} (active)", "ret_pct": a["net_pct"], "maxdd_pct": a["maxdd_pct"]})

    years = (win_end - win_start).days / 365.25

    print("Fetching SPY...", flush=True)
    spy = fetch_spy(win_start.to_pydatetime(), win_end.to_pydatetime())
    if spy is not None and len(spy) > 50:
        sp_ret, sp_dd = buyhold(spy)
        rows.append({"name": "Buy & hold S&P500 (SPY, ex-div)", "ret_pct": sp_ret, "maxdd_pct": sp_dd})
    else:
        rows.append({"name": "Buy & hold S&P500 (SPY)", "ret_pct": None, "maxdd_pct": None})

    for r in args.hysa_rates:
        total = ((1 + r / 100) ** years - 1) * 100
        rows.append({"name": f"HYSA @ {r:.1f}% APY (risk-free)", "ret_pct": round(total, 2), "maxdd_pct": 0.0})

    print(f"\nWindow: {win_start.date()} -> {win_end.date()}  ({years:.2f} years)\n")
    print(f"  {'instrument':34} {'total return':>13} {'max drawdown':>13}")
    print(f"  {'-' * 34} {'-' * 13} {'-' * 13}")
    for row in rows:
        rp = f"{row['ret_pct']:>+12.2f}%" if row["ret_pct"] is not None else f"{'n/a':>13}"
        dp = f"{row['maxdd_pct']:>+12.2f}%" if row["maxdd_pct"] is not None else f"{'n/a':>13}"
        print(f"  {row['name']:34} {rp} {dp}")

    out = {"generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "window_start": str(win_start.date()), "window_end": str(win_end.date()),
           "years": round(years, 2), "rows": rows}
    with open("backtests/benchmark_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nSaved -> backtests/benchmark_results.json")


if __name__ == "__main__":
    main()
