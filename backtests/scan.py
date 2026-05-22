"""Read-only scanner: prints current 20-min momentum for a watchlist,
using BOTH paper and live credentials (same market data feed either way,
but shown explicitly so both accounts are acknowledged).
Does NOT place trades. Safe to run anytime alongside the live bot.

Usage:
    python3 scan.py SPY QQQ IWM SMH XLE TSLA NVDA AAPL MSFT AMD
"""
import sys
from dotenv import load_dotenv
from src.broker import Broker

LOOKBACK_MIN = 20


def scan(broker, symbols):
    rows = []
    for sym in symbols:
        try:
            bars = broker.recent_bars(sym, LOOKBACK_MIN)
            if bars is None or len(bars) < 2:
                rows.append((sym, None, None, "no data"))
                continue
            last = float(bars["close"].iloc[-1])
            first = float(bars["close"].iloc[0])
            chg = (last - first) / first * 100
            rows.append((sym, last, chg, ""))
        except Exception as e:
            rows.append((sym, None, None, f"err: {e}"))
    rows.sort(key=lambda r: (r[2] if r[2] is not None else -999), reverse=True)
    return rows


def print_rows(rows, label):
    print(f"\n=== {label} ===")
    print(f"{'SYMBOL':<6} {'PRICE':>10} {'20min %':>10}   SIGNAL")
    print("-" * 50)
    for sym, px, chg, note in rows:
        if chg is None:
            print(f"{sym:<6} {'—':>10} {'—':>10}   {note}")
            continue
        signal = "BUY" if chg >= 0.30 else ("short-cand" if chg <= -0.30 else "hold")
        print(f"{sym:<6} {px:>10.2f} {chg:>+9.2f}%   {signal}")


def main():
    symbols = sys.argv[1:] or ["SPY", "QQQ", "IWM", "SMH", "XLE", "TSLA", "NVDA", "AAPL", "MSFT", "AMD"]

    load_dotenv(".env.paper", override=True)
    paper = Broker(paper=True)
    print(f"PAPER equity: ${paper.account_equity():,.2f}")

    load_dotenv(".env.live", override=True)
    live = Broker(paper=False)
    print(f"LIVE  equity: ${live.account_equity():,.2f}")

    # Market data identical on both keys — scan once, label twice for transparency
    rows = scan(paper, symbols)
    print_rows(rows, "20-min momentum (identical on paper & live data feeds)")


if __name__ == "__main__":
    main()
