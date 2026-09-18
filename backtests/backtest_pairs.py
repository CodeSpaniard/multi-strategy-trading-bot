"""Sprint 1 — Pairs trading backtest (walk-forward + production list export).

Tests cointegration-based pairs trading on the existing 75-symbol equity universe
over the same 18-month window the dip-scanner backtest used. Walk-forward across
4 quarterly out-of-sample windows; require GATE: PASS in ≥3 of 4 (≥75%).

Also writes the production pair list (`backtests/pairs_locked.json`) by
re-screening on the most recent 12 months of available data. The bot reads
this file at startup.

Methodology:
  1. Pull daily bars 2024-01-01 → 2025-06-30 for the 75-symbol universe.
  2. Walk-forward in 4 chunks: train 6mo, trade 3mo, roll 3mo each step.
  3. Engle-Granger cointegration (statsmodels.coint) across all pairs in each
     training window. Filter on p < 0.01 (tightened from initial 0.05) and OLS
     hedge ratio in [0.1, 10]. One-pair-per-symbol concentration guard. Top N.
  4. Per chunk: rolling 60-day β and z-score on OOS. Entry |z|>2; exit |z|<0.5;
     stop |z|>4; time stop 30 trading days. Dollar-neutral $200/leg, 10bps/leg.
  5. Per chunk: compute Sharpe, max DD, trade count. Per-chunk gate check.
  6. Overall pass = ≥3 of 4 chunk-level GATE: PASS.
  7. Production list: re-screen on most recent 12 months ending today, write JSON.

GATE rule (per chunk):
    Sharpe ≥ 1.5  AND  max_drawdown ≤ 15%  AND  ≥ 10 trades (scaled per quarter)

Overall GATE:
    ≥3 of 4 chunk gates pass
"""
import json
import os
import sys
import warnings
from datetime import datetime, timedelta
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, Adjustment
from statsmodels.tsa.stattools import coint

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.scanner import UNIVERSE

warnings.filterwarnings("ignore")


# === GATE thresholds (per-chunk) ===
GATE_SHARPE = 1.5
GATE_MAX_DD_PCT = 15.0
GATE_MIN_TRADES_PER_CHUNK = 10   # quarterly trade count, scaled from 30 for 6mo
OVERALL_MIN_PASS_FRACTION = 0.75  # ≥75% of chunks must pass

# === Backtest period (validation) ===
HIST_START = datetime(2024, 1, 1)
HIST_END = datetime(2025, 6, 30)

# === Walk-forward layout ===
TRAIN_MONTHS = 6
TRADE_MONTHS = 3
WALK_STEP_MONTHS = 3  # chunks advance by this much each step

# === Cointegration filter (tightened) ===
COINT_P_MAX = 0.01      # was 0.05; this controls multiple-testing FP risk
BETA_MIN = 0.1
BETA_MAX = 10.0
TOP_N_PAIRS = 20

# === Strategy parameters ===
LOOKBACK_DAYS = 60
ENTRY_Z = 2.0
EXIT_Z = 0.5
STOP_Z = 4.0
TIME_STOP_DAYS = 30
MAX_CONCURRENT = 5
PER_LEG_USD = 200.0
COST_BPS_PER_LEG = 10
STARTING_CAPITAL = PER_LEG_USD * 2 * MAX_CONCURRENT

TRADING_DAYS_PER_YEAR = 252

# === Production list output ===
PAIRS_LOCKED_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pairs_locked.json"
)
PROD_TRAIN_MONTHS = 12  # use last 12 months for the live screen


# ─────────────────────── data fetch ───────────────────────

def fetch_daily(client, symbol, start, end):
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX,
        adjustment=Adjustment.ALL
    )
    try:
        df = client.get_stock_bars(req).df
    except Exception:
        return None
    if df.empty:
        return None
    if "symbol" in df.index.names:
        df = df.xs(symbol, level="symbol")
    return df


def fetch_all_bars(symbols, start, end):
    load_dotenv(".env.paper", override=True)
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )
    print(f"Fetching daily bars for {len(symbols)} symbols "
          f"({start.date()} → {end.date()})...", flush=True)
    data = {}
    skipped = []
    for i, sym in enumerate(symbols, 1):
        df = fetch_daily(client, sym, start, end)
        if df is not None and len(df) > 50:
            s = df["close"].astype(float)
            if s.index.tz is not None:
                s.index = s.index.tz_convert(None)
            data[sym] = s
        else:
            skipped.append(sym)
        if i % 15 == 0:
            print(f"  {i}/{len(symbols)} fetched", flush=True)
    print(f"  Got {len(data)} usable series. Skipped: {skipped}", flush=True)
    return data


