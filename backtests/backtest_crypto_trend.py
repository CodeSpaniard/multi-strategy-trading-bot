"""Backtest the live crypto trend strategy: MA20/100 daily crossover on BTC/ETH.

Mirrors src/trend_strategy.py logic exactly:
  - Entry: fast MA crosses above slow MA (golden cross)
  - Exits (first hit wins): take-profit, hard stop, trailing stop from hwm, death cross
  - Daily granularity — one decision per day on the daily close

Reports: total return, trade count, max drawdown (peak-to-trough equity curve),
worst rolling 30/60/90-day drawdown, worst single trade, per-symbol breakdown.

Usage: python3 backtest_crypto_trend.py [--days 1100]
"""
import argparse
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from coinbase.rest import RESTClient
import pandas as pd


def fetch_daily_coinbase(client, product_id, days):
    """Coinbase candles: granularity ONE_DAY, window max 350 bars per call. Chunk."""
    end = datetime.utcnow()
    start = end - timedelta(days=days + 5)
    bars = []
    chunk_end = end
    while chunk_end > start:
        chunk_start = max(start, chunk_end - timedelta(days=300))
        candles = client.get_candles(
            product_id=product_id,
            start=str(int(chunk_start.timestamp())),
            end=str(int(chunk_end.timestamp())),
            granularity="ONE_DAY",
        )
        clist = candles.candles if hasattr(candles, 'candles') else candles['candles']
        for c in clist:
            if isinstance(c, dict):
                bars.append({"timestamp": datetime.utcfromtimestamp(int(c['start'])),
                             "open": float(c['open']), "high": float(c['high']),
                             "low": float(c['low']), "close": float(c['close']),
                             "volume": float(c['volume'])})
            else:
                bars.append({"timestamp": datetime.utcfromtimestamp(int(c.start)),
                             "open": float(c.open), "high": float(c.high),
                             "low": float(c.low), "close": float(c.close),
                             "volume": float(c.volume)})
        chunk_end = chunk_start
    df = pd.DataFrame(bars).drop_duplicates(subset=["timestamp"]).set_index("timestamp").sort_index()
    return df.tail(days)


def simulate(df, ma_fast=20, ma_slow=100,
             take_profit_pct=40.0, stop_loss_pct=15.0, trailing_stop_pct=12.0,
             cost_bps=60):
    """Returns (trades_list, equity_curve_series) where equity_curve tracks
    cumulative net return % of a unit-notional bet, marked daily at close.

    Exit priority matches live: TP → hard stop → trailing → death cross.
    """
    close = df["close"].astype(float)
    ma_f = close.rolling(ma_fast).mean()
    ma_s = close.rolling(ma_slow).mean()

    trades = []
    position = None  # dict(entry_date, entry_price, hwm)
    equity_pct = 0.0  # cumulative closed-trade return, in percent
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


def rolling_drawdown(equity_series, window_days):
    """Worst drawdown over any window of window_days in the equity curve."""
    if equity_series.empty:
        return 0.0
    eq = equity_series["equity_pct"]
    worst = 0.0
    for i in range(len(eq)):
        end = eq.index[i]
        start = end - timedelta(days=window_days)
        window = eq[(eq.index >= start) & (eq.index <= end)]
        if len(window) < 2:
            continue
        peak = window.cummax()
        dd = (window - peak).min()
        worst = min(worst, dd)
    return worst


def max_drawdown(equity_series):
    if equity_series.empty:
        return 0.0
    eq = equity_series["equity_pct"]
    peak = eq.cummax()
    return (eq - peak).min()


def report(symbol, trades, eq_series):
    print(f"\n{'=' * 70}")
    print(f"  {symbol}  MA20/100 daily crossover  (TP=40%, SL=15%, trail=12%, 60bps)")
    print(f"{'=' * 70}")
    if not trades:
        print("  No trades.")
        return
    net = [t["net_pnl"] for t in trades]
    wins = [p for p in net if p > 0]
    losses = [p for p in net if p <= 0]
    print(f"  Period:           {eq_series.index[0].date()} → {eq_series.index[-1].date()}")
    print(f"  Trades:           {len(trades)}")
    print(f"  Win / Loss:       {len(wins)} / {len(losses)}  (win rate: {len(wins) / len(trades) * 100:.1f}%)")
    print(f"  Total net P&L:    {sum(net):+.2f}%")
    print(f"  Best trade:       {max(net):+.2f}%")
    print(f"  Worst trade:      {min(net):+.2f}%")
    print(f"  Max drawdown:     {max_drawdown(eq_series):+.2f}% (equity curve, any duration)")
    print(f"  Worst 30-day DD:  {rolling_drawdown(eq_series, 30):+.2f}%")
    print(f"  Worst 60-day DD:  {rolling_drawdown(eq_series, 60):+.2f}%")
    print(f"  Worst 90-day DD:  {rolling_drawdown(eq_series, 90):+.2f}%")
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print(f"  Exit reasons:     {reasons}")
    print(f"\n  Trade log:")
    for t in trades:
        print(f"    {t['entry_date'].date()} → {t['exit_date'].date()}  "
              f"entry ${t['entry_price']:.2f} exit ${t['exit_price']:.2f}  "
              f"net {t['net_pnl']:+.2f}% ({t['reason']})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1100, help="lookback in days (~3yr default)")
    parser.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    args = parser.parse_args()

    load_dotenv(".env.coinbase", override=True)
    client = RESTClient(api_key=os.environ["COINBASE_API_KEY"],
                        api_secret=os.environ["COINBASE_API_SECRET"])

    for sym in args.symbols:
        print(f"Fetching {args.days} daily bars for {sym}...", flush=True)
        df = fetch_daily_coinbase(client, sym, args.days)
        print(f"  Got {len(df)} bars from {df.index[0].date()} to {df.index[-1].date()}", flush=True)
        trades, eq = simulate(df)
        report(sym, trades, eq)


if __name__ == "__main__":
    main()
