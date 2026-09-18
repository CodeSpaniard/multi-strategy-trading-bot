"""Dip scanner bot.

Two-phase daily cycle:
  Phase 1 (9:30am-3:50pm): Monitor open positions, exit on bounce/stop
  Phase 2 (3:50pm-4:00pm): Scan for new dips, buy top candidates

Per-trade size computed by src.sizer: proportional to account equity with
training-wheels cap, growth cap, and 7-day drawdown circuit breaker.
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

from src.broker import Broker, BrokerError
from src.daily_summary import write_scanner_summary
from src.equity_history import append_daily_equity
from src.healthcheck import ping_healthcheck
from src.logger import get_logger, state_path as state_path_for
from src.notifier import notify_buy, notify_sell, notify_freeze, notify_error, notify_daily_brake
from src.restart_tracker import track_restart_and_alert
from src.daily_loss import daily_block, entries_blocked, resolve_limit, restore_daily, utc_today_iso
from src.fill import realized_pnl_usd
from src.risk import RiskManager
from src.atomic_io import write_json_atomic
from src.scanner import scan_for_dips
from src.scanner_strategy import ScannerStrategy
from src.sizer import SizerConfig, decide_size


EQUITY_SAMPLE_INTERVAL_SECONDS = 1800  # sample equity every 30 min
EQUITY_SAMPLE_RETENTION_DAYS = 14


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
    """State-stored samples are [iso_str, float]; parse back to [datetime, float]."""
    out = []
    for item in raw or []:
        try:
            ts = datetime.fromisoformat(item[0])
            out.append((ts, float(item[1])))
        except Exception:
            continue
    return out


def serialize_equity_samples(samples: list) -> list:
    return [[ts.isoformat(), v] for ts, v in samples]


def prune_equity_samples(samples: list, retention_days: int, now: datetime) -> list:
    cutoff = now - timedelta(days=retention_days)
    return [(t, v) for t, v in samples if t >= cutoff]


def reconcile_positions(broker, positions, log, log_name) -> list:
    """Compare in-memory positions against broker's view. Mutate in-memory to match.

    Returns a list of human-readable change strings; empty list means clean.
    Adopt-on-find and drop-on-missing are silent self-healing for known cases
    (broker manual action, restart races). Caller is expected to surface a
    non-empty change list via Pushover so the operator notices drift.
    """
    changes: list = []
    try:
        alpaca_positions = broker.get_all_positions()
    except BrokerError as e:
        log.error(f"[{log_name}] RECONCILE FAILED: {e}")
        return changes
    alpaca_symbols = set()
    for p in alpaca_positions:
        sym = p.symbol
        alpaca_symbols.add(sym)
        if sym not in positions:
            entry = float(p.avg_entry_price)
            current = float(p.current_price)
            positions[sym] = {
                "entry_price": entry,
                "high_water": max(entry, current),
                "bounce_target_pct": 2.0,
                "entry_date": datetime.now().isoformat(),
            }
            change = f"adopted {sym} @ ${entry:.2f}"
            log.warning(f"[{log_name}] RECONCILE: {change}")
            changes.append(change)
    for sym in list(positions.keys()):
        if sym not in alpaca_symbols:
            change = f"dropped stale {sym}"
            log.info(f"[{log_name}] RECONCILE: {change}")
            del positions[sym]
            changes.append(change)
    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.scanner.paper.yaml")
    parser.add_argument("--env", default=".env.paper")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_config(args.config)

    paper = cfg["paper_mode"]
    log_name = "scanner-paper" if paper else "scanner-live"
    log = get_logger(log_name)

    mode_banner = "PAPER" if paper else "LIVE (REAL MONEY)"
    log.info(f"=== Starting dip scanner [{log_name}] in {mode_banner} mode ===")

    track_restart_and_alert(log_name)

    fill_timeout_s = float(cfg.get("loop", {}).get("fill_timeout_s", 10.0))
    broker = Broker(paper=paper, asset_class=cfg.get("asset_class", "equity"),
                    fill_timeout_s=fill_timeout_s)
    equity = broker.account_equity()
    log.info(f"[{log_name}] Account equity: ${equity:,.2f}")

    strat = ScannerStrategy(
        hard_stop_pct=cfg["strategy"]["hard_stop_pct"],
        trailing_stop_pct=cfg["strategy"]["trailing_stop_pct"],
    )
    max_positions = cfg["risk"]["max_positions"]
    # The code being present is not the same as the policy being active; the
    # config activates it. An invalid value is reported loudly rather than
    # silently reinterpreted — but never by refusing to start, since this bot's
    # stops are enforced by the running loop, not by resting broker orders.
    daily_loss_limit_usd, limit_cfg_error = resolve_limit(
        cfg["risk"].get("daily_realized_loss_limit_usd"))
    dip_threshold = cfg["scanner"]["dip_threshold_pct"]
    bounce_ratio = cfg["scanner"]["bounce_ratio"]
    min_bounce = cfg["scanner"]["min_bounce_pct"]
    poll = cfg["loop"]["monitor_poll_seconds"]

    sizer_cfg = SizerConfig.from_dict(cfg["sizing"])
    log.info(f"[{log_name}] Config: dip>={dip_threshold}%, bounce={bounce_ratio*100:.0f}% of dip, "
             f"hard_stop={strat.hard_stop_pct}%, trail={strat.trailing_stop_pct}%, "
             f"max_pos={max_positions}")
    log.info(f"[{log_name}] Sizing: equity/{sizer_cfg.divisor}, tw_cap=${sizer_cfg.training_wheel_cap_usd:.0f} "
             f"for {sizer_cfg.training_wheel_trades} trades, growth<={sizer_cfg.max_growth_ratio}x, "
             f"DD_freeze=-{sizer_cfg.dd_threshold_pct}%/{sizer_cfg.dd_lookback_days}d, "
             f"enabled={sizer_cfg.enabled}")

    # State
    state_path = Path(state_path_for(log_name))
    equity_daily_path = state_path.with_name(f"{log_name}.equity_daily.jsonl")
    prior = load_state(state_path)
    positions: dict = prior.get("positions", {})
    closed_trades: int = int(prior.get("closed_trades", 0))
    last_size_usd: float = float(prior.get("last_size_usd", 0.0))
    frozen_until_str = prior.get("frozen_until")
    frozen_until = datetime.fromisoformat(frozen_until_str) if frozen_until_str else None
    equity_samples = parse_equity_samples(prior.get("equity_samples"))
    log.info(f"[{log_name}] Restored {len(positions)} positions, "
             f"{closed_trades} closed trades, last_size=${last_size_usd:.2f}, "
             f"{len(equity_samples)} equity samples, "
             f"frozen_until={frozen_until}")

    startup_changes = reconcile_positions(broker, positions, log, log_name)
    if startup_changes:
        notify_error(log_name, f"RECONCILE (startup): {len(startup_changes)} change(s) — "
                               f"{'; '.join(startup_changes)[:200]}")

    # Daily tracking. Restored from state when it belongs to today, so a restart
    # cannot clear a loss limit that has already been reached.
    today_iso = utc_today_iso()
    realized_pnl_today, closed_trades_today, wins_today = restore_daily(prior, today_iso)
    if closed_trades_today or realized_pnl_today:
        log.info(f"[{log_name}] Restored today's book ({today_iso}): "
                 f"realized={realized_pnl_today:+.2f}, closed={closed_trades_today}, wins={wins_today}")
    if limit_cfg_error:
        log.error(f"[{log_name}] CONFIG: {limit_cfg_error}")
        notify_error(log_name, limit_cfg_error)
    if daily_loss_limit_usd is None:
        brake_desc = "UNRESOLVED config — all new entries blocked until fixed"
    elif daily_loss_limit_usd > 0:
        brake_desc = f"limit -${daily_loss_limit_usd:.2f}"
    else:
        brake_desc = "disabled"
    log.info(f"[{log_name}] Daily realized-loss brake: {brake_desc}")

    # Initial equity sample
    now = datetime.now()
    equity_samples.append((now, equity))
    equity_samples = prune_equity_samples(equity_samples, EQUITY_SAMPLE_RETENTION_DAYS, now)
    last_equity_sample_at = now

    def snapshot_state() -> dict:
        return {
            "positions": positions,
            "closed_trades": closed_trades,
            "last_size_usd": last_size_usd,
            "frozen_until": frozen_until.isoformat() if frozen_until else None,
            "equity_samples": serialize_equity_samples(equity_samples),
            "today": datetime.now().date().isoformat(),
            # Same atomic snapshot as the trading state: a crash must not leave
            # "the SELL happened" and "the day's P&L did not" disagreeing.
            "daily": daily_block(today_iso, realized_pnl_today, closed_trades_today, wins_today),
        }

    save_state(state_path, snapshot_state())

    scanned_today = False

    def handle_exit(signum, frame):
        log.warning(f"[{log_name}] Kill switch received. NOT flattening (swing positions held).")
        save_state(state_path, snapshot_state())
        log.info(f"[{log_name}] State saved. Exited.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    log.info(f"[{log_name}] Entering main loop. Poll every {poll}s.")

    while True:
        try:
            log.info(f"[{log_name}] HEARTBEAT")
            ping_healthcheck()
            # Periodic equity sampling (for DD tracking)
            now = datetime.now()
            if (now - last_equity_sample_at).total_seconds() >= EQUITY_SAMPLE_INTERVAL_SECONDS:
                try:
                    current_equity = broker.account_equity()
                    equity_samples.append((now, current_equity))
                    equity_samples = prune_equity_samples(equity_samples, EQUITY_SAMPLE_RETENTION_DAYS, now)
                    last_equity_sample_at = now
                except BrokerError as e:
                    log.warning(f"[{log_name}] equity sample failed: {e}")

            if not broker.market_is_open():
                if scanned_today:
                    log.info(f"[{log_name}] Market closed. Resetting scan flag. Sleeping 60s.")
                    scanned_today = False
                else:
                    log.info(f"[{log_name}] Market closed. Sleeping 60s.")
                time.sleep(60)
                continue

            # === PHASE 1: Monitor open positions for exits ===
            for sym in list(positions.keys()):
                pos = positions[sym]
                try:
                    bars = broker.recent_bars(sym, 1)
                    if bars is None:
                        continue
                    current_price = float(bars["close"].iloc[-1])
                except BrokerError as e:
                    log.error(f"[{log_name}] {sym} price fetch failed: {e}")
                    continue

                pos["high_water"] = max(pos.get("high_water", pos["entry_price"]), current_price)

                sig = strat.check_exit(
                    current_price=current_price,
                    entry_price=pos["entry_price"],
                    high_water=pos["high_water"],
                    bounce_target_pct=pos["bounce_target_pct"],
                )
                log.info(f"[{log_name}] {sym}: {sig.action} | {sig.reason} | px={sig.price:.2f}")

                if sig.action == "SELL":
                    try:
                        fill = broker.close_position(sym)
                        exit_px = fill.fill_price if fill.fill_price > 0 else sig.price
                        if not fill.fully_filled:
                            # Partial close: residual position remains at broker. Do NOT
                            # book the trade as closed — keep state intact so strategy
                            # re-evaluates against the residual on the next tick.
                            log.warning(
                                f"[{log_name}] PARTIAL SELL {sym}: filled_qty={fill.filled_qty} "
                                f"@ ${exit_px:.2f} — keeping position in state for retry"
                            )
                            notify_error(
                                log_name,
                                f"PARTIAL CLOSE {sym}: filled_qty={fill.filled_qty} "
                                f"(order_id={fill.order_id}); position retained"
                            )
                            save_state(state_path, snapshot_state())
                        else:
                            pnl_pct = (exit_px - pos["entry_price"]) / pos["entry_price"] * 100
                            pnl_usd = realized_pnl_usd(pos["entry_price"], exit_px, fill.filled_qty)
                            log.info(f"[{log_name}] SELL {sym} @ ${exit_px:.2f} (fill_qty={fill.filled_qty})")
                            notify_sell(log_name, sym, exit_px, pnl_pct, sig.reason.split("|")[0].strip() if "|" in sig.reason else sig.reason)
                            del positions[sym]
                            closed_trades += 1
                            closed_trades_today += 1
                            realized_pnl_today += pnl_usd
                            if pnl_pct > 0:
                                wins_today += 1
                            save_state(state_path, snapshot_state())
                    except BrokerError as e:
                        log.error(f"[{log_name}] SELL {sym} FAILED: {e}")

            # === PHASE 2: Scan for new dips near close ===
            minutes_to_close = _minutes_to_close(broker, log, log_name)
            scan_window = cfg["scanner"]["scan_time_minutes_before_close"]

            if not scanned_today and minutes_to_close is not None and minutes_to_close <= scan_window:
                log.info(f"[{log_name}] === SCANNING for dips ({minutes_to_close:.0f} min to close) ===")
                candidates = scan_for_dips(broker, dip_threshold, bounce_ratio, min_bounce)

                if not candidates:
                    log.info(f"[{log_name}] No dips >= {dip_threshold}% found today.")
                else:
                    log.info(f"[{log_name}] Found {len(candidates)} dip candidates:")
                    for c in candidates[:10]:
                        log.info(f"[{log_name}]   {c.symbol}: dip -{c.dip_pct:.1f}%, target +{c.bounce_target_pct:.1f}%, px=${c.close_price:.2f}")

                # Sizing decision (same size for all buys this scan)
                try:
                    current_equity = broker.account_equity()
                except BrokerError as e:
                    log.error(f"[{log_name}] can't fetch equity for sizing: {e}")
                    current_equity = equity

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
                    # Extract the dd_pct number from decision.reason for the notification
                    dd_num = 0.0
                    for part in decision.reason.split():
                        if part.startswith("dd="):
                            try:
                                dd_num = float(part.split("=")[1].rstrip("%"))
                            except Exception:
                                pass
                    notify_freeze(log_name, dd_num, frozen_until.strftime("%Y-%m-%d %H:%M"))

                size_usd = decision.per_trade_usd

                if entries_blocked(realized_pnl_today, daily_loss_limit_usd):
                    limit_desc = ("UNRESOLVED config" if daily_loss_limit_usd is None
                                  else f"-{daily_loss_limit_usd:.2f}")
                    log.warning(
                        f"[{log_name}] DAILY LOSS BRAKE: realized {realized_pnl_today:+.2f} today "
                        f"(limit {limit_desc}) — no new entries. Exits still active."
                    )
                    notify_daily_brake(log_name, realized_pnl_today, daily_loss_limit_usd)
                elif size_usd <= 0:
                    log.warning(f"[{log_name}] Sizer returned $0 — no buys this scan.")
                else:
                    slots = max_positions - len(positions)
                    now = datetime.now()
                    for c in candidates[:slots]:
                        if c.symbol in positions:
                            continue
                        try:
                            fill = broker.buy_notional(c.symbol, size_usd)
                            entry_px = fill.fill_price if fill.fill_price > 0 else c.close_price
                            positions[c.symbol] = {
                                "entry_price": entry_px,
                                "high_water": entry_px,
                                "bounce_target_pct": c.bounce_target_pct,
                                "entry_date": now.isoformat(),
                                "entry_order_id": fill.order_id,
                            }
                            last_size_usd = size_usd
                            log.info(f"[{log_name}] BUY {c.symbol} ${size_usd:.2f} @ ${entry_px:.2f} (fill_qty={fill.filled_qty}) | target +{c.bounce_target_pct:.1f}%")
                            notify_buy(log_name, c.symbol, size_usd, entry_px)
                            if not fill.fully_filled:
                                notify_error(log_name, f"PARTIAL BUY {c.symbol}: filled_qty={fill.filled_qty} of ${size_usd} (order_id={fill.order_id})")
                            save_state(state_path, snapshot_state())
                        except BrokerError as e:
                            log.error(f"[{log_name}] BUY {c.symbol} FAILED: {e}")
                            notify_error(log_name, f"BUY {c.symbol} FAILED: {e}")

                scanned_today = True

                # Write daily summary after scan
                try:
                    current_equity = broker.account_equity()
                except BrokerError:
                    current_equity = equity
                write_scanner_summary(
                    bot=log_name, equity=current_equity, positions=positions,
                    closed_trades_today=closed_trades_today, wins_today=wins_today,
                    scan_count=50, dips_found=len(candidates) if candidates else 0,
                    realized_pnl_today=realized_pnl_today,
                )

                # Daily reconcile: catches drift not preventable by Phase A
                # (manual broker action, restart races, unknown unknowns).
                eod_changes = reconcile_positions(broker, positions, log, log_name)
                if eod_changes:
                    notify_error(log_name, f"RECONCILE (EOD): {len(eod_changes)} change(s) — "
                                           f"{'; '.join(eod_changes)[:200]}")
                    save_state(state_path, snapshot_state())

                # Leverage-gate Step 1: durable end-of-day equity history (separate from the
                # ~14d equity_samples) for the future 60-day equity Sharpe + DD-recovery.
                # Placed AFTER the EOD reconcile so open_positions reflects post-reconcile
                # state (per Codex review). current_equity is broker-reported, reconcile-independent.
                try:
                    if append_daily_equity(
                        str(equity_daily_path), datetime.now().strftime("%Y-%m-%d"), log_name,
                        current_equity, closed_trades=closed_trades,
                        open_positions=len(positions), realized_pnl_today=realized_pnl_today,
                    ):
                        log.info(f"[{log_name}] daily equity mark persisted: ${current_equity:.2f}")
                except Exception as e:
                    log.warning(f"[{log_name}] daily-equity persist failed: {e}")

            # Rollover daily counters at date boundary
            current_date_iso = utc_today_iso()
            if current_date_iso != today_iso:
                today_iso = current_date_iso
                closed_trades_today = 0
                wins_today = 0
                realized_pnl_today = 0.0
                # Persist at once: a restart between rollover and the next natural
                # save must not restore yesterday's figure against today.
                save_state(state_path, snapshot_state())

            time.sleep(poll)
        except BrokerError as e:
            log.error(f"[{log_name}] Broker error: {e}")
            time.sleep(poll)
        except Exception as e:
            log.exception(f"[{log_name}] Unexpected error: {e}")
            notify_error(log_name, f"Unexpected error: {e}")
            time.sleep(poll)


def _minutes_to_close(broker, log, log_name) -> float | None:
    """Returns minutes until market close, or None if closed or clock fetch failed."""
    try:
        clock = broker.get_clock()
    except BrokerError as e:
        log.warning(f"[{log_name}] get_clock failed (may miss scan window): {e}")
        return None
    if not clock.is_open:
        return None
    delta = clock.next_close - clock.timestamp
    return delta.total_seconds() / 60


if __name__ == "__main__":
    main()
