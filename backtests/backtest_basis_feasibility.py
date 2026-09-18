"""Zero-capital feasibility study for the basis (cash-and-carry) trade.

Spec (see project memory `project_basis_trade`): long spot + short perp, USDC
margin, delta-neutral, 2-3x leverage on the short leg. The PnL of a delta-neutral
long-spot/short-perp position is the FUNDING the short collects (longs pay shorts
when funding > 0) minus costs. Price exposure cancels. So feasibility =
"is perpetual funding predominantly positive, sizeable, and stable enough to
harvest after costs?"

DATA NOTE: the live Coinbase API key is spot-only (no futures/funding access
until the account is futures-enabled at the $2k gate). Funding rates are tightly
correlated across venues, so this uses OKX USDT-margined perpetual funding as a
PROXY (USDT ≈ USDC). Coinbase-specific funding, fees, and margin must be
re-validated on Coinbase data before any live build. This study answers only:
"does the carry exist and is it worth building?" — not the exact live ROI.

Usage: python3 backtests/backtest_basis_feasibility.py [--days 1100]
"""
import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta

OKX = "https://www.okx.com/api/v5/public/funding-rate-history"
PERIODS_PER_DAY = 3          # OKX funds every 8h
PERIODS_PER_YEAR = 3 * 365


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (basis-feasibility-study)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_funding(inst_id, days):
    """Page backwards through OKX funding-rate-history (100/call, newest-first).
    Returns list of (datetime, funding_rate_float) oldest-first."""
    cutoff_ms = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    out = []
    after = ""  # 'after' = return records older than this ts
    while True:
        url = f"{OKX}?instId={inst_id}&limit=100"
        if after:
            url += f"&after={after}"
        resp = http_get(url)
        if resp.get("code") != "0":
            raise RuntimeError(f"OKX error for {inst_id}: {resp.get('msg')}")
        rows = resp.get("data", [])
        if not rows:
            break
        for r in rows:
            ts = int(r["fundingTime"])
            out.append((ts, float(r["realizedRate"])))
        oldest = min(int(r["fundingTime"]) for r in rows)
        after = str(oldest)
        if oldest <= cutoff_ms or len(rows) < 100:
            break
        time.sleep(0.15)  # politeness
    out = [(datetime.utcfromtimestamp(ts / 1000), rate) for ts, rate in out
           if ts >= cutoff_ms]
    out.sort(key=lambda x: x[0])
    return out


def analyze(inst_id, series, cost_bps_roundtrip=80, avg_hold_days=90):
    """Carry economics from a funding series. cost amortizes the 4-leg open/close
    over an assumed holding period to annualize the drag."""
    if not series:
        return {"inst_id": inst_id, "periods": 0}
    rates = [r for _, r in series]
    n = len(rates)
    mean_8h = sum(rates) / n
    pos = sum(1 for r in rates if r > 0)
    gross_cum = sum(rates) * 100            # % on notional over the window
    span_days = (series[-1][0] - series[0][0]).days or 1
    ann_gross = mean_8h * PERIODS_PER_YEAR * 100

    # cumulative-carry equity curve (on notional) and its worst drawdown
    cum, peak, worst_dd, run, longest_neg = 0.0, 0.0, 0.0, 0, 0
    for r in rates:
        cum += r * 100
        peak = max(peak, cum)
        worst_dd = min(worst_dd, cum - peak)
        run = run + 1 if r < 0 else 0
        longest_neg = max(longest_neg, run)

    # cost drag: 4 legs (open 2 + close 2) per holding cycle, annualized
    cycles_per_year = 365 / avg_hold_days
    ann_cost = cost_bps_roundtrip / 100 * cycles_per_year
    ann_net = ann_gross - ann_cost

    return {
        "inst_id": inst_id,
        "period_start": str(series[0][0].date()),
        "period_end": str(series[-1][0].date()),
        "periods": n,
        "span_days": span_days,
        "mean_funding_8h_pct": round(mean_8h * 100, 5),
        "pct_periods_positive": round(pos / n * 100, 1),
        "annualized_gross_pct": round(ann_gross, 2),
        "cumulative_gross_pct": round(gross_cum, 2),
        "worst_carry_drawdown_pct": round(worst_dd, 2),
        "longest_negative_streak_periods": longest_neg,
        "longest_negative_streak_days": round(longest_neg / PERIODS_PER_DAY, 1),
        "assumed_cost_bps_roundtrip": cost_bps_roundtrip,
        "assumed_avg_hold_days": avg_hold_days,
        "annualized_net_pct": round(ann_net, 2),
        "net_at_2x_leverage_pct": round(ann_net * 2, 2),
        "net_at_3x_leverage_pct": round(ann_net * 3, 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1100)
    parser.add_argument("--insts", nargs="+", default=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    parser.add_argument("--cost-bps", type=int, default=80)
    parser.add_argument("--hold-days", type=int, default=90)
    parser.add_argument("--save", default="backtests/basis_feasibility_results.json")
    args = parser.parse_args()

    summaries = {}
    for inst in args.insts:
        print(f"Fetching OKX funding history for {inst}...", flush=True)
        series = fetch_funding(inst, args.days)
        print(f"  Got {len(series)} funding periods "
              f"({series[0][0].date()} → {series[-1][0].date()})" if series else "  none", flush=True)
        s = analyze(inst, series, args.cost_bps, args.hold_days)
        summaries[inst] = s

        print(f"\n{'=' * 68}\n  {inst}  (long spot + short perp, delta-neutral)\n{'=' * 68}")
        print(f"  Window:                 {s['period_start']} → {s['period_end']} ({s['span_days']}d)")
        print(f"  Funding periods (8h):   {s['periods']}")
        print(f"  Mean funding / 8h:      {s['mean_funding_8h_pct']:+.5f}%")
        print(f"  Periods positive:       {s['pct_periods_positive']:.1f}%  (short collects)")
        print(f"  Annualized GROSS carry: {s['annualized_gross_pct']:+.2f}%")
        print(f"  Cumulative over window: {s['cumulative_gross_pct']:+.2f}%")
        print(f"  Worst carry drawdown:   {s['worst_carry_drawdown_pct']:+.2f}%  "
              f"(longest negative streak {s['longest_negative_streak_days']}d)")
        print(f"  -- after costs ({s['assumed_cost_bps_roundtrip']}bps r/t, {s['assumed_avg_hold_days']}d hold) --")
        print(f"  Annualized NET carry:   {s['annualized_net_pct']:+.2f}%  (1x, on notional)")
        print(f"  Net @2x / @3x leverage: {s['net_at_2x_leverage_pct']:+.2f}% / {s['net_at_3x_leverage_pct']:+.2f}%")

    if args.save:
        out = {
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "study": "Basis (cash-and-carry) feasibility via OKX funding proxy",
            "data_source": "OKX USDT-margined perpetual funding (PROXY for Coinbase USDC perp)",
            "caveats": ["OKX proxy not Coinbase", "ignores liquidation/margin-call risk",
                        "ignores exact Coinbase fee schedule + USDC borrow",
                        "perpetual funding model, not dated-futures basis convergence"],
            "lookback_days": args.days,
            "results": summaries,
        }
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved results → {args.save}")


if __name__ == "__main__":
    main()
