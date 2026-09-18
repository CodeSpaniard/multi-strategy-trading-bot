"""Thesis check: does a 200-day MA regime filter on ENTRY improve the live
crypto trend strategy by cutting chop/top whipsaws without killing the winners?

Baseline = src/trend_strategy.py logic (golden cross 20/100, TP40/SL15/trail12).
Filtered = same, but only ENTER if price > 200-day MA (confirmed uptrend regime).

Diagnostic only — results saved to JSON, nothing touches the live bot.
Usage: python3 backtest_crypto_trend_regime.py [--days 1100]
"""
import argparse
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from coinbase.rest import RESTClient

from backtest_crypto_trend import (
    fetch_daily_coinbase, max_drawdown, rolling_drawdown,
)


def simulate(df, regime_ma=None, ma_fast=20, ma_slow=100,
             take_profit_pct=40.0, stop_loss_pct=15.0, trailing_stop_pct=12.0,
             cost_bps=60):
    """Same exit logic as live. If regime_ma is set, only enter when
    price > rolling(regime_ma) mean (and that MA is defined)."""
    close = df["close"].astype(float)
    ma_f = close.rolling(ma_fast).mean()
    ma_s = close.rolling(ma_slow).mean()
    ma_r = close.rolling(regime_ma).mean() if regime_ma else None

    trades, position = [], None
    equity_pct, unrealized_pct, equity_series = 0.0, 0.0, []
    skipped = 0  # crosses vetoed by the regime filter

    start_i = max(ma_slow, regime_ma or 0) + 1
    for i in range(start_i, len(df)):
        t = df.index[i]
        px = float(close.iloc[i])
        fast_now, slow_now = float(ma_f.iloc[i]), float(ma_s.iloc[i])
        fast_prev, slow_prev = float(ma_f.iloc[i - 1]), float(ma_s.iloc[i - 1])

        if position is not None:
            position["hwm"] = max(position["hwm"], px)
            pnl_pct = (px - position["entry_price"]) / position["entry_price"] * 100
            hwm_dd = (px - position["hwm"]) / position["hwm"] * 100
            unrealized_pct = pnl_pct
            reason = None
            if pnl_pct >= take_profit_pct:
                reason = "take-profit"
            elif pnl_pct <= -stop_loss_pct:
                reason = "hard-stop"
            elif hwm_dd <= -trailing_stop_pct:
                reason = "trailing-stop"
            elif fast_now < slow_now and fast_prev >= slow_prev:
                reason = "death-cross"
            if reason:
                net = pnl_pct - cost_bps / 100
                trades.append({"entry_date": position["entry_date"], "exit_date": t,
                               "entry_price": position["entry_price"], "exit_price": px,
                               "net_pnl": net, "reason": reason})
                equity_pct += net
                unrealized_pct, position = 0.0, None
        else:
            golden = fast_now > slow_now and fast_prev <= slow_prev
            if golden:
                if regime_ma is None:
                    position = {"entry_date": t, "entry_price": px, "hwm": px}
                else:
                    regime_ok = ma_r.iloc[i] == ma_r.iloc[i] and px > float(ma_r.iloc[i])
                    if regime_ok:
                        position = {"entry_date": t, "entry_price": px, "hwm": px}
                    else:
                        skipped += 1
        equity_series.append({"date": t, "equity_pct": equity_pct + unrealized_pct})

    import pandas as pd
    eq_df = pd.DataFrame(equity_series).set_index("date") if equity_series else pd.DataFrame()
    return trades, eq_df, skipped


def summarize(trades, eq, skipped):
    if not trades:
        return {"trades": 0, "skipped_by_filter": skipped, "total_net_pnl_pct": 0.0}
    net = [t["net_pnl"] for t in trades]
    wins = [p for p in net if p > 0]
    tp = sum(1 for t in trades if t["reason"] == "take-profit")
    return {
        "trades": len(trades),
        "skipped_by_filter": skipped,
        "wins": len(wins), "losses": len(trades) - len(wins),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "take_profit_wins": tp,
        "total_net_pnl_pct": round(sum(net), 2),
        "best_trade_pct": round(max(net), 2),
        "worst_trade_pct": round(min(net), 2),
        "max_drawdown_pct": round(max_drawdown(eq), 2),
        "trade_log": [{"entry": str(t["entry_date"].date()), "exit": str(t["exit_date"].date()),
                       "net_pnl_pct": round(t["net_pnl"], 2), "reason": t["reason"]} for t in trades],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    ap.add_argument("--regime-ma", type=int, default=200)
    ap.add_argument("--save", default="backtests/crypto_trend_regime_results.json")
    args = ap.parse_args()

    load_dotenv(".env.coinbase", override=True)
    client = RESTClient(api_key=os.environ["COINBASE_API_KEY"],
                        api_secret=os.environ["COINBASE_API_SECRET"])

    out = {"generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "regime_ma": args.regime_ma, "lookback_days": args.days, "results": {}}

    for sym in args.symbols:
        print(f"\nFetching {sym}...", flush=True)
        df = fetch_daily_coinbase(client, sym, args.days)
        base_t, base_eq, _ = simulate(df, regime_ma=None)
        filt_t, filt_eq, skipped = simulate(df, regime_ma=args.regime_ma)
        base, filt = summarize(base_t, base_eq, 0), summarize(filt_t, filt_eq, skipped)
        out["results"][sym] = {"baseline": base, "filtered": filt}

        print(f"{'=' * 64}\n  {sym}  baseline  vs  +{args.regime_ma}MA regime filter\n{'=' * 64}")
        for label, s in [("BASELINE", base), ("FILTERED", filt)]:
            print(f"  {label:9} trades={s['trades']:<2} win%={s.get('win_rate_pct',0):<5} "
                  f"TPwins={s.get('take_profit_wins',0)} netP&L={s['total_net_pnl_pct']:+.2f}% "
                  f"maxDD={s.get('max_drawdown_pct',0):+.2f}% skipped={s['skipped_by_filter']}")
        # which trades the filter removed
        base_keys = {(t["entry_date"].date(), t["net_pnl"]) for t in base_t}
        filt_keys = {(t["entry_date"].date(), t["net_pnl"]) for t in filt_t}
        removed = [t for t in base_t if (t["entry_date"].date(), t["net_pnl"]) not in filt_keys]
        if removed:
            print("  Removed by filter:")
            for t in removed:
                print(f"    {t['entry_date'].date()}  net {t['net_pnl']:+.2f}% ({t['reason']})")

    if args.save:
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved → {args.save}")


if __name__ == "__main__":
    main()
