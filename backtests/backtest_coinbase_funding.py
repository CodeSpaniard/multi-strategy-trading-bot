"""Coinbase-specific funding study — calibrate the live activation gate.

Codex's call (2026-06-14): the basis-trade build thesis is real, but the live
activation gate must be set from COINBASE's own funding distribution, not a
proxy — Coinbase appears structurally lower-yield (~1/2 OKX, ~1/3 Hyperliquid).
15% (from proxies) is now a benchmark, NOT a deployable Coinbase threshold.

This pulls the deepest Coinbase International perp funding history available and
measures what Codex asked for:
  1. long-run average (annualized)
  2. percentile distribution
  3. persistence of high-funding windows
  4. post-cost carry at realistic hold durations (Coinbase taker ~0.05%/leg)
  5. a threshold calibrated from Coinbase's OWN distribution (captures ~2/3 carry)

Caveat: Coinbase International != Coinbase Advanced US (same firm, offshore
entity; closest available). Hourly funding; Coinbase Advanced US is 8h, ±0.75% cap.

Usage: python3 backtests/backtest_coinbase_funding.py
"""
import argparse
import json
import time
import urllib.request
from datetime import datetime

CBI_URL = "https://api.international.coinbase.com/api/v1/instruments/{}-PERP/funding"
HOURLY_ANN = 24 * 365
TAKER_PER_LEG_BPS = 5          # ~0.05%; 4 legs (open 2 + close 2) = 20bps round trip
ROUND_TRIP_BPS = TAKER_PER_LEG_BPS * 4


def parse_dt(s):
    s = s.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable event_time: {s}")


def _get(url, tries=5):
    delay = 1.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cb-funding-study)"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(delay); delay *= 2; continue
            raise