# ─────────────────────── cointegration ───────────────────────

def screen_pairs(prices_train: dict, verbose: bool = False) -> list:
    """Engle-Granger across all pairs on training window.
    Filtered + sorted by p asc, with single-name concentration guard.
    """
    symbols = sorted(prices_train.keys())
    n_pairs = len(symbols) * (len(symbols) - 1) // 2
    if verbose:
        print(f"  Testing {n_pairs} pairs on training window (p<{COINT_P_MAX})...",
              flush=True)
    results = []
    for a, b in combinations(symbols, 2):
        sa, sb = prices_train[a], prices_train[b]
        df = pd.concat([sa, sb], axis=1, join="inner").dropna()
        if len(df) < 100:
            continue
        df.columns = ["a", "b"]
        try:
            _score, pvalue, _ = coint(df["a"].values, df["b"].values)
        except Exception:
            continue
        if pvalue > COINT_P_MAX:
            continue
        x = np.vstack([df["b"].values, np.ones(len(df))]).T
        beta, _intercept = np.linalg.lstsq(x, df["a"].values, rcond=None)[0]
        if not (BETA_MIN <= abs(beta) <= BETA_MAX):
            continue
        results.append({"a": a, "b": b, "pvalue": float(pvalue), "beta": float(beta)})

    results.sort(key=lambda r: r["pvalue"])
    selected = []
    used = set()
    for r in results:
        if r["a"] in used or r["b"] in used:
            continue
        selected.append(r)
        used.add(r["a"])
        used.add(r["b"])
        if len(selected) >= TOP_N_PAIRS:
            break
    return selected


# ─────────────────────── simulator ───────────────────────

def simulate_pair(pair_info: dict, prices_a_full: pd.Series, prices_b_full: pd.Series,
                  trade_start: datetime, trade_end: datetime):
    """Walk-forward simulate one pair on a [trade_start, trade_end] window.
    Returns list of completed trade dicts.
    """
    df = pd.concat([prices_a_full, prices_b_full], axis=1, join="inner").dropna()
    df.columns = ["a", "b"]
    if df.index.tz is not None:
        df.index = df.index.tz_convert(None)
    start_ts = pd.Timestamp(trade_start)
    end_ts = pd.Timestamp(trade_end)

    trades = []
    in_position = False
    pos = None
    name = f"{pair_info['a']}-{pair_info['b']}"

    for t in df.index:
        if t < start_ts or t > end_ts:
            continue
        history = df.loc[df.index <= t].tail(LOOKBACK_DAYS + 1)
        if len(history) < LOOKBACK_DAYS + 1:
            continue
        a_hist = history["a"].values
        b_hist = history["b"].values
        x = np.vstack([b_hist[:-1], np.ones(LOOKBACK_DAYS)]).T
        try:
            beta_hat = float(np.linalg.lstsq(x, a_hist[:-1], rcond=None)[0][0])
        except Exception:
            continue
        if not (BETA_MIN <= abs(beta_hat) <= BETA_MAX):
            continue

        spread_hist = a_hist[:-1] - beta_hat * b_hist[:-1]
        mu = float(np.mean(spread_hist))
        sigma = float(np.std(spread_hist, ddof=1))
        if sigma <= 1e-9:
            continue
        spread_now = a_hist[-1] - beta_hat * b_hist[-1]
        z = (spread_now - mu) / sigma

        price_a = float(history["a"].iloc[-1])
        price_b = float(history["b"].iloc[-1])

        if in_position:
            days_held = (t - pos["entry_date"]).days
            ret_a = (price_a - pos["entry_a"]) / pos["entry_a"]
            ret_b = (price_b - pos["entry_b"]) / pos["entry_b"]
            gross_usd = pos["side"] * (PER_LEG_USD * ret_a - PER_LEG_USD * ret_b)

            exit_reason = None
            if abs(z) <= EXIT_Z:
                exit_reason = "mean-revert"
            elif abs(z) >= STOP_Z:
                exit_reason = "z-stop"
            elif days_held >= TIME_STOP_DAYS:
                exit_reason = "time-stop"

            if exit_reason is not None:
                cost_usd = 4 * COST_BPS_PER_LEG / 10000 * PER_LEG_USD
                net_usd = gross_usd - cost_usd
                trades.append({
                    "pair": name,
                    "entry_date": pos["entry_date"],
                    "exit_date": t,
                    "side": pos["side"],
                    "entry_z": pos["entry_z"],
                    "exit_z": z,
                    "days": days_held,
                    "gross_usd": gross_usd,
                    "net_usd": net_usd,
                    "reason": exit_reason,
                })
                in_position = False
                pos = None
        else:
            if z >= ENTRY_Z:
                pos = {"side": -1, "entry_date": t, "entry_a": price_a,
                       "entry_b": price_b, "entry_z": z, "beta": beta_hat}
                in_position = True
            elif z <= -ENTRY_Z:
                pos = {"side": +1, "entry_date": t, "entry_a": price_a,
                       "entry_b": price_b, "entry_z": z, "beta": beta_hat}
                in_position = True

    # Force-close at window end if still in position
    if in_position:
        sub = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
        if len(sub) > 0:
            t = sub.index[-1]
            price_a = float(sub["a"].iloc[-1])
            price_b = float(sub["b"].iloc[-1])
            ret_a = (price_a - pos["entry_a"]) / pos["entry_a"]
            ret_b = (price_b - pos["entry_b"]) / pos["entry_b"]
            gross_usd = pos["side"] * (PER_LEG_USD * ret_a - PER_LEG_USD * ret_b)
            cost_usd = 4 * COST_BPS_PER_LEG / 10000 * PER_LEG_USD
            trades.append({
                "pair": name,
                "entry_date": pos["entry_date"], "exit_date": t,
                "side": pos["side"], "entry_z": pos["entry_z"], "exit_z": 0.0,
                "days": (t - pos["entry_date"]).days,
                "gross_usd": gross_usd, "net_usd": gross_usd - cost_usd,
                "reason": "eof",
            })
    return trades


