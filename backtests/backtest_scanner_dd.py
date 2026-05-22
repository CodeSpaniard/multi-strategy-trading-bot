"""Extended scanner backtest: adds equity-curve style rolling drawdowns.

Reuses run_backtest() from backtest_scanner.py and enriches with 30/60/90-day
rolling drawdown, plus the date-windowed max DD used for the live graduation rule.
"""
from datetime import datetime, timedelta
from backtest_scanner import run_backtest


def rolling_dd(equity_by_date, window_days):
    """equity_by_date: sorted list of (date, equity_pct). Worst drop in any window_days-day window."""
    worst = 0.0
    for i, (end_date, _) in enumerate(equity_by_date):
        window_start = end_date - timedelta(days=window_days)
        window = [(d, e) for d, e in equity_by_date if window_start <= d <= end_date]
        if len(window) < 2:
            continue
        peak = window[0][1]
        local_dd = 0.0
        for _, eq in window:
            peak = max(peak, eq)
            local_dd = min(local_dd, eq - peak)
        worst = min(worst, local_dd)
    return worst


def max_dd(equity_by_date):
    peak = 0.0
    worst = 0.0
    for _, eq in equity_by_date:
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return worst


def build_equity_curve(trades):
    """Build cumulative net-P&L curve by trade exit date.
    This treats P&L as fixed-unit per trade (not compounded), same convention
    as backtest_scanner.py's summarize(). Good enough for drawdown rule-setting.
    """
    sorted_trades = sorted(trades, key=lambda t: t["exit_date"])
    curve = []
    cum = 0.0
    for t in sorted_trades:
        cum += t["net_pnl"]
        curve.append((t["exit_date"], cum))
    return curve


def main():
    print("=" * 72)
    print("SCANNER BACKTEST — dip≥5%, bounce=40% of dip (live config)")
    print("Period: 2024-01-01 → 2025-06-30 (18 months)")
    print("=" * 72, flush=True)
    trades = run_backtest(
        start=datetime(2024, 1, 1), end=datetime(2025, 6, 30),
        dip_threshold_pct=5.0, bounce_ratio=0.40, min_bounce_pct=2.0,
    )
    if not trades:
        print("No trades."); return

    curve = build_equity_curve(trades)
    net = [t["net_pnl"] for t in trades]
    wins = [p for p in net if p > 0]
    total = sum(net)
    print(f"\n  Total trades:     {len(trades)}")
    print(f"  Win rate:         {len(wins) / len(trades) * 100:.1f}%")
    print(f"  Total net P&L:    {total:+.2f}%")
    print(f"  Best trade:       {max(net):+.2f}%")
    print(f"  Worst trade:      {min(net):+.2f}%")
    print(f"  Max DD (trade-seq): {max_dd(curve):+.2f}%")
    print(f"  Worst 30-day DD:  {rolling_dd(curve, 30):+.2f}%")
    print(f"  Worst 60-day DD:  {rolling_dd(curve, 60):+.2f}%")
    print(f"  Worst 90-day DD:  {rolling_dd(curve, 90):+.2f}%")

    # trade count per rolling 30-day window (for rule-sizing context)
    max_in_30 = 0
    for i in range(len(curve)):
        end = curve[i][0]
        start = end - timedelta(days=30)
        cnt = sum(1 for d, _ in curve if start <= d <= end)
        max_in_30 = max(max_in_30, cnt)
    avg_per_30 = len(trades) / max(1, (curve[-1][0] - curve[0][0]).days / 30)
    print(f"\n  Trade cadence:    avg {avg_per_30:.1f} trades / 30-day window, peak {max_in_30}")


if __name__ == "__main__":
    main()
