"""Mean-reversion variant backtester.
Same infra as backtest.py, but inverted signal:
- BUY when momentum <= -threshold (buy the dip)
- TP at +take_profit_pct, hard stop at -stop_loss_pct from entry
- Trailing stop still locks in gains as price rises after entry
"""
import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


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


def fetch_bars(client, symbol, start, end):
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end)
    df = client.get_stock_bars(req).df
    if df.empty: return None
    if "symbol" in df.index.names: df = df.xs(symbol, level="symbol")
    return df


def backtest_meanrev(symbol, start, end,
                     lookback_min=20,
                     dip_threshold_pct=0.30,   # buy when momentum <= -this
                     take_profit_pct=0.50,
                     stop_loss_pct=0.30,
                     trailing_stop_pct=0.20):
    load_dotenv(".env.paper", override=True)
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    df = fetch_bars(client, symbol, start, end)
    if df is None: return None

    trades = []
    current = None
    hwm = 0.0

    for i in range(lookback_min, len(df)):
        window = df.iloc[i - lookback_min:i + 1]
        last_price = float(window["close"].iloc[-1])
        first_price = float(window["close"].iloc[0])
        change_pct = (last_price - first_price) / first_price * 100
        bar_time = window.index[-1]

        if current is not None:
            hwm = max(hwm, last_price)
            pnl_pct = (last_price - current.entry_price) / current.entry_price * 100
            hwm_dd = (last_price - hwm) / hwm * 100
            # exits (same as momentum strategy)
            if pnl_pct >= take_profit_pct:
                current.exit_time = bar_time; current.exit_price = last_price; current.exit_reason = "take-profit"
                current = None; hwm = 0.0
            elif pnl_pct <= -stop_loss_pct:
                current.exit_time = bar_time; current.exit_price = last_price; current.exit_reason = "hard-stop"
                current = None; hwm = 0.0
            elif hwm_dd <= -trailing_stop_pct:
                current.exit_time = bar_time; current.exit_price = last_price; current.exit_reason = "trailing-stop"
                current = None; hwm = 0.0
        else:
            # INVERTED entry: buy on dip
            if change_pct <= -dip_threshold_pct:
                current = Trade(symbol=symbol, entry_time=bar_time, entry_price=last_price)
                hwm = last_price
                trades.append(current)

    if current is not None:
        current.exit_time = df.index[-1]; current.exit_price = float(df["close"].iloc[-1]); current.exit_reason = "eof"

    closed = [t for t in trades if t.exit_price is not None]
    if not closed:
        return {"symbol": symbol, "trades": 0, "bars": len(df)}

    pnls = [t.pnl_pct for t in closed]
    wins = [p for p in pnls if p > 0]
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
    return {
        "symbol": symbol,
        "trades": len(closed),
        "bars": len(df),
        "win_rate": len(wins) / len(closed) * 100,
        "total_pl": sum(pnls),
        "max_dd": max_dd,
        "best": max(pnls),
        "worst": min(pnls),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM", "SMH", "TSLA", "AAPL"])
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2025-03-31")
    args = parser.parse_args()
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)

    print(f"{'SYM':<6} {'TRADES':>7} {'WIN%':>6} {'P&L':>8} {'DD':>8}")
    print("-" * 42)
    for sym in args.symbols:
        r = backtest_meanrev(sym, start, end)
        if r is None or r.get("trades", 0) == 0:
            print(f"{sym:<6} (no trades)")
            continue
        print(f"{sym:<6} {r['trades']:>7} {r['win_rate']:>5.1f}% {r['total_pl']:>+7.2f}% {r['max_dd']:>+7.2f}%")


if __name__ == "__main__":
    main()