# ─────────────────────── aggregation ───────────────────────

def portfolio_stats(all_trades, period_start, period_end):
    """Aggregate trades into daily P&L, return (sharpe, max_dd_pct, trade_count, total_pnl_usd)."""
    if not all_trades:
        return 0.0, 0.0, 0, 0.0
    df = pd.DataFrame(all_trades)
    df["exit_date"] = pd.to_datetime(df["exit_date"]).dt.tz_localize(None)
    daily = df.groupby(df["exit_date"].dt.date)["net_usd"].sum()
    daily.index = pd.to_datetime(daily.index)
    full_idx = pd.bdate_range(period_start, period_end)
    daily = daily.reindex(full_idx, fill_value=0.0)
    cum = daily.cumsum()
    total = float(cum.iloc[-1]) if len(cum) else 0.0

    returns = daily / STARTING_CAPITAL
    mean_ret = returns.mean()
    std_ret = returns.std(ddof=1)
    sharpe = float(mean_ret / std_ret * np.sqrt(TRADING_DAYS_PER_YEAR)) if std_ret > 0 else 0.0

    equity = STARTING_CAPITAL + cum
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max * 100
    max_dd_pct = float(abs(dd.min()))

    return sharpe, max_dd_pct, len(all_trades), total


def gate_check(sharpe, max_dd_pct, trade_count, min_trades):
    failures = []
    if sharpe < GATE_SHARPE:
        failures.append(f"Sharpe {sharpe:.2f} < {GATE_SHARPE}")
    if max_dd_pct > GATE_MAX_DD_PCT:
        failures.append(f"max_dd {max_dd_pct:.2f}% > {GATE_MAX_DD_PCT}%")
    if trade_count < min_trades:
        failures.append(f"trades {trade_count} < {min_trades}")
    return ("PASS", []) if not failures else ("FAIL", failures)


# ─────────────────────── walk-forward driver ───────────────────────

