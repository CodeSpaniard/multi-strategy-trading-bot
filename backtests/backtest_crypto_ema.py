"""EMA-vs-SMA study for the live crypto trend strategy.

Question: does swapping the simple moving averages (live: MA20/100) for
exponential moving averages improve the crypto trend leg? EMAs react faster to
recent price, so they should enter/exit sooner — the open question left by the
earlier crypto studies (trend / exit / regime / meanrev), none of which varied
the *average type*.

Method: reuse the exact live logic from backtest_crypto_trend.simulate
(entry = golden cross, exits = TP/hard-stop/trailing/death-cross, 60bps cost,
daily bars). The ONLY change is ma_kind: 'sma' uses rolling().mean() (the live
control), 'ema' uses ewm(span=N, adjust=False).mean() (standard recursive EMA,
alpha=2/(N+1)). Same symbols, same window, same warmup start so the comparison
is apples-to-apples.

Usage: python3 backtests/backtest_crypto_ema.py [--days 1100]
"""
import argparse
import json
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from coinbase.rest import RESTClient
import pandas as pd

from backtest_crypto_trend import (
    fetch_daily_coinbase, rolling_drawdown, max_drawdown, summarize,
)


def simulate(df, ma_kind="sma", ma_fast=20, ma_slow=100,
             take_profit_pct=40.0, stop_loss_pct=15.0, trailing_stop_pct=12.0,
             cost_bps=60):
    """Identical to backtest_crypto_trend.simulate except ma_kind selects the
    average type. Loop still starts at ma_slow+1 so EMA gets the same warmup
    runway as SMA — fair comparison, no look-ahead difference."""
    close = df["close"].astype(float)
    if ma_kind == "ema":
        ma_f = close.ewm(span=ma_fast, adjust=False).mean()
        ma_s = close.ewm(span=ma_slow, adjust=False).mean()
    else:
        ma_f = close.rolling(ma_fast).mean()
        ma_s = close.rolling(ma_slow).mean()

    trades = []
    position = None
    equity_pct = 0.0
    unrealized_pct = 0.0
    equity_series = []

    for i in range(ma_slow + 1, len(df)):
        t = df.index[i]
        px = float(close.iloc[i])
        fast_now, slow_now = float(ma_f.iloc[i]), float(ma_s.iloc[i])
        fast_prev, slow_prev = float(ma_f.iloc[i - 1]), float(ma_s.iloc[i - 1])

        if position is not None:
            position["hwm"] = max(position["hwm"], px)
            pnl_pct = (px - position["entry_price"]) / position["entry_price"] * 100
            hwm_dd = (px - position["hwm"]) / position["hwm"] * 100
            unrealized_pct = pnl_pct

            exit_reason = None
            if pnl_pct >= take_profit_pct:
                exit_reason = "take-profit"
            elif pnl_pct <= -stop_loss_pct:
                exit_reason = "hard-stop"
            elif hwm_dd <= -trailing_stop_pct:
                exit_reason = "trailing-stop"
            elif fast_now < slow_now and fast_prev >= slow_prev:
                exit_reason = "death-cross"

            if exit_reason:
                net = pnl_pct - cost_bps / 100
                trades.append({
                    "entry_date": position["entry_date"], "exit_date": t,
                    "entry_price": position["entry_price"], "exit_price": px,
                    "gross_pnl": pnl_pct, "net_pnl": net, "reason": exit_reason,
                })
                equity_pct += net
                unrealized_pct = 0.0
                position = None
        else:
            if fast_now > slow_now and fast_prev <= slow_prev:
                position = {"entry_date": t, "entry_price": px, "hwm": px}

        equity_series.append({"date": t, "equity_pct": equity_pct + unrealized_pct})

    if position is not None:
        px = float(close.iloc[-1])
        pnl_pct = (px - position["entry_price"]) / position["entry_price"] * 100
        trades.append({
            "entry_date": position["entry_date"], "exit_date": df.index[-1],
            "entry_price": position["entry_price"], "exit_price": px,
            "gross_pnl": pnl_pct, "net_pnl": pnl_pct - cost_bps / 100, "reason": "eof",
        })

    eq_df = pd.DataFrame(equity_series).set_index("date") if equity_series else pd.DataFrame()
    return trades, eq_df


def r_over_dd(summary):
    dd = summary.get("max_drawdown_pct", 0)
    if not dd:
        return None
    return round(summary["total_net_pnl_pct"] / abs(dd), 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1100)
    parser.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    parser.add_argument("--save", default="backtests/crypto_ema_results.json")
    args = parser.parse_args()

    load_dotenv(".env.coinbase", override=True)
    client = RESTClient(api_key=os.environ["COINBASE_API_KEY"],
                        api_secret=os.environ["COINBASE_API_SECRET"])

    results = {}
    for sym in args.symbols:
        print(f"Fetching {args.days} daily bars for {sym}...", flush=True)
        df = fetch_daily_coinbase(client, sym, args.days)
        print(f"  Got {len(df)} bars: {df.index[0].date()} → {df.index[-1].date()}", flush=True)
        results[sym] = {}
        for kind in ("sma", "ema"):
            trades, eq = simulate(df, ma_kind=kind)
            s = summarize(sym, trades, eq)
            s["return_over_maxdd"] = r_over_dd(s)
            results[sym][kind] = s

        print(f"\n{'=' * 74}")
        print(f"  {sym}   MA20/100 daily crossover   SMA (live) vs EMA")
        print(f"{'=' * 74}")
        hdr = f"  {'metric':<22}{'SMA (live)':>16}{'EMA':>16}"
        print(hdr)
        print("  " + "-" * 52)
        for label, key in (("trades", "trades"), ("win_rate_pct", "win_rate_pct"),
                           ("total_net_pnl_pct", "total_net_pnl_pct"),
                           ("best_trade_pct", "best_trade_pct"),
                           ("worst_trade_pct", "worst_trade_pct"),
                           ("max_drawdown_pct", "max_drawdown_pct"),
                           ("return_over_maxdd", "return_over_maxdd")):
            sma_v = results[sym]["sma"].get(key)
            ema_v = results[sym]["ema"].get(key)
            print(f"  {label:<22}{str(sma_v):>16}{str(ema_v):>16}")

    if args.save:
        out = {
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "study": "EMA vs SMA for MA20/100 crypto trend (all else identical)",
            "params": {"ma_fast": 20, "ma_slow": 100, "take_profit_pct": 40.0,
                       "stop_loss_pct": 15.0, "trailing_stop_pct": 12.0, "cost_bps": 60},
            "lookback_days": args.days,
            "results": results,
        }
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved results → {args.save}")


if __name__ == "__main__":
    main()
