"""Crypto DAILY mean-reversion sleeve — feasibility test (regime-diversification idea).

Premise: the live trend follower wins in trends and whipsaws in chop. A daily
buy-the-dip mean-reversion strategy might earn during exactly those choppy stretches,
making the two complementary. The prior shelved crypto mean-rev was HOURLY (killed by
cost drag); this is DAILY (low-turnover, cost-survivable) on the SAME Coinbase data as
the trend backtest, so results are comparable.

Strategy (fixed sensible params; only the entry dip threshold is swept, to avoid
curve-fitting): enter at the close when the 1-day return <= -dip_thr; exit on the
first of bounce-target / hard-stop / trailing-stop / time-stop.

Judge STANDALONE edge first (is it even positive after 60bps cost?). Complementarity
vs the trend strategy is a separate step, only worth running if this clears the bar.
Usage: python3 backtest_crypto_meanrev.py [--days 1100]
"""
import argparse
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from coinbase.rest import RESTClient

from backtest_crypto_trend import fetch_daily_coinbase

# Fixed, un-optimized params (avoid overfitting; only dip-threshold is swept).
BOUNCE_TARGET = 8.0
HARD_STOP = 15.0
TRAILING = 10.0
MAX_HOLD_DAYS = 14
COST_BPS = 60


def simulate(df, dip_thr, ma_filter=None):
    """ma_filter: if set (e.g. 200), only ENTER when price > that-day MA (uptrend only)."""
    close = df["close"].astype(float)
    ret = close.pct_change() * 100
    ma = close.rolling(ma_filter).mean() if ma_filter else None
    start_i = (ma_filter + 1) if ma_filter else 1
    trades, pos, eq, series = [], None, 0.0, []
    for i in range(start_i, len(df)):
        t = df.index[i]
        px = float(close.iloc[i])
        unreal = 0.0
        if pos is not None:
            pos["hwm"] = max(pos["hwm"], px)
            pnl = (px - pos["entry"]) / pos["entry"] * 100
            dd = (px - pos["hwm"]) / pos["hwm"] * 100
            held = (t - pos["d"]).days
            unreal = pnl
            reason = None
            if pnl >= BOUNCE_TARGET:
                reason = "bounce-target"
            elif pnl <= -HARD_STOP:
                reason = "hard-stop"
            elif dd <= -TRAILING:
                reason = "trailing-stop"
            elif held >= MAX_HOLD_DAYS:
                reason = "time-stop"
            if reason:
                net = pnl - COST_BPS / 100
                trades.append({"d": pos["d"], "exit_d": t, "net": net, "reason": reason})
                eq += net
                unreal = 0.0
                pos = None
        elif float(ret.iloc[i]) <= -dip_thr:
            uptrend_ok = ma is None or (ma.iloc[i] == ma.iloc[i] and px > float(ma.iloc[i]))
            if uptrend_ok:
                pos = {"d": t, "entry": px, "hwm": px}
        series.append({"date": t, "eq": eq + unreal})
    if pos is not None:
        px = float(close.iloc[-1])
        pnl = (px - pos["entry"]) / pos["entry"] * 100
        trades.append({"d": pos["d"], "exit_d": df.index[-1], "net": pnl - COST_BPS / 100, "reason": "eof"})
    import pandas as pd
    eqs = pd.DataFrame(series).set_index("date") if series else pd.DataFrame()
    return trades, eqs


def maxdd(eqs):
    if eqs.empty:
        return 0.0
    e = eqs["eq"]
    return float((e - e.cummax()).min())


def summarize(trades, eqs):
    if not trades:
        return {"trades": 0}
    net = [t["net"] for t in trades]
    wins = [x for x in net if x > 0]
    total = sum(net)
    dd = maxdd(eqs)
    holds = [(t["exit_d"] - t["d"]).days for t in trades]
    return {"trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
            "total_net_pnl_pct": round(total, 2), "avg_per_trade_pct": round(total / len(trades), 3),
            "best_pct": round(max(net), 2), "worst_pct": round(min(net), 2),
            "max_drawdown_pct": round(dd, 2), "avg_hold_days": round(sum(holds) / len(holds), 1),
            "return_over_maxdd": round(total / abs(dd), 2) if dd else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    ap.add_argument("--dips", nargs="+", type=float, default=[4.0, 6.0, 8.0, 10.0])
    ap.add_argument("--save", default="backtests/crypto_meanrev_results.json")
    args = ap.parse_args()

    load_dotenv(".env.coinbase", override=True)
    cb = RESTClient(api_key=os.environ["COINBASE_API_KEY"], api_secret=os.environ["COINBASE_API_SECRET"])
    out = {"generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "params": {"bounce_target": BOUNCE_TARGET, "hard_stop": HARD_STOP, "trailing": TRAILING,
                      "max_hold_days": MAX_HOLD_DAYS, "cost_bps": COST_BPS}, "results": {}}

    for sym in args.symbols:
        print(f"\nFetching {sym}...", flush=True)
        df = fetch_daily_coinbase(cb, sym, args.days)
        out["results"][sym] = {}
        print(f"{'=' * 78}\n  {sym}  daily mean-reversion  ({df.index[0].date()}->{df.index[-1].date()})")
        print(f"  params: buy 1-day drop>=X%; exit +{BOUNCE_TARGET}%/-{HARD_STOP}%/trail{TRAILING}%/{MAX_HOLD_DAYS}d; {COST_BPS}bps")
        print(f"{'=' * 78}")
        print(f"  {'dip>=':>6} {'filter':>8} {'trades':>6} {'win%':>6} {'netP&L':>9} {'avg/trade':>10} {'maxDD':>9} {'ret/DD':>7}")
        for dip in args.dips:
            for ma in [None, 200]:
                tr, eqs = simulate(df, dip, ma_filter=ma)
                s = summarize(tr, eqs)
                out["results"][sym][f"dip{dip}_ma{ma}"] = s
                label = "none" if ma is None else f">{ma}MA"
                print(f"  {dip:>5}% {label:>8} {s.get('trades', 0):>6} {s.get('win_rate_pct', 0):>6} "
                      f"{s.get('total_net_pnl_pct', 0):>+8.2f}% {s.get('avg_per_trade_pct', 0):>+9.3f}% "
                      f"{s.get('max_drawdown_pct', 0):>+8.2f}% {str(s.get('return_over_maxdd')):>7}")

    with open(args.save, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved -> {args.save}")


if __name__ == "__main__":
    main()