def build_walk_forward_chunks():
    """Returns list of (train_start, train_end, trade_start, trade_end)."""
    chunks = []
    # First trade window starts at HIST_START + TRAIN_MONTHS
    trade_start = HIST_START + pd.DateOffset(months=TRAIN_MONTHS)
    while trade_start + pd.DateOffset(months=TRADE_MONTHS) <= HIST_END + timedelta(days=1):
        train_start = trade_start - pd.DateOffset(months=TRAIN_MONTHS)
        train_end = trade_start - timedelta(days=1)
        trade_end = trade_start + pd.DateOffset(months=TRADE_MONTHS) - timedelta(days=1)
        chunks.append((train_start.to_pydatetime(), train_end.to_pydatetime(),
                       trade_start.to_pydatetime(), trade_end.to_pydatetime()))
        trade_start = trade_start + pd.DateOffset(months=WALK_STEP_MONTHS)
    return chunks


def run_walk_forward(prices: dict):
    chunks = build_walk_forward_chunks()
    print(f"\n{'═' * 72}")
    print(f"WALK-FORWARD VALIDATION: {len(chunks)} chunks")
    print(f"{'═' * 72}", flush=True)

    chunk_results = []
    for i, (tr_s, tr_e, td_s, td_e) in enumerate(chunks, 1):
        print(f"\n--- Chunk {i}/{len(chunks)}: "
              f"train {tr_s.date()}→{tr_e.date()}, trade {td_s.date()}→{td_e.date()} ---")
        # Slice training data
        prices_train = {}
        for sym, s in prices.items():
            train = s[(s.index >= pd.Timestamp(tr_s)) & (s.index <= pd.Timestamp(tr_e))]
            if len(train) > 80:
                prices_train[sym] = train
        pairs = screen_pairs(prices_train, verbose=True)
        if not pairs:
            print(f"  No pairs pass screen — chunk FAIL by default")
            chunk_results.append({
                "chunk": i, "n_pairs": 0, "trades": 0,
                "sharpe": 0.0, "max_dd": 0.0, "pnl_usd": 0.0,
                "status": "FAIL", "reason": "no pairs"
            })
            continue
        print(f"  Selected {len(pairs)} pairs after concentration guard")

        all_trades = []
        for p in pairs:
            if p["a"] not in prices or p["b"] not in prices:
                continue
            trades = simulate_pair(p, prices[p["a"]], prices[p["b"]], td_s, td_e)
            all_trades.extend(trades)

        sharpe, max_dd, n_trades, pnl = portfolio_stats(all_trades, td_s, td_e)
        status, fails = gate_check(sharpe, max_dd, n_trades, GATE_MIN_TRADES_PER_CHUNK)
        reason = "; ".join(fails) if fails else "all gates pass"
        chunk_results.append({
            "chunk": i, "n_pairs": len(pairs), "trades": n_trades,
            "sharpe": sharpe, "max_dd": max_dd, "pnl_usd": pnl,
            "status": status, "reason": reason
        })
        print(f"  Result: Sharpe={sharpe:.2f}, max_dd={max_dd:.2f}%, "
              f"trades={n_trades}, P&L=${pnl:+.2f} → {status}")
        if fails:
            print(f"  Fail reasons: {'; '.join(fails)}")

    # Overall verdict
    n_pass = sum(1 for r in chunk_results if r["status"] == "PASS")
    n_total = len(chunk_results)
    pass_frac = n_pass / n_total if n_total else 0.0

    print(f"\n{'═' * 72}")
    print(f"WALK-FORWARD SUMMARY")
    print(f"{'═' * 72}")
    print(f"  {'CHUNK':<6} {'PAIRS':>6} {'TRADES':>7} {'SHARPE':>7} {'DD':>7} "
          f"{'PNL$':>9} {'GATE':>6}")
    for r in chunk_results:
        print(f"  {r['chunk']:<6} {r['n_pairs']:>6} {r['trades']:>7} "
              f"{r['sharpe']:>7.2f} {r['max_dd']:>6.2f}% {r['pnl_usd']:>+8.2f} "
              f"{r['status']:>6}")
    print(f"\n  Chunks passing GATE: {n_pass}/{n_total} ({pass_frac*100:.0f}%)")
    overall = "PASS" if pass_frac >= OVERALL_MIN_PASS_FRACTION else "FAIL"
    print(f"\n  OVERALL GATE: {overall} "
          f"(required ≥{OVERALL_MIN_PASS_FRACTION*100:.0f}% of chunks)")
    print(f"{'═' * 72}", flush=True)
    return overall, chunk_results


# ─────────────────────── production list export ───────────────────────

