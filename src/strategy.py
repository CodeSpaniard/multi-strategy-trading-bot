from dataclasses import dataclass
from typing import Optional


@dataclass
class Signal:
    action: str  # "BUY", "SELL", "HOLD"
    reason: str
    price: float


class MomentumStrategy:
    """Long-only strategy with two entry modes, shared exit logic.

    Entry modes (set via `mode` param):
      - "breakout" (momentum):       BUY when change_pct >= +threshold
      - "dip" (mean-reversion):      BUY when change_pct <= -threshold

    Exits (identical in both modes):
      - take-profit at +take_profit_pct from entry (hard ceiling)
      - hard stop at -stop_loss_pct from entry (absolute floor)
      - trailing stop at -trailing_stop_pct from high-water mark
    """

    def __init__(
        self,
        momentum_threshold_pct: float,
        take_profit_pct: float,
        stop_loss_pct: float,
        trailing_stop_pct: float,
        mode: str = "breakout",
    ):
        if mode not in ("breakout", "dip"):
            raise ValueError(f"mode must be 'breakout' or 'dip', got {mode!r}")
        self.momentum_threshold_pct = momentum_threshold_pct
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.mode = mode

    def decide(
        self,
        bars,
        holding_qty: float,
        entry_price: Optional[float],
        high_water: Optional[float],
    ) -> Signal:
        if bars is None or len(bars) < 2:
            return Signal("HOLD", "insufficient data", 0.0)

        last_price = float(bars["close"].iloc[-1])
        first_price = float(bars["close"].iloc[0])
        change_pct = (last_price - first_price) / first_price * 100

        if holding_qty > 0 and entry_price:
            pnl_pct = (last_price - entry_price) / entry_price * 100
            hwm = high_water if high_water else entry_price
            hwm_drawdown_pct = (last_price - hwm) / hwm * 100

            if pnl_pct >= self.take_profit_pct:
                return Signal("SELL", f"take-profit hit ({pnl_pct:.2f}%)", last_price)
            if pnl_pct <= -self.stop_loss_pct:
                return Signal("SELL", f"hard stop hit ({pnl_pct:.2f}%)", last_price)
            if hwm_drawdown_pct <= -self.trailing_stop_pct:
                return Signal(
                    "SELL",
                    f"trailing stop hit (hwm {hwm:.2f}, dd {hwm_drawdown_pct:.2f}%, pnl {pnl_pct:.2f}%)",
                    last_price,
                )
            return Signal(
                "HOLD",
                f"holding ({self.mode}), pnl {pnl_pct:.2f}%, hwm {hwm:.2f}, dd {hwm_drawdown_pct:.2f}%",
                last_price,
            )

        if self.mode == "breakout":
            if change_pct >= self.momentum_threshold_pct:
                return Signal("BUY", f"breakout {change_pct:+.2f}% over window", last_price)
        else:  # dip / mean-reversion
            if change_pct <= -self.momentum_threshold_pct:
                return Signal("BUY", f"dip {change_pct:+.2f}% over window", last_price)

        return Signal("HOLD", f"no signal ({self.mode}, momentum {change_pct:+.2f}%)", last_price)
