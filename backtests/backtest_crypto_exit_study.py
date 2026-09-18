"""Exit-rule optimality study for the crypto trend strategy (PRE-REGISTERED 2026-04-23).

Question: is the live +40% take-profit leaving money on the table in strong trends?
Backtest the live baseline against the three alternatives flagged in the roadmap:
  baseline   : TP +40%, SL 15%, trail 12%, death cross   (LIVE rule)
  trail_only : NO take-profit — SL 15%, trail 12%, death  (pure trend-following)
  tp80       : TP +80%, else same
  tp100      : TP +100%, else same
  scaleout   : sell 50% at +40%, let the other 50% ride on trail/SL/death

Daily bars, BTC + ETH, 60 bps cost per realized leg. Diagnostic only; saves JSON.
Exit priority matches live: TP -> hard stop -> trailing -> death cross.
Usage: python3 backtest_crypto_exit_study.py [--days 1100]
"""
import argparse
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from coinbase.rest import RESTClient
import pandas as pd

from backtest_crypto_trend import fetch_daily_coinbase

TP_BY_MODE = {"baseline": 40.0, "trail_only": None, "tp80": 80.0, "tp100": 100.0, "scaleout": None}


def simulate(df, mode, ma_fast=20, ma_slow=100, sl=15.0, trail=12.0, cost_bps=60):
    close = df["close"].astype(float)
    ma_f = close.rolling(ma_fast).mean()
    ma_s = close.rolling(ma_slow).mean()
    tp = TP_BY_MODE[mode]
    scaleout = mode == "scaleout"
    c = cost_bps / 100.0

    trades, pos, eq, series = [], None, 0.0, []
    for i in range(ma_slow + 1, len(df)):
        t = df.index[i]
        px = float(close.iloc[i])
        fn, sn = float(ma_f.iloc[i]), float(ma_s.iloc[i])
        fp, sp = float(ma_f.iloc[i - 1]), float(ma_s.iloc[i - 1])
        unreal = 0.0

        if pos is not None:
            pos["hwm"] = max(pos["hwm"], px)
            pnl = (px - pos["entry"]) / pos["entry"] * 100
            dd = (px - pos["hwm"]) / pos["hwm"] * 100

            # scale-out: book half at +40% the first time it's hit, then let the rest ride
            if scaleout and not pos["scaled"] and pnl >= 40.0:
                eq += 0.5 * (40.0 - c)
                pos["scaled"] = True

            reason = None
            if tp is not None and pnl >= tp:
                reason = "take-profit"
            elif pnl <= -sl:
                reason = "hard-stop"
            elif dd <= -trail:
                reason = "trailing-stop"
            elif fn < sn and fp >= sp:
                reason = "death-cross"

            if reason:
                if scaleout and pos["scaled"]:
                    remaining = 0.5 * (pnl - c)
                    trade_net = 0.5 * (40.0 - c) + remaining
                    eq += remaining
                else:
                    trade_net = pnl - c
                    eq += trade_net
                trades.append({"entry_date": pos["d"], "exit_date": t, "entry": pos["entry"],
                               "exit": px, "net_pnl": trade_net, "gross_pnl": pnl, "reason": reason})
                pos = None
            else:
                unreal = 0.5 * pnl if (scaleout and pos["scaled"]) else pnl
        else:
            if fn > sn and fp <= sp:
                pos = {"d": t, "entry": px, "hwm": px, "scaled": False}

        series.append({"date": t, "eq": eq + unreal})

    if pos is not None:  # mark-to-market close at end of data
        px = float(close.iloc[-1])
        pnl = (px - pos["entry"]) / pos["entry"] * 100
        if scaleout and pos["scaled"]:
            trade_net = 0.5 * (40.0 - c) + 0.5 * (pnl - c)
        else:
            trade_net = pnl - c
        trades.append({"entry_date": pos["d"], "exit_date": df.index[-1], "entry": pos["entry"],
                       "exit": px, "net_pnl": trade_net, "gross_pnl": pnl, "reason": "eof"})

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
    net = [t["net_pnl"] for t in trades]
    wins = [x for x in net if x > 0]
    total = sum(net)
    dd = maxdd(eqs)
    return {"trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
            "total_net_pnl_pct": round(total, 2), "best_trade_pct": round(max(net), 2),
            "worst_trade_pct": round(min(net), 2), "max_drawdown_pct": round(dd, 2),
            "return_over_maxdd": round(total / abs(dd), 2) if dd else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    ap.add_argument("--save", default="backtests/crypto_exit_study_results.json")
    args = ap.parse_args()

    load_dotenv(".env.coinbase", override=True)
    client = RESTClient(api_key=os.environ["COINBASE_API_KEY"], api_secret=os.environ["COINBASE_API_SECRET"])
    modes = ["baseline", "trail_only", "tp80", "tp100", "scaleout"]
    out = {"generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z", "results": {}}

    for sym in args.symbols:
        print(f"\nFetching {sym}...", flush=True)
        df = fetch_daily_coinbase(client, sym, args.days)
        out["results"][sym] = {}
        print(f"{'=' * 80}\n  {sym}  exit-rule study  ({df.index[0].date()} -> {df.index[-1].date()})\n{'=' * 80}")
        print(f"  {'mode':11} {'trades':>6} {'win%':>6} {'netP&L':>10} {'best':>9} {'maxDD':>9} {'ret/DD':>7}")
        for m in modes:
            tr, eqs = simulate(df, m)
            s = summarize(tr, eqs)
            out["results"][sym][m] = s
            print(f"  {m:11} {s['trades']:>6} {s.get('win_rate_pct', 0):>6} "
                  f"{s.get('total_net_pnl_pct', 0):>+9.2f}% {s.get('best_trade_pct', 0):>+8.2f}% "
                  f"{s.get('max_drawdown_pct', 0):>+8.2f}% {str(s.get('return_over_maxdd')):>7}")

    if args.save:
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved -> {args.save}")


if __name__ == "__main__":
    main()
