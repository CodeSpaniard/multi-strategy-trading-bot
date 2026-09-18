"""Crypto daily-trend bot using Coinbase.
Same TrendStrategy logic as Alpaca version, different broker.
Checks once per hour for daily MA crossovers.

Per-trade size computed by src.sizer.
"""
import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import yaml
from dotenv import load_dotenv

from src.coinbase_broker import CoinbaseBroker, CoinbaseBrokerError
from src.daily_summary import write_crypto_summary
from src.healthcheck import ping_healthcheck
from src.logger import get_logger, state_path as state_path_for
from src.notifier import notify_buy, notify_sell, notify_freeze, notify_error, notify_milestone
from src.restart_tracker import track_restart_and_alert
from src.fill import realized_pnl_usd
from src.sizer import SizerConfig, decide_size
from src.atomic_io import write_json_atomic
from src.trend_strategy import TrendStrategy


EQUITY_SAMPLE_RETENTION_DAYS = 14
DUST_THRESHOLD_USD = 10.0  # untracked positions worth less than this are treated as empty


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict):
    write_json_atomic(path, state, default=str)


def parse_equity_samples(raw) -> list:
    out = []
    for item in raw or []:
        try:
            out.append((datetime.fromisoformat(item[0]), float(item[1])))
        except Exception:
            continue
    return out


def serialize_equity_samples(samples: list) -> list:
    return [[ts.isoformat(), v] for ts, v in samples]


def prune_equity_samples(samples: list, retention_days: int, now: datetime) -> list:
    cutoff = now - timedelta(days=retention_days)
    return [(t, v) for t, v in samples if t >= cutoff]