def fetch_coinbase_deep(coin, hard_cap=60000):
    """Paginate result_offset until exhausted (or offset ceiling). Returns
    (dt, rate) oldest-first and the offset where it stopped."""
    out, offset, limit = [], 0, 300
    stop = "exhausted"
    while offset < hard_cap:
        url = CBI_URL.format(coin) + f"?result_limit={limit}&result_offset={offset}"
        resp = _get(url)
        rows = resp.get("results", []) if resp else []
        if not rows:
            break
        for r in rows:
            out.append((parse_dt(r["event_time"]), float(r["funding_rate"])))
        if len(rows) < limit:
            break
        offset += limit
        time.sleep(0.15)
    else:
        stop = f"hit hard_cap {hard_cap}"
    seen = {dt: rate for dt, rate in out}
    return sorted(seen.items()), stop


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def analyze(coin, series):
    rates_raw = [r for _, r in series]
    n = len(rates_raw)
    # OUTLIER GUARD: real hourly perp funding is tiny; |rate| > 1%/hr is almost
    # certainly a bad print (Coinbase Advanced caps ±0.75% per *8h*). Clip for the
    # mean/cumulative (which outliers corrupt); percentiles are robust as-is.
    CLIP = 0.01
    n_clipped = sum(1 for r in rates_raw if abs(r) > CLIP)
    rates = [max(-CLIP, min(CLIP, r)) for r in rates_raw]
    raw_min_ann = round(min(rates_raw) * HOURLY_ANN * 100, 1)
    raw_max_ann = round(max(rates_raw) * HOURLY_ANN * 100, 1)

    ann = [r * HOURLY_ANN * 100 for r in rates_raw]      # raw per-period annualized %
    ann_sorted = sorted(ann)
    mean_ann = sum(rates) / n * HOURLY_ANN * 100         # clipped mean
    total_carry = sum(rates) * 100                       # clipped cumulative

    # rolling 7-day (168h) annualized series for persistence
    win = 168
    roll = []
    for i in range(win - 1, n):
        roll.append(sum(rates[i - win + 1:i + 1]) / win * HOURLY_ANN * 100)

    def regime(T):
        above = [v for v in roll if v >= T]
        durations, run = [], 0
        for v in roll:
            if v >= T:
                run += 1
            elif run:
                durations.append(run); run = 0
        if run:
            durations.append(run)
        # share of carry while rolling >= T
        carry_above = sum(rates[i] * 100 for i in range(win - 1, n) if roll[i - win + 1] >= T)
        return {
            "pct_of_time": round(len(above) / len(roll) * 100, 1) if roll else 0,
            "n_windows": len(durations),
            "median_days": round(sorted(durations)[len(durations) // 2] / 24, 1) if durations else 0,
            "max_days": round(max(durations) / 24, 1) if durations else 0,
            "share_of_carry_pct": round(carry_above / total_carry * 100, 1) if total_carry else None,
        }

    # post-cost carry at hold durations (annualized net, on notional)
    mean_daily = sum(rates) / n * 24 * 100
    holds = {}
    for H in (7, 14, 30, 90):
        gross_cycle = mean_daily * H
        net_cycle = gross_cycle - ROUND_TRIP_BPS / 100
        holds[f"{H}d"] = round(net_cycle * (365 / H), 2)

    # calibrate a Coinbase-specific threshold capturing ~2/3 of carry
    calib = None
    for T in [t / 2 for t in range(0, 60)]:   # 0..30% in 0.5 steps
        if regime(T)["share_of_carry_pct"] is not None and regime(T)["share_of_carry_pct"] <= 67:
            calib = T
            break

    return {
        "period_start": str(series[0][0].date()),
        "period_end": str(series[-1][0].date()),
        "periods_hours": n,
        "approx_days": round(n / 24, 0),
        "mean_annualized_pct_clipped": round(mean_ann, 2),
        "median_annualized_pct": round(percentile(ann_sorted, 50), 2),
        "pct_periods_positive": round(sum(1 for r in rates_raw if r > 0) / n * 100, 1),
        "cumulative_carry_pct_clipped": round(total_carry, 2),
        "n_bad_prints_clipped": n_clipped,
        "raw_min_annualized_pct": raw_min_ann,
        "raw_max_annualized_pct": raw_max_ann,
        "percentiles_annualized": {f"p{p}": round(percentile(ann_sorted, p), 2)
                                   for p in (10, 25, 50, 75, 90)},
        "persistence": {f"{T}pct": regime(T) for T in (5, 10, 15)},
        "post_cost_net_annualized_by_hold": holds,
        "round_trip_cost_bps": ROUND_TRIP_BPS,
        "calibrated_threshold_for_~2/3_carry_pct": calib,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--save", default="backtests/coinbase_funding_results.json")
    args = parser.parse_args()

    results = {}
    for coin in args.coins:
        print(f"Pulling deepest Coinbase International funding for {coin}-PERP...", flush=True)
        series, stop = fetch_coinbase_deep(coin)
        if not series:
            print("  none"); continue
        print(f"  Got {len(series)} hourly periods ({series[0][0].date()} → {series[-1][0].date()}), pagination: {stop}", flush=True)
        s = analyze(coin, series)
        results[coin] = s

        print(f"\n{'=' * 70}\n  {coin}-PERP  Coinbase International funding ({s['period_start']} → {s['period_end']}, ~{s['approx_days']:.0f}d)\n{'=' * 70}")
        print(f"  Median annualized:  {s['median_annualized_pct']:+.2f}%  (robust headline)   positive {s['pct_periods_positive']:.1f}% of hours")
        print(f"  Mean annualized:    {s['mean_annualized_pct_clipped']:+.2f}% (clipped ±1%/hr)   cumulative {s['cumulative_carry_pct_clipped']:+.1f}%")
        print(f"  Bad prints clipped: {s['n_bad_prints_clipped']}  (raw range {s['raw_min_annualized_pct']:+.0f}% .. {s['raw_max_annualized_pct']:+.0f}% annualized — data errors)")
        print(f"  Percentiles (ann):  " + "  ".join(f"{k} {v:+.1f}%" for k, v in s['percentiles_annualized'].items()))
        print(f"  Post-cost net annualized ({s['round_trip_cost_bps']}bps r/t) by hold:")
        print(f"      " + "   ".join(f"{k}: {v:+.2f}%" for k, v in s['post_cost_net_annualized_by_hold'].items()))
        print(f"  Persistence (rolling-7d annualized):")
        print(f"      {'thresh':<10}{'% time':>8}{'#win':>6}{'med d':>8}{'max d':>8}{'% carry':>9}")
        for T in (5, 10, 15):
            r = s['persistence'][f'{T}pct']
            print(f"      >= {T:>2}%    {r['pct_of_time']:>7}{r['n_windows']:>6}{r['median_days']:>8}{r['max_days']:>8}{str(r['share_of_carry_pct']):>9}")
        print(f"  Calibrated threshold capturing ~2/3 of carry: {s['calibrated_threshold_for_~2/3_carry_pct']}% annualized")

    if args.save:
        out = {
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "study": "Coinbase-specific funding distribution — calibrate live activation gate",
            "data_source": "Coinbase International BTC/ETH-PERP funding (hourly)",
            "caveats": ["Coinbase Intl != Coinbase Advanced US (closest available)",
                        "hourly vs Coinbase Advanced US 8h ±0.75% cap",
                        "depth limited by API result_offset pagination",
                        f"cost = {ROUND_TRIP_BPS}bps round trip (taker {TAKER_PER_LEG_BPS}bps x4 legs)"],
            "results": results,
        }
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved results → {args.save}")


if __name__ == "__main__":
    main()
