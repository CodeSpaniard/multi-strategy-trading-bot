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
from src.healthcheck import ping_healthcheck
from src.logger import get_logger, state_path as state_path_for
from src.notifier import notify_buy, notify_sell, notify_freeze, notify_error
from src.risk import RiskManager
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
    path.write_text(json.dumps(state, default=str))


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


def reconcile_positions(broker, positions, log, log_name):
    try:
        alpaca_positions = broker.get_all_positions()
    except BrokerError as e:
        log.error(f"[{log_name}] RECONCILE FAILED: {e}")
        return
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
            log.warning(f"[{log_name}] RECONCILE: adopted {sym} entry=${entry:.2f}")
    for sym in list(positions.keys()):
        if sym not in alpaca_symbols:
            log.info(f"[{log_name}] RECONCILE: dropping stale {sym}")
            del positions[sym]


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

    broker = Broker(paper=paper, asset_class=cfg.get("asset_class", "equity"))
    equity = broker.account_equity()
    log.info(f"[{log_name}] Account equity: ${equity:,.2f}")

    strat = ScannerStrategy(
        hard_stop_pct=cfg["strategy"]["hard_stop_pct"],
        trailing_stop_pct=cfg["strategy"]["trailing_stop_pct"],
    )
    max_positions = cfg["risk"]["max_positions"]
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

    reconcile_positions(broker, positions, log, log_name)

    # Daily tracking
    today_iso = datetime.now().date().isoformat()
    closed_trades_today = 0
    wins_today = 0
    realized_pnl_today = 0.0

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
                        broker.close_position(sym)
                        pnl_pct = (sig.price - pos["entry_price"]) / pos["entry_price"] * 100
                        pnl_usd = last_size_usd * pnl_pct / 100 if last_size_usd > 0 else 0.0
                        log.info(f"[{log_name}] SELL {sym} @ ~${sig.price:.2f}")
                        notify_sell(log_name, sym, sig.price, pnl_pct, sig.reason.split("|")[0].strip() if "|" in sig.reason else sig.reason)
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

                if size_usd <= 0:
                    log.warning(f"[{log_name}] Sizer returned $0 — no buys this scan.")
                else:
                    slots = max_positions - len(positions)
                    now = datetime.now()
                    for c in candidates[:slots]:
                        if c.symbol in positions:
                            continue
                        try:
                            broker.buy_notional(c.symbol, size_usd)
                            positions[c.symbol] = {
                                "entry_price": c.close_price,
                                "high_water": c.close_price,
                                "bounce_target_pct": c.bounce_target_pct,
                                "entry_date": now.isoformat(),
                            }
                            last_size_usd = size_usd
                            log.info(f"[{log_name}] BUY {c.symbol} ${size_usd:.2f} @ ~${c.close_price:.2f} | target +{c.bounce_target_pct:.1f}%")
                            notify_buy(log_name, c.symbol, size_usd, c.close_price)
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

            # Rollover daily counters at date boundary
            current_date_iso = datetime.now().date().isoformat()
            if current_date_iso != today_iso:
                today_iso = current_date_iso
                closed_trades_today = 0
                wins_today = 0
                realized_pnl_today = 0.0

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
