"""BTC dip strategy with regime filter.

Filter logic: only enter a dip if price is not extended above its long-term MA.
Specifically: require (last_price / ma_200h) <= extension_max.

extension_max = 1.00 means only buy when at or below the 200h MA (deep value)
extension_max = 1.10 means only buy when not more than 10% above MA (ranging-ish)
extension_max = 999 effectively disables the filter

Rationale: in strong uptrends, price stays well above MA and dip-buying catches
shallow pullbacks that immediately resume trending — generating losses.
In ranging markets, price oscillates around MA and dips are meaningful.
"""
import sys
from datetime import datetime
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest


def backtest_with_regime(symbol, start, end,
                         lookback_min=6,
                         threshold_pct=3.0,
                         take_profit_pct=6.0,
                         stop_loss_pct=3.0,
                         trailing_stop_pct=2.5,
                         ma_window=200,
                         extension_max=1.10,   # price / MA must be <= this to enter
                         cost_bps_per_trade=60,
                         timeframe=TimeFrame.Hour):
    load_dotenv(".env.paper", override=True)
    client = CryptoHistoricalDataClient()
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end)
    df = client.get_crypto_bars(req).df
    if df.empty: return None
    if "symbol" in df.index.names: df = df.xs(symbol, level="symbol")

    close = df["close"]
    ma = close.rolling(window=ma_window, min_periods=ma_window).mean()

    trades = []
    current = None
    hwm = 0.0

    start_idx = max(lookback_min, ma_window)
    for i in range(start_idx, len(df)):
        window = df.iloc[i - lookback_min:i + 1]
        last_price = float(window["close"].iloc[-1])
        first_price = float(window["close"].iloc[0])
        change_pct = (last_price - first_price) / first_price * 100
        bar_time = window.index[-1]
        ma_val = ma.iloc[i]

        if current is not None:
            hwm = max(hwm, last_price)
            pnl_pct = (last_price - current["entry_price"]) / current["entry_price"] * 100
            hwm_dd = (last_price - hwm) / hwm * 100
            if pnl_pct >= take_profit_pct:
                current["exit_price"] = last_price; current["exit_reason"] = "tp"; current = None; hwm = 0.0
            elif pnl_pct <= -stop_loss_pct:
                current["exit_price"] = last_price; current["exit_reason"] = "sl"; current = None; hwm = 0.0
            elif hwm_dd <= -trailing_stop_pct:
                current["exit_price"] = last_price; current["exit_reason"] = "trail"; current = None; hwm = 0.0
        else:
            if change_pct <= -threshold_pct:
                # Regime filter
                extension = last_price / ma_val if ma_val > 0 else 999
                if extension <= extension_max:
                    current = {"entry_price": last_price, "entry_time": bar_time, "exit_price": None, "extension": extension}
                    hwm = last_price
                    trades.append(current)

    if current is not None and current["exit_price"] is None:
        current["exit_price"] = float(df["close"].iloc[-1])
        current["exit_reason"] = "eof"

    closed = [t for t in trades if t["exit_price"] is not None]
    if not closed:
        return {"trades": 0, "bars": len(df)}
    gross_pnls = [(t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100 for t in closed]
    net_pnls = [p - cost_bps_per_trade / 100 for p in gross_pnls]
    wins = [p for p in net_pnls if p > 0]
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in net_pnls:
        cum += p; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
    return {
        "trades": len(closed),
        "bars": len(df),
        "win_rate": len(wins) / len(closed) * 100,
        "gross_pl": sum(gross_pnls),
        "net_pl": sum(net_pnls),
        "max_dd": max_dd,
    }


def main():
    print("BTC/USD dip strategy + regime filter")
    print("Baseline config: thr=3%, TP=6%, SL=3%, trail=2.5%, cost=60bps")
    print()
    print(f"{'PERIOD':<25} {'FILTER':<12} {'TRADES':>6}  {'WIN%':>5}  {'GROSS':>8}  {'NET':>8}  {'DD':>8}")
    print("-" * 90)

    periods = [
        ("2023", datetime(2023, 1, 1), datetime(2023, 12, 31)),
        ("Jan-May 2024 (BAD)", datetime(2024, 1, 1), datetime(2024, 5, 31)),
        ("Jun2024-Jun2025", datetime(2024, 6, 1), datetime(2025, 6, 30)),
        ("Jul-Dec 2025", datetime(2025, 7, 1), datetime(2025, 12, 31)),
    ]

    # Test three filter strengths
    for label, start, end in periods:
        for ext_label, ext_max in [("none(>>1)", 999.0), ("<1.10 (mild)", 1.10), ("<1.00 (strict)", 1.00)]:
            r = backtest_with_regime("BTC/USD", start, end, extension_max=ext_max)
            if r is None or r.get("trades", 0) == 0:
                print(f"{label:<25} {ext_label:<12}   0  (no trades)")
                continue
            print(f"{label:<25} {ext_label:<12} {r['trades']:>5}  {r['win_rate']:>5.1f}%  {r['gross_pl']:>+6.2f}%  {r['net_pl']:>+6.2f}%  {r['max_dd']:>+6.2f}%", flush=True)
        print()


if __name__ == "__main__":
    main()
