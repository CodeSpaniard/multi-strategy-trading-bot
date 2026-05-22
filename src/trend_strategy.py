from dataclasses import dataclass
from typing import Optional


@dataclass
class Signal:
    action: str  # "BUY", "SELL", "HOLD"
    reason: str
    price: float


class TrendStrategy:
    """Daily trend following via MA crossover.

    Entry: fast MA crosses above slow MA (golden cross) → BUY
    Exit conditions (checked each tick while holding):
      - take-profit at +take_profit_pct from entry
      - hard stop at -stop_loss_pct from entry
      - trailing stop at -trailing_stop_pct from high-water mark
      - death cross: fast MA crosses below slow MA
    """

    def __init__(self, ma_fast: int, ma_slow: int,
                 take_profit_pct: float, stop_loss_pct: float, trailing_stop_pct: float):
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct

    def decide(self, daily_bars, holding_qty: float,
               entry_price: Optional[float], high_water: Optional[float]) -> Signal:
        if daily_bars is None or len(daily_bars) < self.ma_slow + 1:
            return Signal("HOLD", "insufficient data", 0.0)

        close = daily_bars["close"].astype(float)
        last_price = float(close.iloc[-1])
        ma_f = close.rolling(self.ma_fast).mean()
        ma_s = close.rolling(self.ma_slow).mean()

        fast_now = float(ma_f.iloc[-1])
        slow_now = float(ma_s.iloc[-1])
        fast_prev = float(ma_f.iloc[-2])
        slow_prev = float(ma_s.iloc[-2])

        if holding_qty > 0 and entry_price:
            pnl_pct = (last_price - entry_price) / entry_price * 100
            hwm = high_water if high_water else entry_price
            hwm_dd = (last_price - hwm) / hwm * 100

            if pnl_pct >= self.take_profit_pct:
                return Signal("SELL", f"take-profit hit ({pnl_pct:+.2f}%)", last_price)
            if pnl_pct <= -self.stop_loss_pct:
                return Signal("SELL", f"hard stop hit ({pnl_pct:+.2f}%)", last_price)
            if hwm_dd <= -self.trailing_stop_pct:
                return Signal(
                    "SELL",
                    f"trailing stop (hwm {hwm:.2f}, dd {hwm_dd:+.2f}%, pnl {pnl_pct:+.2f}%)",
                    last_price,
                )
            if fast_now < slow_now and fast_prev >= slow_prev:
                return Signal("SELL", f"death cross (pnl {pnl_pct:+.2f}%)", last_price)

            return Signal(
                "HOLD",
                f"holding (trend), pnl {pnl_pct:+.2f}%, hwm {hwm:.2f}, dd {hwm_dd:+.2f}%, MA {fast_now:.0f}/{slow_now:.0f}",
                last_price,
            )

        # Entry: golden cross
        if fast_now > slow_now and fast_prev <= slow_prev:
            return Signal("BUY", f"golden cross MA{self.ma_fast}/{self.ma_slow} ({fast_now:.0f}>{slow_now:.0f})", last_price)

        trend = "bullish" if fast_now > slow_now else "bearish"
        return Signal("HOLD", f"no cross ({trend}, MA {fast_now:.0f}/{slow_now:.0f}, px {last_price:.2f})", last_price)
