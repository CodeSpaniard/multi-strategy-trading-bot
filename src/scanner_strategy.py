"""Scanner strategy — manages positions opened by the dip scanner.

Entry: handled by scanner.py (scans for dips, bot buys candidates)
Exit: this class evaluates each holding and decides when to sell.

Exit conditions:
  - bounce_target: price recovered X% of dip (dynamic per position)
  - trailing_stop: price fell Y% from high-water mark since entry
  - hard_stop: price fell Z% from entry (absolute floor)
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Signal:
    action: str  # "BUY", "SELL", "HOLD"
    reason: str
    price: float


class ScannerStrategy:
    def __init__(self, hard_stop_pct: float = 8.0, trailing_stop_pct: float = 5.0):
        self.hard_stop_pct = hard_stop_pct
        self.trailing_stop_pct = trailing_stop_pct

    def check_exit(self, current_price: float, entry_price: float,
                   high_water: float, bounce_target_pct: float) -> Signal:
        """Check if a held position should exit."""
        pnl_pct = (current_price - entry_price) / entry_price * 100
        hwm = max(high_water, entry_price)
        hwm_dd = (current_price - hwm) / hwm * 100

        if pnl_pct >= bounce_target_pct:
            return Signal("SELL", f"bounce-target hit ({pnl_pct:+.2f}% vs target {bounce_target_pct:+.2f}%)", current_price)

        if pnl_pct <= -self.hard_stop_pct:
            return Signal("SELL", f"hard stop hit ({pnl_pct:+.2f}%)", current_price)

        if hwm_dd <= -self.trailing_stop_pct:
            return Signal("SELL", f"trailing stop (hwm {hwm:.2f}, dd {hwm_dd:+.2f}%, pnl {pnl_pct:+.2f}%)", current_price)

        return Signal("HOLD", f"pnl {pnl_pct:+.2f}%, hwm {hwm:.2f}, dd {hwm_dd:+.2f}%, target {bounce_target_pct:+.2f}%", current_price)
