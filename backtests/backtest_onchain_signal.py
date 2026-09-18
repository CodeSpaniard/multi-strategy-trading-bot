"""LEARNING EXERCISE (not live research): does free on-chain data carry signal?

Crypto-entry research is FROZEN (see project memory) — this is decoupled from the
live bot. Purpose is to practice the alternative-data workflow end-to-end:
  1. source a FREE on-chain dataset (blockchain.info, no API key)
  2. join it to price on dates
  3. test honestly whether it has predictive signal — with the caveats stated

Hypothesis: BTC network activity (daily active addresses) leads price. Define an
on-chain "expansion" regime as a fast/slow MA cross on active addresses
(7d MA > 30d MA), the same crossover idea learned for price. Then ask: are
forward 30-day BTC returns higher in the expansion regime than the contraction
regime?

HONEST CAVEATS (the point of the exercise is to state these):
  - forward windows overlap → autocorrelation inflates apparent significance
  - one asset, one in-sample window → not out-of-sample validated
  - correlation, not causation; activity and price may share a common driver
  - this is a teaching demo, NOT evidence to trade on

Usage: python3 backtests/backtest_onchain_signal.py
"""
import argparse
import json
import os
import urllib.request
from datetime import datetime
from dotenv import load_dotenv
from coinbase.rest import RESTClient
import pandas as pd

from backtest_crypto_trend import fetch_daily_coinbase, simulate


def fetch_blockchain_chart(chart, timespan="3years"):
    url = f"https://api.blockchain.info/charts/{chart}?timespan={timespan}&format=json&sampled=false"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (onchain-learning-study)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode())
    s = pd.Series(
        {datetime.utcfromtimestamp(p["x"]).date(): float(p["y"]) for p in data["values"]}
    )
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", type=int, default=30, help="forward-return horizon (days)")
    parser.add_argument("--fast", type=int, default=7)
    parser.add_argument("--slow", type=int, default=30)
    parser.add_argument("--save", default="backtests/onchain_signal_results.json")
    args = parser.parse_args()

    load_dotenv(".env.coinbase", override=True)
    client = RESTClient(api_key=os.environ["COINBASE_API_KEY"],
                        api_secret=os.environ["COINBASE_API_SECRET"])

    print("Fetching BTC spot (Coinbase) + active addresses (blockchain.info)...", flush=True)
    price = fetch_daily_coinbase(client, "BTC-USD", 1100)["close"].astype(float)
    price.index = pd.to_datetime([d.date() for d in price.index])
    addr = fetch_blockchain_chart("n-unique-addresses")

    # align on common dates
    df = pd.DataFrame({"price": price, "addr": addr}).dropna()
    print(f"  Aligned {len(df)} days: {df.index[0].date()} → {df.index[-1].date()}", flush=True)

    # on-chain regime: 7d MA of active addresses above 30d MA = expansion
    df["addr_fast"] = df["addr"].rolling(args.fast).mean()
    df["addr_slow"] = df["addr"].rolling(args.slow).mean()
    df["expansion"] = df["addr_fast"] > df["addr_slow"]

    # forward H-day price return
    H = args.horizon
    df["fwd_ret"] = df["price"].shift(-H) / df["price"] - 1.0
    test = df.dropna(subset=["addr_slow", "fwd_ret"])

    exp = test[test["expansion"]]["fwd_ret"]
    con = test[~test["expansion"]]["fwd_ret"]

    def stats(x):
        return {
            "days": int(len(x)),
            "mean_fwd_ret_pct": round(x.mean() * 100, 2),
            "median_fwd_ret_pct": round(x.median() * 100, 2),
            "pct_positive": round((x > 0).mean() * 100, 1),
        }

    s_exp, s_con = stats(exp), stats(con)
    spread = round(s_exp["mean_fwd_ret_pct"] - s_con["mean_fwd_ret_pct"], 2)

    print(f"\n{'=' * 66}")
    print(f"  BTC on-chain regime vs forward {H}-day return")
    print(f"  regime = active-addr {args.fast}d MA {'>' if True else ''} {args.slow}d MA")
    print(f"{'=' * 66}")
    hdr = f"  {'regime':<16}{'days':>7}{'mean fwd%':>12}{'median%':>10}{'% positive':>12}"
    print(hdr + "\n  " + "-" * 55)
    print(f"  {'expansion':<16}{s_exp['days']:>7}{s_exp['mean_fwd_ret_pct']:>12}{s_exp['median_fwd_ret_pct']:>10}{s_exp['pct_positive']:>12}")
    print(f"  {'contraction':<16}{s_con['days']:>7}{s_con['mean_fwd_ret_pct']:>12}{s_con['median_fwd_ret_pct']:>10}{s_con['pct_positive']:>12}")
    print(f"\n  SPREAD (expansion - contraction) mean fwd return: {spread:+.2f}%")

    # illustration: tag the live SMA golden-cross entries by on-chain regime
    trades, _ = simulate(fetch_daily_coinbase(client, "BTC-USD", 1100))
    tagged = []
    for t in trades:
        d = pd.Timestamp(t["entry_date"].date())
        regime = None
        if d in df.index and not pd.isna(df.loc[d, "addr_slow"]):
            regime = "expansion" if bool(df.loc[d, "expansion"]) else "contraction"
        tagged.append({"entry": str(d.date()), "net_pnl_pct": round(t["net_pnl"], 2),
                       "reason": t["reason"], "onchain_regime": regime})
    print(f"\n  Illustration — BTC golden-cross trades tagged by on-chain regime at entry:")
    for t in tagged:
        print(f"    {t['entry']}  {t['net_pnl_pct']:+7.2f}%  {t['reason']:<14} onchain={t['onchain_regime']}")
    conf = [t for t in tagged if t["onchain_regime"] == "expansion"]
    noconf = [t for t in tagged if t["onchain_regime"] == "contraction"]
    if conf:
        print(f"    confirmed (expansion) avg: {sum(t['net_pnl_pct'] for t in conf)/len(conf):+.2f}% over {len(conf)} trades")
    if noconf:
        print(f"    unconfirmed (contraction) avg: {sum(t['net_pnl_pct'] for t in noconf)/len(noconf):+.2f}% over {len(noconf)} trades")

    if args.save:
        out = {
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "study": "LEARNING: on-chain active-address regime vs forward BTC return",
            "data_source": "Coinbase spot + blockchain.info n-unique-addresses (free)",
            "horizon_days": H, "regime_def": f"active-addr {args.fast}d MA > {args.slow}d MA",
            "caveats": ["overlapping forward windows inflate significance", "one asset / in-sample",
                        "correlation not causation", "teaching demo, not tradeable evidence"],
            "expansion": s_exp, "contraction": s_con, "spread_mean_fwd_pct": spread,
            "trades_tagged": tagged,
        }
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved results → {args.save}")


if __name__ == "__main__":
    main()