def generate_production_list(prices: dict):
    """Re-screen on the most recent PROD_TRAIN_MONTHS of available data. Write JSON."""
    print(f"\n{'═' * 72}")
    print(f"PRODUCTION PAIR LIST: re-screen on most recent {PROD_TRAIN_MONTHS} months")
    print(f"{'═' * 72}", flush=True)
    # Use the most recent date present in the data, not today (data may lag)
    last_date = max(s.index.max() for s in prices.values())
    cutoff = last_date - pd.DateOffset(months=PROD_TRAIN_MONTHS)
    print(f"  Train window: {cutoff.date()} → {last_date.date()}")

    prices_recent = {}
    for sym, s in prices.items():
        recent = s[s.index >= cutoff]
        if len(recent) > 80:
            prices_recent[sym] = recent

    pairs = screen_pairs(prices_recent, verbose=True)
    if not pairs:
        print(f"  WARNING: no pairs pass on recent data; pairs_locked.json NOT written")
        return None

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "train_window_start": str(cutoff.date()),
        "train_window_end": str(last_date.date()),
        "params": {
            "coint_p_max": COINT_P_MAX,
            "beta_range": [BETA_MIN, BETA_MAX],
            "top_n": TOP_N_PAIRS,
            "lookback_days": LOOKBACK_DAYS,
            "entry_z": ENTRY_Z,
            "exit_z": EXIT_Z,
            "stop_z": STOP_Z,
            "time_stop_days": TIME_STOP_DAYS,
        },
        "pairs": pairs,
    }
    with open(PAIRS_LOCKED_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  Wrote {len(pairs)} pairs to {PAIRS_LOCKED_PATH}")
    for p in pairs:
        print(f"    {p['a']:>5}/{p['b']:<5}  p={p['pvalue']:.5f}  β={p['beta']:+.3f}")
    return pairs


# ─────────────────────── main ───────────────────────

def main():
    print("=" * 72)
    print(f"PAIRS TRADING BACKTEST — Walk-forward + Production List")
    print(f"Universe: {len(UNIVERSE)} symbols (src/scanner.py)")
    print(f"History:  {HIST_START.date()} → {HIST_END.date()}")
    print(f"Walk:     {TRAIN_MONTHS}mo train, {TRADE_MONTHS}mo trade, "
          f"step {WALK_STEP_MONTHS}mo")
    print(f"Screen:   p<{COINT_P_MAX} (tightened), β∈[{BETA_MIN},{BETA_MAX}], "
          f"top {TOP_N_PAIRS}, one-pair-per-symbol")
    print(f"Strategy: rolling {LOOKBACK_DAYS}d β + z-score | "
          f"entry |z|>{ENTRY_Z} exit |z|<{EXIT_Z} stop |z|>{STOP_Z} time {TIME_STOP_DAYS}d")
    print(f"GATE per-chunk: Sharpe≥{GATE_SHARPE} AND max_dd≤{GATE_MAX_DD_PCT}% "
          f"AND trades≥{GATE_MIN_TRADES_PER_CHUNK}")
    print(f"GATE overall:   ≥{OVERALL_MIN_PASS_FRACTION*100:.0f}% chunks pass")
    print("=" * 72, flush=True)

    # Fetch enough history for walk-forward + most-recent production screen
    fetch_start = HIST_START
    fetch_end = datetime.now() - timedelta(days=1)  # yesterday (avoid 15-min SIP delay edge)
    prices = fetch_all_bars(UNIVERSE, fetch_start, fetch_end)
    if len(prices) < 20:
        print("\nERROR: insufficient symbols with usable data; aborting.")
        sys.exit(1)

    # Walk-forward validation on historical window
    overall_validation, chunk_results = run_walk_forward(prices)

    # Production list (separate from validation)
    prod_pairs = generate_production_list(prices)

    print(f"\n{'═' * 72}")
    print(f"SPRINT 1 VERDICT")
    print(f"{'═' * 72}")
    print(f"  Walk-forward overall:  GATE: {overall_validation}")
    if prod_pairs:
        print(f"  Production pairs:      {len(prod_pairs)} written to pairs_locked.json")
    else:
        print(f"  Production pairs:      NONE (screen on recent data returned 0 pairs)")
    print(f"{'═' * 72}", flush=True)

    if overall_validation == "FAIL" or not prod_pairs:
        sys.exit(1)


if __name__ == "__main__":
    main()
