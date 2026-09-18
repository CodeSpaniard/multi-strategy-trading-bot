"""Multi-year funding regime study — the data-confidence gate for the basis trade.

Answers the decision-memo blocker: how often, and for how long, does perpetual
funding sit above the proposed activation threshold (~15% annualized)? Determines
whether the funding gate would ever fire and whether high-carry windows last long
enough to be worth harvesting.

Sources (probed reachable 2026-06-14):
  - Hyperliquid: deep history (≥2023), HOURLY funding, free  → PRIMARY multi-year
  - Coinbase International: Coinbase's OWN perp funding, HOURLY → venue-gap check
  - OKX: 3-month cap, 8h funding                              → cross-check overlap

Annualization: hourly rate × 24 × 365; 8h rate × 3 × 365.

Usage: python3 backtests/backtest_basis_funding_history.py
"""
import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta

from backtest_basis_feasibility import fetch_funding as fetch_okx_8h  # OKX 8h series

HL_URL = "https://api.hyperliquid.xyz/info"
CBI_URL = "https://api.international.coinbase.com/api/v1/instruments/{}-PERP/funding"
HOURLY_ANN = 24 * 365
OKX_ANN = 3 * 365


def _retry(fn, tries=5):
    """Retry on HTTP 429 with exponential backoff."""
    delay = 1.0
    for attempt in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _post(url, payload):
    data = json.dumps(payload).encode()

    def go():
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "Mozilla/5.0 (funding-study)"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    return _retry(go)


def _get(url):
    def go():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (funding-study)"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    return _retry(go)


def fetch_hyperliquid(coin, start_ms):
    """Page forward through HL fundingHistory (hourly). Returns (dt, rate) oldest-first."""
    out, cur, pages = [], start_ms, 0
    while pages < 200:
        rows = _post(HL_URL, {"type": "fundingHistory", "coin": coin, "startTime": cur})
        if not rows:
            break
        for r in rows:
            out.append((int(r["time"]), float(r["fundingRate"])))
        last = max(r["time"] for r in rows)
        if last <= cur:
            break
        cur = last + 1
        pages += 1
        if len(rows) < 500:
            break
        time.sleep(0.35)
    seen = {}
    for ts, rate in out:
        seen[ts] = rate
    series = sorted((datetime.utcfromtimestamp(ts / 1000), rate) for ts, rate in seen.items())
    return series


def fetch_coinbase_intl(coin, max_rows=2200):
    """Page Coinbase International perp funding (hourly) via result_offset."""
    out, offset, limit = [], 0, 200
    while offset < max_rows:
        url = CBI_URL.format(coin) + f"?result_limit={limit}&result_offset={offset}"
        resp = _get(url)
        rows = resp.get("results", [])
        if not rows:
            break
        for r in rows:
            dt = datetime.strptime(r["event_time"], "%Y-%m-%dT%H:%M:%SZ")
            out.append((dt, float(r["funding_rate"])))
        if len(rows) < limit:
            break
        offset += limit
        time.sleep(0.1)
    out.sort()
    return out


def rolling_ann(series, ann_factor, win):
    """Return list of (dt, rolling-mean-rate * ann_factor * 100) in annualized %."""
    rates = [r for _, r in series]
    out = []
    for i in range(len(series)):
        if i + 1 < win:
            continue
        avg = sum(rates[i - win + 1:i + 1]) / win
        out.append((series[i][0], avg * ann_factor * 100))
    return out


