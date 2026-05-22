"""Daily dip scanner.
Scans a universe of stocks for significant daily dips, scores them,
and returns the top candidates for purchase.
"""
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# S&P 500 large-cap subset — liquid, institutional, mean-reverting.
# Expanded 75-stock universe (2026-04-20): reduced tech concentration from 38% to 25%
# by adding underweight sectors. Backtest vs original 50: +194% vs +132% over 18mo,
# 63% win rate vs 59%, similar drawdown. See backtests/backtest_scanner_expanded.py.
UNIVERSE = [
    # Original 50 names (tech-heavy)
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "V",
    "UNH", "MA", "HD", "PG", "JNJ", "MRK", "ABBV", "CVX", "XOM", "BAC",
    "KO", "PEP", "COST", "AVGO", "TMO", "WMT", "MCD", "CSCO", "ACN", "LIN",
    "AMD", "ADBE", "CRM", "NFLX", "TXN", "INTC", "QCOM", "AMAT", "ISRG", "NOW",
    "GS", "MS", "BLK", "SCHW", "AXP", "C", "DE", "CAT", "BA", "RTX",
    # Utilities (5)
    "NEE", "SO", "DUK", "AEP", "D",
    # Real Estate (3)
    "PLD", "AMT", "CCI",
    # Industrials (6)
    "HON", "UNP", "GE", "LMT", "MMM", "UPS",
    # Energy (3)
    "COP", "SLB", "EOG",
    # Consumer (4)
    "NKE", "SBUX", "DIS", "TGT",
    # Comms / Staples (4)
    "T", "VZ", "CMCSA", "MO",
]


@dataclass
class DipCandidate:
    symbol: str
    dip_pct: float          # how far it dropped today (positive number, e.g. 5.5)
    close_price: float      # today's close price
    bounce_target_pct: float  # dynamic exit target (% gain from entry)
    score: float            # composite score for ranking


def scan_for_dips(broker, dip_threshold_pct: float = 5.0,
                  bounce_ratio: float = 0.40,
                  min_bounce_pct: float = 2.0) -> list[DipCandidate]:
    """Scan universe for today's dips. Returns sorted list of candidates (best first)."""
    candidates = []

    for symbol in UNIVERSE:
        try:
            bars = broker.daily_bars(symbol, 2)
            if bars is None or len(bars) < 2:
                continue

            prev_close = float(bars["close"].iloc[-2])
            today_close = float(bars["close"].iloc[-1])
            change_pct = (today_close - prev_close) / prev_close * 100

            if change_pct <= -dip_threshold_pct:
                dip_depth = abs(change_pct)
                bounce_target = max(dip_depth * bounce_ratio, min_bounce_pct)

                # Score: deeper dips rank higher (more bounce potential)
                score = dip_depth

                candidates.append(DipCandidate(
                    symbol=symbol,
                    dip_pct=dip_depth,
                    close_price=today_close,
                    bounce_target_pct=bounce_target,
                    score=score,
                ))
        except Exception as e:
            log.debug(f"Scanner skip {symbol}: {e}")
            continue

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
