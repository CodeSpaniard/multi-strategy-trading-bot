"""Robustness / perturbation test of the LIVE crypto SMA trend config.

Not a search for a better entry (that research is frozen) — a stress test of the
config we're keeping: SMA 20/100, TP40/SL15/trail12, 60bps. Codex's narrow
exception. Question: does the live config sit on a stable PLATEAU (neighbors
behave similarly → trustworthy) or a lucky SPIKE (small changes collapse it →
fragile)?

Three perturbations, per symbol + aggregate:
  1. Parameter grid — ma_fast in {15,20,25} x ma_slow in {80,100,120}
  2. Cost sensitivity — cost_bps in {30,60,90,120} at live 20/100
  3. Start-date / walk-forward — full, first-half, second-half, last-2yr, last-1yr

Verdict heuristics:
  - plateau if the 8 grid neighbors share the live cell's SIGN and none is a wild outlier
  - cost-robust if aggregate net stays positive across the cost range
  - regime-robust if per-window sign is stable (NOTE: n is tiny per window — a
    sign flip here is expected fragility, not a bug)

Usage: python3 backtests/backtest_crypto_robustness.py [--days 1100]
"""
import argparse
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from coinbase.rest import RESTClient

from backtest_crypto_trend import fetch_daily_coinbase, simulate, max_drawdown


LIVE_FAST, LIVE_SLOW, LIVE_COST = 20, 100, 60


def net_and_dd(df, **kw):
    trades, eq = simulate(df, **kw)
    net = round(sum(t["net_pnl"] for t in trades), 2)
    dd = round(max_drawdown(eq), 2) if not eq.empty else 0.0
    return {"net": net, "trades": len(trades), "maxdd": dd}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1100)
    parser.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    parser.add_argument("--save", default="backtests/crypto_robustness_results.json")
    args = parser.parse_args()

    load_dotenv(".env.coinbase", override=True)
    client = RESTClient(api_key=os.environ["COINBASE_API_KEY"],
                        api_secret=os.environ["COINBASE_API_SECRET"])

    dfs = {}
    for sym in args.symbols:
        print(f"Fetching {args.days} daily bars for {sym}...", flush=True)
        dfs[sym] = fetch_daily_coinbase(client, sym, args.days)
        print(f"  Got {len(dfs[sym])} bars: {dfs[sym].index[0].date()} → {dfs[sym].index[-1].date()}", flush=True)

    fasts, slows = [15, 20, 25], [80, 100, 120]
    costs = [30, 60, 90, 120]
    results = {"grid": {}, "cost": {}, "walkforward": {}}

    # 1. PARAMETER GRID -------------------------------------------------------
    print(f"\n{'=' * 70}\n  1. PARAMETER GRID (net% / trades), SMA, 60bps\n{'=' * 70}")
    for sym in args.symbols:
        results["grid"][sym] = {}
        print(f"\n  {sym}")
        print("           " + "".join(f"slow={s:<10}" for s in slows))
        for f in fasts:
            row = f"  fast={f:<4}"
            for s in slows:
                r = net_and_dd(dfs[sym], ma_fast=f, ma_slow=s, cost_bps=LIVE_COST)
                results["grid"][sym][f"{f}/{s}"] = r
                tag = "*" if (f == LIVE_FAST and s == LIVE_SLOW) else " "
                row += f"{r['net']:+7.1f}/{r['trades']}t{tag}  "
            print(row)
    # aggregate grid (BTC+ETH net)
    print(f"\n  AGGREGATE net (BTC+ETH), * = live cell")
    print("           " + "".join(f"slow={s:<8}" for s in slows))
    agg_grid = {}
    for f in fasts:
        row = f"  fast={f:<4}"
        for s in slows:
            agg = sum(results["grid"][sym][f"{f}/{s}"]["net"] for sym in args.symbols)
            agg_grid[f"{f}/{s}"] = round(agg, 1)
            tag = "*" if (f == LIVE_FAST and s == LIVE_SLOW) else " "
            row += f"{agg:+8.1f}{tag} "
        print(row)
    results["aggregate_grid"] = agg_grid

    # 2. COST SENSITIVITY -----------------------------------------------------
    print(f"\n{'=' * 70}\n  2. COST SENSITIVITY at live {LIVE_FAST}/{LIVE_SLOW} (aggregate net%)\n{'=' * 70}")
    for c in costs:
        agg = 0.0
        per = {}
        for sym in args.symbols:
            r = net_and_dd(dfs[sym], ma_fast=LIVE_FAST, ma_slow=LIVE_SLOW, cost_bps=c)
            per[sym] = r
            agg += r["net"]
        results["cost"][str(c)] = {"aggregate_net": round(agg, 1), "per_symbol": per}
        live = " (live)" if c == LIVE_COST else ""
        print(f"  cost={c:>4}bps   aggregate net {agg:+8.1f}%   "
              f"BTC {per[args.symbols[0]]['net']:+.1f}  ETH {per[args.symbols[1]]['net']:+.1f}{live}")

    # 3. WALK-FORWARD / START-DATE --------------------------------------------
    print(f"\n{'=' * 70}\n  3. WALK-FORWARD WINDOWS at live {LIVE_FAST}/{LIVE_SLOW} (net% / trades)\n{'=' * 70}")
    for sym in args.symbols:
        df = dfs[sym]
        n = len(df)
        windows = {
            "full": df,
            "first_half": df.iloc[: n // 2],
            "second_half": df.iloc[n // 2:],
            "last_2yr": df.iloc[-730:],
            "last_1yr": df.iloc[-365:],
        }
        results["walkforward"][sym] = {}
        print(f"\n  {sym}")
        for name, wdf in windows.items():
            if len(wdf) < LIVE_SLOW + 5:
                results["walkforward"][sym][name] = {"net": None, "trades": 0, "note": "too short"}
                print(f"    {name:<13} (too short for {LIVE_SLOW}-bar MA)")
                continue
            r = net_and_dd(wdf, ma_fast=LIVE_FAST, ma_slow=LIVE_SLOW, cost_bps=LIVE_COST)
            results["walkforward"][sym][name] = r
            print(f"    {name:<13} {r['net']:+8.1f}%   {r['trades']} trades   maxDD {r['maxdd']:+.1f}%")

    if args.save:
        out = {
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "study": "Robustness/perturbation test of live crypto SMA 20/100 config",
            "live_config": {"ma_fast": LIVE_FAST, "ma_slow": LIVE_SLOW, "cost_bps": LIVE_COST,
                            "take_profit_pct": 40, "stop_loss_pct": 15, "trailing_stop_pct": 12},
            "lookback_days": args.days,
            "results": results,
        }
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved results → {args.save}")


if __name__ == "__main__":
    main()
