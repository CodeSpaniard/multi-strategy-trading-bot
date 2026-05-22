"""Scanner backtest on EXPANDED universe (75 S&P 500 large-caps).

Adds 25 names to the original 50 across sectors that were underweight:
  - Utilities (5)
  - Real Estate (3)
  - Industrials (6)
  - Energy (3)
  - Consumer/Staples (4)
  - Communications (2)
  - Misc adds (2)

This reduces tech concentration from 38% → 25% of universe.
Runs the same dip-scanner logic as backtest_scanner.py for direct comparison.
"""
import sys
from datetime import datetime

# Import the original backtest module to reuse its functions
import backtest_scanner
from backtest_scanner import run_backtest, summarize
from backtest_scanner_dd import build_equity_curve, max_dd, rolling_dd
from datetime import timedelta


EXPANDED_UNIVERSE = [
    # Original 50 — keep for apples-to-apples
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "V",
    "UNH", "MA", "HD", "PG", "JNJ", "MRK", "ABBV", "CVX", "XOM", "BAC",
    "KO", "PEP", "COST", "AVGO", "TMO", "WMT", "MCD", "CSCO", "ACN", "LIN",
    "AMD", "ADBE", "CRM", "NFLX", "TXN", "INTC", "QCOM", "AMAT", "ISRG", "NOW",
    "GS", "MS", "BLK", "SCHW", "AXP", "C", "DE", "CAT", "BA", "RTX",
    # 25 new additions from underweight sectors
    # Utilities (5) — low-beta, rare dips, but when they dip they bounce clean
    "NEE", "SO", "DUK", "AEP", "D",
    # Real Estate (3)
    "PLD", "AMT", "CCI",
    # Industrials (6)
    "HON", "UNP", "GE", "LMT", "MMM", "UPS",
    # Energy (3)
    "COP", "SLB", "EOG",
    # Consumer (4)
    "NKE", "SBUX", "DIS", "TGT",
    # Comms / Misc (4)
    "T", "VZ", "CMCSA", "MO",
]


def main():
    # Monkey-patch the UNIVERSE
    backtest_scanner.UNIVERSE = EXPANDED_UNIVERSE
    print(f"Running scanner backtest on EXPANDED universe of {len(EXPANDED_UNIVERSE)} stocks")
    print(f"Period: 2024-01-01 → 2025-06-30 (18 months, same as original)")
    print(f"Config: dip≥5%, bounce=40%, hard_stop=8%, trail=5%, max_pos=5, cost=10bps")
    print("=" * 72, flush=True)

    trades = run_backtest(
        start=datetime(2024, 1, 1), end=datetime(2025, 6, 30),
        dip_threshold_pct=5.0, bounce_ratio=0.40, min_bounce_pct=2.0,
    )
    summarize(trades)

    # Equity-curve drawdown stats to match what we had before
    if trades:
        curve = build_equity_curve(trades)
        print(f"\n  Worst 30-day DD:  {rolling_dd(curve, 30):+.2f}%")
        print(f"  Worst 60-day DD:  {rolling_dd(curve, 60):+.2f}%")
        print(f"  Worst 90-day DD:  {rolling_dd(curve, 90):+.2f}%")


if __name__ == "__main__":
    main()
