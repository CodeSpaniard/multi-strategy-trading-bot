"""Shared order-fill result for both broker adapters.

Returned from broker buy/sell methods after fill verification. Callers use
fill_price for accurate entry/exit recording (vs. the scan-time price, which
can be 0-60s stale by the time the order actually fills).
"""
from dataclasses import dataclass


@dataclass
class FillResult:
    fill_price: float       # avg actual fill price; 0.0 if no fill recorded
    filled_qty: float       # actual filled quantity (base units; or notional $ when broker reports that way)
    fully_filled: bool      # True if status is terminal-FILLED; False if partial at timeout
    order_id: str           # broker order id, useful for reconciliation / debug


def realized_pnl_usd(entry_price: float, exit_price: float, filled_qty: float) -> float:
    """Realized dollar P&L for a closed position, from the quantity actually filled
    on the exit.

    Deliberately NOT `last_size_usd * pnl_pct`, which is what both bots used
    before. That scales by the CURRENT sizing decision rather than by what was
    actually invested in this position, so it drifts whenever size has moved since
    entry — training wheels lifting, the growth cap, or equity changing. It is
    correct only by coincidence, and the coincidence is strongest right after a
    position is opened and weakest for long holds, which is exactly backwards.

    Excludes fees: FillResult carries none today. Alpaca equities are
    commission-free, so this is exact for the scanner. Coinbase charges real fees,
    so crypto figures are optimistic by roughly the fee until FillResult can carry
    them.
    """
    return (exit_price - entry_price) * filled_qty