def reconcile_positions(broker, symbols, entry_prices, high_water_marks,
                        entry_order_ids, log, log_name) -> list:
    """Compare in-memory entry_prices against broker's held qty for the configured symbols.

    Returns a list of human-readable change strings; empty means clean.
    Adopts symbols held on Coinbase but missing in-memory (uses current price
    as a stand-in entry since we have no history). Drops in-memory symbols
    that Coinbase no longer reports (dust threshold applies).

    All three state dicts (entry_prices, high_water_marks, entry_order_ids)
    are kept in sync on every adopt/drop so no residue survives reconcile.
    Adopted symbols have no known order_id; the key is explicitly removed
    rather than left to dangle from a prior buy.

    Caller surfaces a non-empty change list via Pushover.
    """
    changes: list = []
    for symbol in symbols:
        try:
            qty = broker.get_position_qty(symbol)
        except CoinbaseBrokerError as e:
            log.error(f"[{log_name}] RECONCILE qty fetch failed for {symbol}: {e}")
            continue
        in_memory = symbol in entry_prices

        try:
            current_price = broker.get_current_price(symbol)
        except CoinbaseBrokerError:
            current_price = 0.0

        # dust check: same threshold as elsewhere in this module
        position_usd = qty * current_price if current_price > 0 else 0.0
        has_real_position = qty > 0 and position_usd >= DUST_THRESHOLD_USD

        if has_real_position and not in_memory:
            entry_prices[symbol] = current_price
            high_water_marks[symbol] = current_price
            # Adopted via reconcile — no known order_id. Drop any stale residue.
            entry_order_ids.pop(symbol, None)
            change = f"adopted {symbol} qty={qty:.6f} @ ${current_price:.2f}"
            log.warning(f"[{log_name}] RECONCILE: {change}")
            changes.append(change)
        elif in_memory and not has_real_position:
            entry_prices.pop(symbol, None)
            high_water_marks.pop(symbol, None)
            entry_order_ids.pop(symbol, None)
            change = f"dropped stale {symbol} (qty={qty:.8f})"
            log.info(f"[{log_name}] RECONCILE: {change}")
            changes.append(change)

    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.crypto.coinbase.yaml")
    args = parser.parse_args()

    load_dotenv(".env.coinbase", override=True)
    cfg = load_config(args.config)

    log_name = "crypto-coinbase"
    log = get_logger(log_name)

    log.info(f"=== Starting crypto trend bot [{log_name}] on Coinbase ===")

    track_restart_and_alert(log_name)

    fill_timeout_s = float(cfg.get("loop", {}).get("fill_timeout_s", 10.0))
    broker = CoinbaseBroker(fill_timeout_s=fill_timeout_s)
    try:
        equity = broker.account_equity()
        log.info(f"[{log_name}] Coinbase portfolio value: ${equity:,.2f}")
    except CoinbaseBrokerError as e:
        log.error(f"[{log_name}] Cannot connect to Coinbase: {e}")
        sys.exit(1)

    strat = TrendStrategy(
        ma_fast=cfg["strategy"]["ma_fast"],
        ma_slow=cfg["strategy"]["ma_slow"],
        take_profit_pct=cfg["strategy"]["take_profit_pct"],
        stop_loss_pct=cfg["strategy"]["stop_loss_pct"],
        trailing_stop_pct=cfg["strategy"]["trailing_stop_pct"],
    )
    log.info(f"[{log_name}] Strategy: MA{strat.ma_fast}/{strat.ma_slow}, TP={strat.take_profit_pct}%, SL={strat.stop_loss_pct}%, trail={strat.trailing_stop_pct}%")

    symbols = cfg["symbols"]
    poll = cfg["loop"]["poll_seconds"]
    ma_days_needed = strat.ma_slow + 10

    sizer_cfg = SizerConfig.from_dict(cfg["sizing"])
    log.info(f"[{log_name}] Sizing: equity/{sizer_cfg.divisor}, tw_cap=${sizer_cfg.training_wheel_cap_usd:.0f} "
             f"for {sizer_cfg.training_wheel_trades} trades, growth<={sizer_cfg.max_growth_ratio}x, "
             f"DD_freeze=-{sizer_cfg.dd_threshold_pct}%/{sizer_cfg.dd_lookback_days}d, "
             f"enabled={sizer_cfg.enabled}")

    basis_cfg = cfg.get("basis_trade", {})
    basis_unlock_threshold = float(basis_cfg.get("unlock_threshold_usd", 0))
    # Re-arm hysteresis: equity must fall this fraction below the threshold
    # before a new crossing can re-fire the alert. Prevents oscillation spam
    # if equity sits right at the line.
    basis_rearm_ratio = float(basis_cfg.get("rearm_ratio", 0.95))
    basis_rearm_threshold = basis_unlock_threshold * basis_rearm_ratio

    state_path = Path(state_path_for(log_name))
    prior = load_state(state_path)
    entry_prices: dict = prior.get("entry_prices", {})
    high_water_marks: dict = prior.get("high_water_marks", {})
    entry_order_ids: dict = prior.get("entry_order_ids", {})
    closed_trades: int = int(prior.get("closed_trades", 0))
    last_size_usd: float = float(prior.get("last_size_usd", 0.0))
    frozen_until_str = prior.get("frozen_until")
    frozen_until = datetime.fromisoformat(frozen_until_str) if frozen_until_str else None
    equity_samples = parse_equity_samples(prior.get("equity_samples"))
    basis_unlock_alerted_str = prior.get("basis_unlock_alerted_at")
    basis_unlock_alerted_at = (datetime.fromisoformat(basis_unlock_alerted_str)
                                if basis_unlock_alerted_str else None)

    now = datetime.now()
    equity_samples.append((now, equity))
    equity_samples = prune_equity_samples(equity_samples, EQUITY_SAMPLE_RETENTION_DAYS, now)

    log.info(f"[{log_name}] Restored state: {len(entry_prices)} positions, "
             f"{closed_trades} closed trades, last_size=${last_size_usd:.2f}, "
             f"{len(equity_samples)} equity samples, frozen_until={frozen_until}")

    startup_changes = reconcile_positions(broker, symbols, entry_prices, high_water_marks,
                                           entry_order_ids, log, log_name)
    if startup_changes:
        notify_error(log_name, f"RECONCILE (startup): {len(startup_changes)} change(s) — "
                               f"{'; '.join(startup_changes)[:200]}")

    def snapshot_state() -> dict:
        return {
            "entry_prices": entry_prices,
            "high_water_marks": high_water_marks,
            "entry_order_ids": entry_order_ids,
            "closed_trades": closed_trades,
            "last_size_usd": last_size_usd,
            "frozen_until": frozen_until.isoformat() if frozen_until else None,
            "equity_samples": serialize_equity_samples(equity_samples),
            "basis_unlock_alerted_at": (basis_unlock_alerted_at.isoformat()
                                         if basis_unlock_alerted_at else None),
            "updated": datetime.now().isoformat(),
        }

    def maybe_alert_basis_unlock(equity_now: float):
        """One-shot Pushover on below→above transition of the unlock threshold.

        State machine (per Codex review 2026-06-02):
          - ARMED (alerted_at is None):
              equity ≥ threshold → fire alert; on confirmed delivery, record
              timestamp and persist immediately so a crash before the next
              scheduled save_state cannot duplicate the alert. If delivery
              fails (Pushover down), DO NOT record — retry next loop.
          - FIRED (alerted_at is set):
              equity ≥ rearm_threshold (= threshold × rearm_ratio) → stay quiet.
              equity < rearm_threshold → re-arm (clear timestamp, persist) so
              a future crossing can fire again. The hysteresis band avoids
              ping-pong if equity oscillates right at the threshold.
        """
        nonlocal basis_unlock_alerted_at
        if basis_unlock_threshold <= 0:
            return

        if basis_unlock_alerted_at is None:
            if equity_now < basis_unlock_threshold:
                return
            msg = (f"Coinbase equity ${equity_now:,.2f} ≥ "
                   f"${basis_unlock_threshold:,.0f} — basis trade build "
                   f"(Sprint 4) is unlocked")
            log.warning(f"[{log_name}] BASIS UNLOCK: {msg}")
            delivered = notify_milestone(log_name, "BASIS UNLOCK", msg)
            if delivered:
                basis_unlock_alerted_at = datetime.now()
                # Persist before the next equity poll; protects the one-shot
                # guarantee against crash between fire and the next loop save.
                save_state(state_path, snapshot_state())
            else:
                log.warning(f"[{log_name}] BASIS UNLOCK notify failed; will "
                            f"retry next cycle (state unchanged)")
        else:
            if equity_now < basis_rearm_threshold:
                log.info(f"[{log_name}] basis-unlock re-armed: equity "
                         f"${equity_now:,.2f} < ${basis_rearm_threshold:,.2f}")
                basis_unlock_alerted_at = None
                save_state(state_path, snapshot_state())

    def handle_exit(signum, frame):
        log.warning(f"[{log_name}] Shutting down. Positions held (trend = long hold).")
        save_state(state_path, snapshot_state())
        log.info(f"[{log_name}] State saved. Exited.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    log.info(f"[{log_name}] Entering main loop. Polling every {poll}s ({poll/3600:.1f}h).")

    today_iso = datetime.utcnow().date().isoformat()
    closed_trades_today = 0
    realized_pnl_today = 0.0
    ma_states: dict = {}

    while True:
        try:
            log.info(f"[{log_name}] HEARTBEAT")
            ping_healthcheck()
            # Sample equity each cycle (crypto polls hourly, so once per hour is fine)
            now = datetime.now()
            try:
                current_equity = broker.account_equity()
                equity_samples.append((now, current_equity))
                equity_samples = prune_equity_samples(equity_samples, EQUITY_SAMPLE_RETENTION_DAYS, now)
                maybe_alert_basis_unlock(current_equity)
            except CoinbaseBrokerError as e:
                log.warning(f"[{log_name}] equity sample failed: {e}")
                current_equity = equity

            for symbol in symbols:
                try:
                    daily = broker.daily_bars(symbol, ma_days_needed)
                    qty = broker.get_position_qty(symbol)
                except CoinbaseBrokerError as e:
                    log.error(f"[{log_name}] {symbol}: data fetch failed: {e}")
                    continue

                entry = entry_prices.get(symbol)
                hwm = high_water_marks.get(symbol)

                # Dust detection: untracked tiny positions (left-over from earlier activity)
                # should not block new entries. Treat them as empty for decision purposes.
                current_price_for_dust = float(daily["close"].iloc[-1]) if daily is not None and len(daily) >= 1 else 0
                is_dust = (qty > 0 and entry is None
                           and current_price_for_dust > 0
                           and qty * current_price_for_dust < DUST_THRESHOLD_USD)
                effective_qty = 0.0 if is_dust else qty
                if is_dust:
                    log.info(f"[{log_name}] {symbol}: dust detected "
                             f"(qty={qty:.8f}, ~${qty * current_price_for_dust:.2f}) — treating as empty")

                if effective_qty > 0 and daily is not None and len(daily) >= 1:
                    current_price = float(daily["close"].iloc[-1])
                    hwm = max(hwm or entry or current_price, current_price)
                    high_water_marks[symbol] = hwm

                sig = strat.decide(daily, effective_qty, entry, hwm)
                log.info(f"[{log_name}] {symbol}: {sig.action} | {sig.reason} | px={sig.price:.2f}")
                ma_states[symbol] = "bullish" if "bullish" in sig.reason else ("bearish" if "bearish" in sig.reason else "?")

                if sig.action == "BUY" and effective_qty == 0:
                    decision = decide_size(
                        cfg=sizer_cfg,
                        equity=current_equity,
                        closed_trades=closed_trades,
                        equity_history=equity_samples,
                        prior_size=last_size_usd,
                        frozen_until=frozen_until,
                        now=datetime.now(),
                    )
                    log.info(f"[{log_name}] SIZER: ${decision.per_trade_usd:.2f} | {decision.reason}")
                    if decision.new_frozen_until:
                        frozen_until = datetime.fromisoformat(decision.new_frozen_until)
                        log.warning(f"[{log_name}] FREEZE triggered. Sizing locked until {frozen_until}.")
                        dd_num = 0.0
                        for part in decision.reason.split():
                            if part.startswith("dd="):
                                try:
                                    dd_num = float(part.split("=")[1].rstrip("%"))
                                except Exception:
                                    pass
                        notify_freeze(log_name, dd_num, frozen_until.strftime("%Y-%m-%d %H:%M"))

                    size_usd = decision.per_trade_usd
                    if size_usd <= 0:
                        log.warning(f"[{log_name}] Sizer returned $0 — skipping BUY {symbol}.")
                        continue
                    try:
                        fill = broker.buy_notional(symbol, size_usd)
                        entry_px = fill.fill_price if fill.fill_price > 0 else sig.price
                        entry_prices[symbol] = entry_px
                        high_water_marks[symbol] = entry_px
                        entry_order_ids[symbol] = fill.order_id
                        last_size_usd = size_usd
                        log.info(f"[{log_name}] BUY {symbol} ${size_usd:.2f} @ ${entry_px:.2f} (fill_qty={fill.filled_qty})")
                        notify_buy(log_name, symbol, size_usd, entry_px)
                        if not fill.fully_filled:
                            notify_error(log_name, f"PARTIAL BUY {symbol}: filled_qty={fill.filled_qty} of ${size_usd} (order_id={fill.order_id})")
                        save_state(state_path, snapshot_state())
                    except CoinbaseBrokerError as e:
                        log.error(f"[{log_name}] BUY {symbol} FAILED: {e}")

                elif sig.action == "SELL" and effective_qty > 0:
                    try:
                        fill = broker.sell_all(symbol)
                        exit_px = fill.fill_price if (fill and fill.fill_price > 0) else sig.price
                        if fill and not fill.fully_filled:
                            # Partial close: residual still held on Coinbase. Keep state
                            # so the next tick re-evaluates against what's left.
                            log.warning(
                                f"[{log_name}] PARTIAL SELL {symbol}: filled_qty={fill.filled_qty} "
                                f"@ ${exit_px:.2f} — keeping position in state for retry"
                            )
                            notify_error(
                                log_name,
                                f"PARTIAL SELL {symbol}: filled_qty={fill.filled_qty} "
                                f"(order_id={fill.order_id}); position retained"
                            )
                            save_state(state_path, snapshot_state())
                        else:
                            entry_price_for_pnl = entry_prices.get(symbol, exit_px)
                            pnl_pct = (exit_px - entry_price_for_pnl) / entry_price_for_pnl * 100 if entry_price_for_pnl else 0.0
                            qty_closed = fill.filled_qty if fill else effective_qty
                            pnl_usd = realized_pnl_usd(entry_price_for_pnl, exit_px, qty_closed)
                            entry_prices.pop(symbol, None)
                            high_water_marks.pop(symbol, None)
                            entry_order_ids.pop(symbol, None)
                            closed_trades += 1
                            closed_trades_today += 1
                            realized_pnl_today += pnl_usd
                            log.info(f"[{log_name}] SELL {symbol} @ ${exit_px:.2f} (fill_qty={fill.filled_qty if fill else 0})")
                            notify_sell(log_name, symbol, exit_px, pnl_pct, sig.reason.split("(")[0].strip())
                            save_state(state_path, snapshot_state())
                    except CoinbaseBrokerError as e:
                        log.error(f"[{log_name}] SELL {symbol} FAILED: {e}")

            save_state(state_path, snapshot_state())

            # Daily summary (once per day, guarded by _should_write_today)
            write_crypto_summary(
                bot=log_name, equity=current_equity, positions=entry_prices,
                ma_states=ma_states,
                closed_trades_today=closed_trades_today,
                realized_pnl_today=realized_pnl_today,
            )

            # Date-boundary work: rollover counters + daily reconcile.
            # Reconcile runs at UTC rollover so drift is surfaced once per day
            # (Phase A prevents most of it; this catches manual broker actions,
            # restart races, dust accumulation, unknown unknowns).
            # UTC-bound rollover: crypto is 24/7, so "day" semantics must not
            # follow the server's local timezone.
            current_date_iso = datetime.utcnow().date().isoformat()
            if current_date_iso != today_iso:
                eod_changes = reconcile_positions(broker, symbols, entry_prices,
                                                   high_water_marks, entry_order_ids,
                                                   log, log_name)
                if eod_changes:
                    notify_error(log_name, f"RECONCILE (daily): {len(eod_changes)} change(s) — "
                                           f"{'; '.join(eod_changes)[:200]}")
                    save_state(state_path, snapshot_state())

                today_iso = current_date_iso
                closed_trades_today = 0
                realized_pnl_today = 0.0

            time.sleep(poll)
        except CoinbaseBrokerError as e:
            log.error(f"[{log_name}] Broker error: {e}")
            time.sleep(poll)
        except Exception as e:
            log.exception(f"[{log_name}] Unexpected error: {e}")
            notify_error(log_name, f"Unexpected error: {e}")
            time.sleep(poll)


if __name__ == "__main__":
    main()
