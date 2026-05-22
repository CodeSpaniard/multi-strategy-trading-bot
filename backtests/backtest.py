"""Minimal backtester for the current long-only momentum strategy.

Design:
- Fetches historical minute bars from Alpaca (free feed, IEX-only)
- Replays the strategy tick-by-tick (same logic as live bot)
- Reports: trade count, win rate, total P&L (%), biggest win/loss, max drawdown

Usage:
    python3 backtest.py --symbol SPY --start 2025-01-01 --end 2025-12-31
    python3 backtest.py --symbol TSLA --start 2025-06-01 --end 2025-09-01

Intentionally simple. No slippage/fee model yet (that's a Phase 2 refinement).
"""
import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from src.strategy import MomentumStrategy


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime = None
    exit_price: float = None
    exit_reason: str = ""

    @property
    def pnl_pct(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100


@dataclass
class Result:
    symbol: str
    trades: list = field(default_factory=list)
    total_bars: int = 0

    def summary(self):
        closed = [t for t in self.trades if t.exit_price is not None]
        if not closed:
            return f"{self.symbol}: no closed trades across {self.total_bars} bars"
        pnls = [t.pnl_pct for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total = sum(pnls)
        # Max drawdown on cumulative P&L
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        out = []
        out.append(f"=== {self.symbol} ({self.total_bars} bars) ===")
        out.append(f"  Trades:       {len(closed)}")
        out.append(f"  Wins/Losses:  {len(wins)} / {len(losses)}  (win rate: {len(wins)/len(closed)*100:.1f}%)")
        out.append(f"  Total P&L:    {total:+.2f}%")
        out.append(f"  Avg per trade: {total/len(closed):+.3f}%")
        out.append(f"  Best trade:   {max(pnls):+.2f}%")
        out.append(f"  Worst trade:  {min(pnls):+.2f}%")
        out.append(f"  Max drawdown: {max_dd:.2f}%")
        # Exit reason breakdown
        by_reason = {}
        for t in closed:
            by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
        out.append(f"  Exits by reason: {by_reason}")
        return "\n".join(out)


def fetch_bars(client, symbol: str, start: datetime, end: datetime):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
    )
    df = client.get_stock_bars(req).df
    if df.empty:
        return None
    if "symbol" in df.index.names:
        df = df.xs(symbol, level="symbol")
    return df


def backtest(symbol: str, start: datetime, end: datetime,
             lookback_min: int = 20,
             momentum_threshold_pct: float = 0.3,
             take_profit_pct: float = 0.5,
             stop_loss_pct: float = 0.3,
             trailing_stop_pct: float = 0.2) -> Result:
    load_dotenv(".env.paper", override=True)
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    print(f"Fetching {symbol} minute bars from {start.date()} to {end.date()}...")
    df = fetch_bars(client, symbol, start, end)
    if df is None:
        return Result(symbol=symbol)
    print(f"  Got {len(df)} bars.")

    strat = MomentumStrategy(
        momentum_threshold_pct=momentum_threshold_pct,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        trailing_stop_pct=trailing_stop_pct,
    )

    result = Result(symbol=symbol, total_bars=len(df))
    current: Trade = None
    hwm: float = 0.0

    # Walk forward bar by bar. At bar i, lookback window is [i-lookback_min+1 .. i].
    for i in range(lookback_min, len(df)):
        window = df.iloc[i - lookback_min:i + 1]
        bar_time = window.index[-1]
        last_price = float(window["close"].iloc[-1])

        if current is not None:
            hwm = max(hwm, last_price)

        sig = strat.decide(
            window,
            holding_qty=1.0 if current is not None else 0.0,
            entry_price=current.entry_price if current else None,
            high_water=hwm if current else None,
        )

        if sig.action == "BUY" and current is None:
            current = Trade(symbol=symbol, entry_time=bar_time, entry_price=last_price)
            hwm = last_price
            result.trades.append(current)
        elif sig.action == "SELL" and current is not None:
            current.exit_time = bar_time
            current.exit_price = last_price
            current.exit_reason = sig.reason.split()[0] + "-" + sig.reason.split()[1] if len(sig.reason.split()) >= 2 else sig.reason
            current = None
            hwm = 0.0

    # Close any dangling position at last bar (end-of-period flatten)
    if current is not None:
        last_bar = df.iloc[-1]
        current.exit_time = df.index[-1]
        current.exit_price = float(last_bar["close"])
        current.exit_reason = "end-of-period-flatten"

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2025-03-31")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    result = backtest(args.symbol, start, end)
    print()
    print(result.summary())


if __name__ == "__main__":
    main()
