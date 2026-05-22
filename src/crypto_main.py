"""Crypto daily-trend bot.
Checks once per hour for daily MA crossovers. Manages positions with trailing stop.
Designed to run 24/7 (crypto never closes).
"""
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
from src.trend_strategy import TrendStrategy


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


def reconcile_positions(broker, entry_prices, high_water_marks, log, log_name):
    try:
        positions = broker.get_all_positions()
    except BrokerError as e:
        log.error(f"[{log_name}] RECONCILE FAILED: {e}")
        return
    alpaca_symbols = set()
    for p in positions:
        sym = p.symbol
        alpaca_symbols.add(sym)
        entry = float(p.avg_entry_price)
        current = float(p.current_price)
        if sym not in entry_prices:
            entry_prices[sym] = entry
            log.warning(f"[{log_name}] RECONCILE: adopted {sym} entry=${entry:.4f}")
        high_water_marks[sym] = max(high_water_marks.get(sym, 0.0), entry_prices[sym], current)
    for sym in list(entry_prices.keys()):
        if sym not in alpaca_symbols:
            log.info(f"[{log_name}] RECONCILE: dropping stale {sym}")
            entry_prices.pop(sym, None)
            high_water_marks.pop(sym, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.crypto.paper.yaml")
    parser.add_argument("--env", default=".env.paper")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_config(args.config)

    paper = cfg["paper_mode"]
    asset_class = cfg.get("asset_class", "crypto")
    log_name = "crypto-paper" if paper else "crypto-live"
    log = get_logger(log_name)

    mode_banner = "PAPER" if paper else "LIVE (REAL MONEY)"
    log.info(f"=== Starting crypto trend bot [{log_name}] in {mode_banner} mode ===")

    broker = Broker(paper=paper, asset_class=asset_class)
    equity = broker.account_equity()
    log.info(f"[{log_name}] Account equity: ${equity:,.2f}")

    risk = RiskManager(
        starting_equity=equity,
        max_position_usd=cfg["risk"]["max_position_usd"],
        daily_loss_limit_usd=cfg["risk"]["daily_loss_limit_usd"],
        max_trades_per_day=cfg["risk"]["max_trades_per_day"],
    )
    strat = TrendStrategy(
        ma_fast=cfg["strategy"]["ma_fast"],
        ma_slow=cfg["strategy"]["ma_slow"],
        take_profit_pct=cfg["strategy"]["take_profit_pct"],
        stop_loss_pct=cfg["strategy"]["stop_loss_pct"],
        trailing_stop_pct=cfg["strategy"]["trailing_stop_pct"],
    )
    log.info(f"[{log_name}] Strategy: MA{strat.ma_fast}/{strat.ma_slow} crossover, TP={strat.take_profit_pct}%, SL={strat.stop_loss_pct}%, trail={strat.trailing_stop_pct}%")

    symbols = cfg["symbols"]
    poll = cfg["loop"]["poll_seconds"]
    ma_days_needed = strat.ma_slow + 10

    state_path = Path("logs") / f"{log_name}.state.json"
    prior = load_state(state_path)
    entry_prices: dict = prior.get("entry_prices", {})
    high_water_marks: dict = prior.get("high_water_marks", {})
    if prior.get("today") == risk.today.isoformat():
        risk.trades_today = prior.get("trades_today", 0)
    log.info(f"[{log_name}] Restored state: {len(entry_prices)} positions, {risk.trades_today} trades today")

    reconcile_positions(broker, entry_prices, high_water_marks, log, log_name)
    save_state(state_path, entry_prices, high_water_marks, risk.trades_today, risk.today.isoformat())

    def handle_exit(signum, frame):
        log.warning(f"[{log_name}] Kill switch received.")
        try:
            broker.flatten_all()
            log.info(f"[{log_name}] flatten_all succeeded.")
        except BrokerError as e:
            log.error(f"[{log_name}] FLATTEN FAILED: {e}")
        log.info(f"[{log_name}] Exited.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    log.info(f"[{log_name}] Entering main loop. Polling every {poll}s ({poll/3600:.1f}h).")

    while True:
        try:
            current_equity = broker.account_equity()
            entries_allowed, why = risk.can_trade(current_equity)
            if not entries_allowed:
                log.info(f"[{log_name}] New entries blocked: {why}. Exits still active.")

            for symbol in symbols:
                try:
                    daily = broker.daily_bars(symbol, ma_days_needed)
                    qty = broker.get_position_qty(symbol)
                except BrokerError as e:
                    log.error(f"[{log_name}] {symbol}: data fetch failed — skipping: {e}")
                    continue

                entry = entry_prices.get(symbol)
                hwm = high_water_marks.get(symbol)

                if qty > 0 and daily is not None and len(daily) >= 1:
                    last_price = float(daily["close"].iloc[-1])
                    hwm = max(hwm or entry or last_price, last_price)
                    high_water_marks[symbol] = hwm

                sig = strat.decide(daily, qty, entry, hwm)
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
                        log.error(f"[{log_name}] SELL {symbol} FAILED: {e}")
                        continue
                    entry_prices.pop(symbol, None)
                    high_water_marks.pop(symbol, None)
                    risk.record_trade()
                    log.info(f"[{log_name}] SELL {symbol} @ ~{sig.price:.2f}")
                    save_state(state_path, entry_prices, high_water_marks, risk.trades_today, risk.today.isoformat())

            time.sleep(poll)
        except BrokerError as e:
            log.error(f"[{log_name}] Broker error: {e}")
            time.sleep(poll)
        except Exception as e:
            log.exception(f"[{log_name}] Unexpected error: {e}")
            time.sleep(poll)


if __name__ == "__main__":
    main()
