"""Automated position sizer.

Decides per-trade $ size based on:
  - equity-proportional base (equity / divisor)
  - training-wheels CAP for first N closed trades (hard ceiling regardless of equity)
  - per-trade growth cap (prior_size * max_growth_ratio) — one hot trade can't 2x you
  - 7-day drawdown circuit breaker (freezes size for N days if breached)
  - manual kill-switch

Pure functions. State is passed in, decision is returned. No I/O.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class SizingDecision:
    per_trade_usd: float
    reason: str
    new_frozen_until: Optional[str] = None  # ISO timestamp if freeze triggered this call


@dataclass
class SizerConfig:
    divisor: int               # equity / divisor = base size (5 for scanner, 2 for crypto)
    training_wheel_cap_usd: float  # hard ceiling for first N closed trades
    training_wheel_trades: int = 20
    max_growth_ratio: float = 1.5  # per-buy cap: size <= prior_size * this
    dd_threshold_pct: float = 10.0  # 7-day peak-to-trough DD % that triggers freeze (as positive number)
    dd_lookback_days: int = 7
    freeze_days: int = 7
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "SizerConfig":
        return cls(
            divisor=d["divisor"],
            training_wheel_cap_usd=d["training_wheel_cap_usd"],
            training_wheel_trades=d.get("training_wheel_trades", 20),
            max_growth_ratio=d.get("max_growth_ratio", 1.5),
            dd_threshold_pct=d.get("dd_threshold_pct", 10.0),
            dd_lookback_days=d.get("dd_lookback_days", 7),
            freeze_days=d.get("freeze_days", 7),
            enabled=d.get("enabled", True),
        )


def compute_drawdown_pct(samples: list) -> float:
    """samples: list of (datetime, float). Returns worst peak-to-trough drawdown as a
    NEGATIVE percentage (e.g., -8.5 means equity dropped 8.5% from peak).
    Caller is responsible for passing only the relevant time window.
    """
    if len(samples) < 2:
        return 0.0
    sorted_samples = sorted(samples, key=lambda x: x[0])
    peak = sorted_samples[0][1]
    worst = 0.0
    for _, v in sorted_samples:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100 if peak > 0 else 0.0
        worst = min(worst, dd)
    return worst


def decide_size(
    cfg: SizerConfig,
    equity: float,
    closed_trades: int,
    equity_history: list,  # list of (datetime, equity_value)
    prior_size: float,
    frozen_until: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> SizingDecision:
    """Return the per-trade $ size to use for the next buy.

    equity_history: samples of account equity over time, oldest→newest.
    prior_size: the last per-trade size used (0.0 if no prior).
    frozen_until: if set and in the future, size is held at prior_size.
    """
    now = now or datetime.now()

    if not cfg.enabled:
        return SizingDecision(0.0, "scaling_disabled_by_config")

    if equity <= 0:
        return SizingDecision(0.0, f"equity=${equity:.2f} — nothing to size against")

    # 1. Check existing freeze
    if frozen_until and now < frozen_until:
        base_size = prior_size if prior_size > 0 else equity / cfg.divisor
        return SizingDecision(base_size, f"frozen_until={frozen_until.isoformat()}")

    # 2. Check 7-day drawdown breach → new freeze
    cutoff = now - timedelta(days=cfg.dd_lookback_days)
    recent = [(t, v) for t, v in equity_history if t >= cutoff]
    dd_pct = compute_drawdown_pct(recent)
    if dd_pct < -cfg.dd_threshold_pct:
        new_freeze = now + timedelta(days=cfg.freeze_days)
        base_size = prior_size if prior_size > 0 else equity / cfg.divisor
        return SizingDecision(
            base_size,
            f"DD_breach dd={dd_pct:.2f}% threshold=-{cfg.dd_threshold_pct}% freezing_until={new_freeze.isoformat()}",
            new_frozen_until=new_freeze.isoformat(),
        )

    # 3. Base proportional size
    raw = equity / cfg.divisor

    # 4. Training wheels — hard cap for first N trades
    if closed_trades < cfg.training_wheel_trades:
        raw = min(raw, cfg.training_wheel_cap_usd)

    # 5. Per-trade growth cap
    if prior_size > 0:
        raw = min(raw, prior_size * cfg.max_growth_ratio)

    reason_parts = [f"equity/{cfg.divisor}={equity / cfg.divisor:.2f}"]
    if closed_trades < cfg.training_wheel_trades:
        reason_parts.append(f"tw_cap=${cfg.training_wheel_cap_usd:.0f}({closed_trades}/{cfg.training_wheel_trades}trades)")
    if prior_size > 0:
        reason_parts.append(f"growth_cap={prior_size * cfg.max_growth_ratio:.2f}")
    return SizingDecision(raw, " ".join(reason_parts))