def regime_stats(series, ann_factor, roll_win, thresholds, periods_per_day):
    rates = [r for _, r in series]
    n = len(rates)
    total_carry = sum(rates) * 100  # cumulative % on notional
    roll = rolling_ann(series, ann_factor, roll_win)
    out = {
        "period_start": str(series[0][0].date()),
        "period_end": str(series[-1][0].date()),
        "periods": n,
        "mean_annualized_pct": round(sum(rates) / n * ann_factor * 100, 2),
        "pct_periods_positive": round(sum(1 for r in rates if r > 0) / n * 100, 1),
        "cumulative_carry_pct": round(total_carry, 2),
        "thresholds": {},
    }
    # map rolling dates back to per-period carry for "share of carry" calc
    roll_by_date = {d: v for d, v in roll}
    for T in thresholds:
        above = [(d, v) for d, v in roll if v >= T]
        pct_time = len(above) / len(roll) * 100 if roll else 0
        # contiguous run durations (in periods) where rolling >= T
        durations, run = [], 0
        for _, v in roll:
            if v >= T:
                run += 1
            elif run:
                durations.append(run); run = 0
        if run:
            durations.append(run)
        # share of total carry earned while rolling-annualized >= T
        carry_above = sum(r * 100 for (d, _), r in zip(series[roll_win - 1:], rates[roll_win - 1:])
                          if roll_by_date.get(d, -1e9) >= T)
        out["thresholds"][f"{T}pct"] = {
            "pct_of_time_above": round(pct_time, 1),
            "n_windows": len(durations),
            "median_window_days": round(sorted(durations)[len(durations) // 2] / periods_per_day, 1) if durations else 0,
            "max_window_days": round(max(durations) / periods_per_day, 1) if durations else 0,
            "share_of_total_carry_pct": round(carry_above / total_carry * 100, 1) if total_carry else None,
        }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--start", default="2023-06-16")
    parser.add_argument("--roll-days", type=int, default=7)
    parser.add_argument("--save", default="backtests/basis_funding_history_results.json")
    args = parser.parse_args()

    start_ms = int(datetime.strptime(args.start, "%Y-%m-%d").timestamp() * 1000)
    thresholds = [10, 15, 20]
    results = {"hyperliquid": {}, "venue_crosscheck": {}}
    hl_series = {}

    for coin in args.coins:
        print(f"Fetching Hyperliquid funding for {coin} (hourly, since {args.start})...", flush=True)
        s = fetch_hyperliquid(coin, start_ms)
        hl_series[coin] = s
        print(f"  Got {len(s)} hourly periods ({s[0][0].date()} → {s[-1][0].date()})", flush=True)
        stats = regime_stats(s, HOURLY_ANN, args.roll_days * 24, thresholds, 24)
        results["hyperliquid"][coin] = stats

        print(f"\n{'=' * 70}\n  {coin}  Hyperliquid funding regime ({stats['period_start']} → {stats['period_end']})\n{'=' * 70}")
        print(f"  Mean annualized funding: {stats['mean_annualized_pct']:+.2f}%   "
              f"positive {stats['pct_periods_positive']:.1f}% of hours   "
              f"cumulative {stats['cumulative_carry_pct']:+.1f}%")
        print(f"  {'threshold':<12}{'% of time':>11}{'#windows':>10}{'median d':>10}{'max d':>9}{'% of carry':>12}")
        for T in thresholds:
            r = stats["thresholds"][f"{T}pct"]
            print(f"  >= {T:>2}% ann  {r['pct_of_time_above']:>10.1f}{r['n_windows']:>10}"
                  f"{r['median_window_days']:>10}{r['max_window_days']:>9}{str(r['share_of_total_carry_pct']):>12}")

    # venue cross-check (mean annualized over the last ~80 overlapping days)
    print(f"\n{'=' * 70}\n  VENUE CROSS-CHECK — mean annualized funding, recent overlap\n{'=' * 70}")
    for coin in args.coins:
        row = {"hyperliquid": None, "coinbase_intl": None, "okx": None}
        cutoff = datetime.utcnow() - timedelta(days=80)
        try:
            hl = [r for d, r in hl_series.get(coin, []) if d >= cutoff]
            row["hyperliquid"] = round(sum(hl) / len(hl) * HOURLY_ANN * 100, 2) if hl else None
        except Exception as e:
            row["hyperliquid"] = f"err:{e}"
        try:
            cbi = [r for d, r in fetch_coinbase_intl(coin) if d >= cutoff]
            row["coinbase_intl"] = round(sum(cbi) / len(cbi) * HOURLY_ANN * 100, 2) if cbi else None
        except Exception as e:
            row["coinbase_intl"] = f"err:{e}"
        try:
            okx = [r for d, r in fetch_okx_8h(f"{coin}-USDT-SWAP", 80)]
            row["okx"] = round(sum(okx) / len(okx) * OKX_ANN * 100, 2) if okx else None
        except Exception as e:
            row["okx"] = f"err:{e}"
        results["venue_crosscheck"][coin] = row
        print(f"  {coin:<5} Hyperliquid {str(row['hyperliquid']):>8}%   "
              f"Coinbase-Intl {str(row['coinbase_intl']):>8}%   OKX {str(row['okx']):>8}%")

    if args.save:
        out = {
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "study": "Multi-year funding regime study (basis-trade data-confidence gate)",
            "roll_window_days": args.roll_days,
            "thresholds_annualized_pct": thresholds,
            "caveats": ["HL/Coinbase-Intl fund hourly vs Coinbase Advanced US 8h",
                        "Coinbase International != Coinbase Advanced US (same firm, offshore entity)",
                        "rolling-window regime def; gate persistence spec still open"],
            "results": results,
        }
        with open(args.save, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved results → {args.save}")


if __name__ == "__main__":
    main()
