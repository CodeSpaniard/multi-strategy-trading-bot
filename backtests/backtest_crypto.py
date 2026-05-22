"""Crypto backtester — both momentum (breakout) and mean-reversion (dip) modes.
Same exit logic as equity version: take-profit, hard-stop, trailing-stop.

Usage:
    python3 backtest_crypto.py --symbol BTC/USD --mode dip --threshold 1.0 --start 2024-01-01 --end 2024-12-31
    python3 backtest_crypto.py --sweep
"""
import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime
from dotenv import load_dotenv
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
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


def fetch_bars(client, symbol, start, end, timeframe=TimeFrame.Minute):
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end)
    df = client.get_crypto_bars(req).df
    if df.empty: return None
    if "symbol" in df.index.names: df = df.xs(symbol, level="symbol")
    return df


def backtest(symbol, start, end, mode,
             lookback_min=20,
             threshold_pct=1.0,
             take_profit_pct=2.0,
             stop_loss_pct=1.5,
             trailing_stop_pct=1.0,
             cost_bps_per_trade=20,
             timeframe=TimeFrame.Minute):
    """cost_bps_per_trade: round-trip cost in basis points (20 bps = 0.2%)"""
    load_dotenv(".env.paper", override=True)
    client = CryptoHistoricalDataClient()  # no auth needed for crypto public data
    df = fetch_bars(client, symbol, start, end, timeframe=timeframe)
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
            triggered = (change_pct >= threshold_pct) if mode == "breakout" else (change_pct <= -threshold_pct)
            if triggered:
                current = Trade(symbol=symbol, entry_time=bar_time, entry_price=last_price)
                hwm = last_price
                trades.append(current)

    if current is not None:
        current.exit_time = df.index[-1]; current.exit_price = float(df["close"].iloc[-1]); current.exit_reason = "eof"

    closed = [t for t in trades if t.exit_price is not None]
    if not closed:
        return {"symbol": symbol, "mode": mode, "threshold": threshold_pct, "trades": 0, "bars": len(df)}

    gross_pnls = [t.pnl_pct for t in closed]
    net_pnls = [p - cost_bps_per_trade / 100 for p in gross_pnls]  # subtract cost per round-trip
    wins = [p for p in net_pnls if p > 0]
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in net_pnls:
        cum += p; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
    return {
        "symbol": symbol,
        "mode": mode,
        "threshold": threshold_pct,
        "trades": len(closed),
        "bars": len(df),
        "win_rate": len(wins) / len(closed) * 100,
        "gross_pl": sum(gross_pnls),
        "net_pl": sum(net_pnls),
        "max_dd": max_dd,
        "best": max(net_pnls),
        "worst": min(net_pnls),
    }


def format_row(r):
    if r is None or r.get("trades", 0) == 0:
        return f"{r['symbol']:<10} {r['mode']:<8} thr={r['threshold']:<5} (no trades)"
    return (f"{r['symbol']:<10} {r['mode']:<8} thr={r['threshold']:<5} "
            f"{r['trades']:>5} tr  {r['win_rate']:>5.1f}%  "
            f"gross:{r['gross_pl']:>+7.2f}% net:{r['net_pl']:>+7.2f}% "
            f"dd:{r['max_dd']:>+7.2f}%")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC/USD")
    parser.add_argument("--mode", default="dip", choices=["breakout", "dip"])
    parser.add_argument("--threshold", type=float, default=1.0)
    parser.add_argument("--start", default="2024-06-01")
    parser.add_argument("--end", default="2025-06-30")
    parser.add_argument("--sweep", action="store_true", help="sweep over symbols x modes x thresholds")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)

    if args.sweep:
        print(f"Sweeping {args.start} to {args.end}  (20 bps round-trip cost included)")
        print()
        print(f"{'SYMBOL':<10} {'MODE':<8} {'THR':<9} {'TRADES':>8}  {'WIN%':>5}  {'GROSS':>12}  {'NET':>12}  {'DD':>9}")
        print("-" * 100)
        for sym in ["BTC/USD", "ETH/USD"]:
            for mode in ["breakout", "dip"]:
                for thr in [0.5, 1.0, 2.0]:
                    r = backtest(sym, start, end, mode, threshold_pct=thr)
                    print(format_row(r))
    else:
        r = backtest(args.symbol, start, end, args.mode, threshold_pct=args.threshold)
        print(format_row(r))


if __name__ == "__main__":
    main()
