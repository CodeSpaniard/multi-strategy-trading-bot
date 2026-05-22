import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
import yaml
from dotenv import load_dotenv

from src.broker import Broker, BrokerError
from src.logger import get_logger
from src.risk import RiskManager
from src.strategy import MomentumStrategy


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception:
            return {}
    return {}


def save_state(state_path: Path, entry_prices: dict, high_water_marks: dict, trades_today: int, today_iso: str):
    state_path.write_text(json.dumps({
        "entry_prices": entry_prices,
        "high_water_marks": high_water_marks,
        "trades_today": trades_today,
        "today": today_iso,
    }))


def reconcile_positions(broker: Broker, entry_prices: dict, high_water_marks: dict, log, log_name: str):
    """Load actual Alpaca positions into local state. Prevents orphan positions after restart."""
    try:
        positions = broker.get_all_positions()
    except BrokerError as e:
        log.error(f"[{log_name}] RECONCILE FAILED — cannot fetch positions: {e}")
        return

    alpaca_symbols = set()
    for p in positions:
        sym = p.symbol
        alpaca_symbols.add(sym)
        entry_from_broker = float(p.avg_entry_price)
        current_price = float(p.current_price)
        if sym not in entry_prices:
            entry_prices[sym] = entry_from_broker
            log.warning(f"[{log_name}] RECONCILE: adopted existing {sym} position entry=${entry_from_broker:.4f}")
        # Init hwm to max(entry, current) — safer than assuming current is peak
        high_water_marks[sym] = max(
            high_water_marks.get(sym, 0.0),
            entry_prices[sym],
            current_price,
        )

    # Clean up stale entries for symbols no longer held
    for sym in list(entry_prices.keys()):
        if sym not in alpaca_symbols:
            log.info(f"[{log_name}] RECONCILE: dropping stale entry for {sym} (not held on Alpaca)")
            entry_prices.pop(sym, None)
            high_water_marks.pop(sym, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.paper.yaml")
    parser.add_argument("--env", default=".env.paper")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_config(args.config)

    paper = cfg["paper_mode"]
    log_name = "paper" if paper else "live"
    log = get_logger(log_name)

    mode_banner = "PAPER TRADING" if paper else "LIVE TRADING (REAL MONEY)"
    log.info(f"=== Starting bot [{log_name}] in {mode_banner} mode ===")
    if not paper:
        log.warning("LIVE MODE — real money at risk.")

    broker = Broker(paper=paper)
    equity = broker.account_equity()
    log.info(f"[{log_name}] Account equity: ${equity:,.2f}")

    risk = RiskManager(
        starting_equity=equity,
        max_position_usd=cfg["risk"]["max_position_usd"],
        daily_loss_limit_usd=cfg["risk"]["daily_loss_limit_usd"],
        max_trades_per_day=cfg["risk"]["max_trades_per_day"],
    )
    strat = MomentumStrategy(
        momentum_threshold_pct=cfg["strategy"]["momentum_threshold_pct"],
        take_profit_pct=cfg["strategy"]["take_profit_pct"],
        stop_loss_pct=cfg["strategy"]["stop_loss_pct"],
        trailing_stop_pct=cfg["strategy"]["trailing_stop_pct"],
        mode=cfg["strategy"].get("mode", "breakout"),
    )
    log.info(f"[{log_name}] Strategy mode: {strat.mode}")

    symbols = cfg["symbols"]
    lookback = cfg["strategy"]["lookback_minutes"]
    poll = cfg["loop"]["poll_seconds"]

    # Persistent state — survives restarts
    state_path = Path("logs") / f"{log_name}.state.json"
    prior = load_state(state_path)
    entry_prices: dict = prior.get("entry_prices", {})
    high_water_marks: dict = prior.get("high_water_marks", {})
    if prior.get("today") == risk.today.isoformat():
        risk.trades_today = prior.get("trades_today", 0)
        log.info(f"[{log_name}] Restored state: {len(entry_prices)} positions, {risk.trades_today} trades today")

    # Reconcile with Alpaca to catch orphans
    reconcile_positions(broker, entry_prices, high_water_marks, log, log_name)
    save_state(state_path, entry_prices, high_water_marks, risk.trades_today, risk.today.isoformat())

    def handle_exit(signum, frame):
        log.warning(f"[{log_name}] Kill switch — flattening positions.")
        try:
            broker.flatten_all()
            log.info(f"[{log_name}] flatten_all succeeded.")
        except BrokerError as e:
            log.error(f"[{log_name}] FLATTEN FAILED: {e}. Positions may still be open on Alpaca — check dashboard.")
        log.info(f"[{log_name}] Exited.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    while True:
        try:
            if not broker.market_is_open():
                log.info(f"[{log_name}] Market closed. Sleeping 60s.")
                time.sleep(60)
                continue

            current_equity = broker.account_equity()
            entries_allowed, why = risk.can_trade(current_equity)
            if not entries_allowed:
                log.info(f"[{log_name}] New entries blocked: {why}. Exits still active.")

            for symbol in symbols:
                try:
                    bars = broker.recent_bars(symbol, lookback)
                    qty = broker.get_position_qty(symbol)
                except BrokerError as e:
                    log.error(f"[{log_name}] {symbol}: data/position fetch failed — skipping this tick: {e}")
                    continue

                entry = entry_prices.get(symbol)
                hwm = high_water_marks.get(symbol)

                if qty > 0 and bars is not None and len(bars) >= 1:
                    last_price = float(bars["close"].iloc[-1])
                    hwm = max(hwm or entry or last_price, last_price)
                    high_water_marks[symbol] = hwm

                sig = strat.decide(bars, qty, entry, hwm)
                log.info(f"[{log_name}] {symbol}: {sig.action} | {sig.reason} | px={sig.price:.2f}")

                if sig.action == "BUY" and qty == 0:
                    if not entries_allowed:
                        continue
                    size = risk.position_size_usd()
                    try:
                        broker.buy_notional(symbol, size)
                    except BrokerError as e:
                        log.error(f"[{log_name}] BUY {symbol} FAILED: {e}")
                        continue
                    entry_prices[symbol] = sig.price
                    high_water_marks[symbol] = sig.price
                    log.info(f"[{log_name}] BUY {symbol} ${size} @ ~{sig.price:.2f}")
                    save_state(state_path, entry_prices, high_water_marks, risk.trades_today, risk.today.isoformat())
                elif sig.action == "SELL" and qty > 0:
                    try:
                        broker.close_position(symbol)
                    except BrokerError as e:
                        log.error(f"[{log_name}] SELL {symbol} FAILED: {e}. Position still open on Alpaca.")
                        continue
                    entry_prices.pop(symbol, None)
                    high_water_marks.pop(symbol, None)
                    risk.record_trade()
                    log.info(f"[{log_name}] SELL {symbol} @ ~{sig.price:.2f}")
                    save_state(state_path, entry_prices, high_water_marks, risk.trades_today, risk.today.isoformat())

            time.sleep(poll)
        except BrokerError as e:
            log.error(f"[{log_name}] Broker error in loop: {e}")
            time.sleep(poll)
        except Exception as e:
            log.exception(f"[{log_name}] Unexpected loop error: {e}")
            time.sleep(poll)


if __name__ == "__main__":
    main()
