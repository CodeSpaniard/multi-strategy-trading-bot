"""Weekend-entry study for the equity dip scanner.

Compares, over the same window and params, three ENTRY-DAY policies:
  baseline      : enter on dips ANY weekday          (current live behavior)
  weekend-only  : enter ONLY on Fridays              (the trades carried over the weekend)
  skip-weekend  : enter Mon-Thu only                 (never enter just before the weekend)

Gates the ENTRY DAY. Note: skip-weekend still lets a Mon-Thu position drift across a
weekend if its target isn't hit — it only avoids *deliberately* entering into one.
The comparable metrics across variants are WIN RATE and AVG/TRADE (total P&L scales
with trade count, so it is not directly comparable). Diagnostic only; saves JSON.
Usage: python3 backtest_scanner_weekend.py
"""
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient

from backtest_scanner import UNIVERSE, fetch_daily, run_backtest, summarize


def stats(trades):
    if not trades:
        return {"trades": 0}
    net = [t["net_pnl"] for t in trades]
    wins = [p for p in net if p > 0]
    cum = peak = mdd = 0.0
    for p in net:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    holds = []
    for t in trades:
        try:
            d = t["exit_date"] - t["entry_date"]
            holds.append(d.days if hasattr(d, "days") else 1)
        except Exception:
            holds.append(1)
    total = sum(net)
    return {"trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
            "total_net_pnl_pct": round(total, 2), "avg_per_trade_pct": round(total / len(trades), 3),
            "best_pct": round(max(net), 2), "worst_pct": round(min(net), 2),
            "max_drawdown_pct": round(mdd, 2), "avg_hold_days": round(sum(holds) / len(holds), 1),
            "return_over_maxdd": round(total / abs(mdd), 2) if mdd else None}


def main():
    START, END = datetime(2024, 1, 1), datetime(2026, 6, 9)
    DIP, BOUNCE, MINB = 3.0, 0.40, 1.0

    load_dotenv(".env.paper", override=True)
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    print(f"Fetching {len(UNIVERSE)} stocks {START.date()}..{END.date()} (once)...", flush=True)
    data = {}
    for sym in UNIVERSE:
        df = fetch_daily(client, sym, START, END)
        if df is not None and len(df) > 20:
            data[sym] = df
    print(f"  coverage {len(data)}/{len(UNIVERSE)}")
    if len(data) < 25:
        raise RuntimeError(f"coverage too low: {len(data)}/{len(UNIVERSE)}")

    variants = [("baseline (all days)", None), ("weekend-only (Fri entries)", {4}),
                ("skip-weekend (Mon-Thu)", {0, 1, 2, 3})]
    out = {"generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "window": f"{START.date()}..{END.date()}", "params": {"dip": DIP, "bounce": BOUNCE, "min_bounce": MINB},
           "results": {}}

    for label, wd in variants:
        print(f"\n{'=' * 72}\n  {label}\n{'=' * 72}")
        trades = run_backtest(start=START, end=END, dip_threshold_pct=DIP, bounce_ratio=BOUNCE,
                              min_bounce_pct=MINB, entry_weekdays=wd, preloaded_data=data)
        summarize(trades)
        out["results"][label] = stats(trades)

    print(f"\n{'=' * 72}\n  COMPARISON  (win% and avg/trade are the comparable metrics)\n{'=' * 72}")
    print(f"  {'variant':28} {'trades':>6} {'win%':>6} {'netP&L':>9} {'avg/trade':>10} {'maxDD':>9} {'ret/DD':>7}")
    for label, _ in variants:
        s = out["results"][label]
        print(f"  {label:28} {s['trades']:>6} {s.get('win_rate_pct', 0):>6} "
              f"{s.get('total_net_pnl_pct', 0):>+8.2f}% {s.get('avg_per_trade_pct', 0):>+9.3f}% "
              f"{s.get('max_drawdown_pct', 0):>+8.2f}% {str(s.get('return_over_maxdd')):>7}")

    with open("backtests/scanner_weekend_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nSaved -> backtests/scanner_weekend_results.json")


if __name__ == "__main__":
    main()
