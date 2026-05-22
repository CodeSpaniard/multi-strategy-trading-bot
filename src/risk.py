from datetime import date

# FINRA PDT threshold — below this equity, must stay under 4 day trades in 5 business days
PDT_EQUITY_THRESHOLD = 25000.0
# Leave 1 trade of headroom so we can always close the current position
PDT_SAFE_TRADES_PER_DAY = 3


class RiskManager:
    def __init__(self, starting_equity: float, max_position_usd: float,
                 daily_loss_limit_usd: float, max_trades_per_day: int):
        self.starting_equity = starting_equity
        self.max_position_usd = max_position_usd
        self.daily_loss_limit_usd = daily_loss_limit_usd
        self.max_trades_per_day = max_trades_per_day
        self.trades_today = 0
        self.today = date.today()

    def _reset_if_new_day(self):
        if date.today() != self.today:
            self.today = date.today()
            self.trades_today = 0

    def can_trade(self, current_equity: float) -> tuple[bool, str]:
        self._reset_if_new_day()
        loss = self.starting_equity - current_equity
        if loss >= self.daily_loss_limit_usd:
            return False, f"daily loss limit hit (${loss:.2f})"

        # Apply PDT cap when under $25k equity, regardless of config
        effective_cap = self.max_trades_per_day
        if current_equity < PDT_EQUITY_THRESHOLD:
            effective_cap = min(effective_cap, PDT_SAFE_TRADES_PER_DAY)

        if self.trades_today >= effective_cap:
            reason = "max trades/day"
            if effective_cap < self.max_trades_per_day:
                reason = f"PDT-safe limit (equity ${current_equity:.2f} < $25k)"
            return False, f"{reason} reached ({self.trades_today}/{effective_cap})"
        return True, "ok"

    def record_trade(self):
        self.trades_today += 1

    def position_size_usd(self) -> float:
        return self.max_position_usd
