"""Volatility breakout backtester for crypto.

Strategy:
1. Compute rolling volatility (std dev of hourly returns over `vol_window` hours)
2. Identify "squeeze": current vol < Xth percentile of vol over last `lookback` hours
3. When in squeeze AND price breaks above recent range by `breakout_pct` → BUY
4. Exit: trailing stop, take-profit, hard stop (same mechanics)

Fundamentally different from momentum/mean-reversion: signal is volatility regime, not price direction.
"""
import sys
import numpy as np
from datetime import datetime
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest


def backtest_volbreak(symbol, start, end,
                      vol_window=24,          # hours to measure current volatility
                      vol_lookback=200,       # hours to compute percentile
                      squeeze_percentile=20,  # current vol must be below this percentile
                      breakout_pct=2.0,       # price must rise X% from squeeze-period low to trigger
                      take_profit_pct=6.0,
                      stop_loss_pct=3.0,
                      trailing_stop_pct=2.5,
                      cost_bps_per_trade=60,
                      timeframe=TimeFrame.Hour):
    load_dotenv(".env.paper", override=True)
    client = CryptoHistoricalDataClient()
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end)
    df = client.get_crypto_bars(req).df
    if df.empty: return None
    if "symbol" in df.index.names: df = df.xs(symbol, level="symbol")

    close = df["close"].astype(float)
    returns = close.pct_change()
    rolling_vol = returns.rolling(window=vol_window).std()

    trades = []
    current = None
    hwm = 0.0
    squeeze_low = None
    in_squeeze = False

    start_idx = max(vol_lookback, vol_window) + 1
    for i in range(start_idx, len(df)):
        last_price = float(close.iloc[i])
        cur_vol = rolling_vol.iloc[i]
        vol_history = rolling_vol.iloc[i - vol_lookback:i].dropna()

        if len(vol_history) < 50 or np.isnan(cur_vol):
            continue

        vol_pctile = (vol_history < cur_vol).sum() / len(vol_history) * 100

        if current is not None:
            hwm = max(hwm, last_price)
            pnl_pct = (last_price - current["entry"]) / current["entry"] * 100
            hwm_dd = (last_price - hwm) / hwm * 100
            if pnl_pct >= take_profit_pct:
                current["exit"] = last_price; current["reason"] = "tp"; current = None; hwm = 0.0
                in_squeeze = False; squeeze_low = None
            elif pnl_pct <= -stop_loss_pct:
                current["exit"] = last_price; current["reason"] = "sl"; current = None; hwm = 0.0
                in_squeeze = False; squeeze_low = None
            elif hwm_dd <= -trailing_stop_pct:
                current["exit"] = last_price; current["reason"] = "trail"; current = None; hwm = 0.0
                in_squeeze = False; squeeze_low = None
        else:
            # Squeeze detection
            if vol_pctile <= squeeze_percentile:
                if not in_squeeze:
                    in_squeeze = True
                    squeeze_low = last_price
                else:
                    squeeze_low = min(squeeze_low, last_price)
            else:
                if in_squeeze and squeeze_low is not None:
                    # Was in squeeze, vol is now expanding — check for upside breakout
                    rise_from_low = (last_price - squeeze_low) / squeeze_low * 100
                    if rise_from_low >= breakout_pct:
                        current = {"entry": last_price, "exit": None, "reason": ""}
                        hwm = last_price
                        trades.append(current)
                in_squeeze = False
                squeeze_low = None

    if current is not None and current["exit"] is None:
        current["exit"] = float(close.iloc[-1]); current["reason"] = "eof"

    closed = [t for t in trades if t["exit"] is not None]
    if not closed:
        return {"trades": 0, "bars": len(df)}
    gross = [(t["exit"] - t["entry"]) / t["entry"] * 100 for t in closed]
    net = [p - cost_bps_per_trade / 100 for p in gross]
    wins = [p for p in net if p > 0]
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in net:
        cum += p; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
    reasons = {}
    for t in closed: reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "trades": len(closed), "bars": len(df),
        "win_rate": len(wins) / len(closed) * 100 if closed else 0,
        "gross_pl": sum(gross), "net_pl": sum(net), "max_dd": max_dd,
        "exits": reasons,
    }


def main():
    print("VOLATILITY BREAKOUT — BTC/USD hourly")
    print("Signal: low-vol squeeze → upside breakout")
    print("Cost: 60 bps round-trip (Alpaca reality)")
    print()
    print(f"{'PERIOD':<25} {'BREAKOUT%':<10} {'TRADES':>6}  {'WIN%':>5}  {'GROSS':>8}  {'NET':>8}  {'DD':>8}  EXITS")
    print("-" * 105)

    periods = [
        ("2023", datetime(2023, 1, 1), datetime(2023, 12, 31)),
        ("Jan-May 2024", datetime(2024, 1, 1), datetime(2024, 5, 31)),
        ("Jun2024-Jun2025", datetime(2024, 6, 1), datetime(2025, 6, 30)),
        ("Jul-Dec 2025", datetime(2025, 7, 1), datetime(2025, 12, 31)),
    ]

    for bo_pct in [1.0, 2.0, 3.0, 5.0]:
        for label, start, end in periods:
            r = backtest_volbreak("BTC/USD", start, end, breakout_pct=bo_pct)
            if r is None or r.get("trades", 0) == 0:
                print(f"{label:<25} {bo_pct:<10}   0  (no trades)")
                continue
            exits = " ".join(f"{k}:{v}" for k, v in r["exits"].items())
            print(f"{label:<25} {bo_pct:<10} {r['trades']:>5}  {r['win_rate']:>5.1f}%  {r['gross_pl']:>+6.2f}%  {r['net_pl']:>+6.2f}%  {r['max_dd']:>+6.2f}%  {exits}", flush=True)
        print()


if __name__ == "__main__":
    main()
