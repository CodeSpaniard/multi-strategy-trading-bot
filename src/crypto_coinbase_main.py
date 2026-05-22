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
from src.logger import get_logger, state_path as state_path_for
from src.notifier import notify_buy, notify_sell, notify_freeze
from src.sizer import SizerConfig, decide_size
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
    path.write_text(json.dumps(state, default=str))


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.crypto.coinbase.yaml")
    args = parser.parse_args()

    load_dotenv(".env.coinbase", override=True)
    cfg = load_config(args.config)

    log_name = "crypto-coinbase"
    log = get_logger(log_name)

    log.info(f"=== Starting crypto trend bot [{log_name}] on Coinbase ===")

    broker = CoinbaseBroker()
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

    state_path = Path(state_path_for(log_name))
    prior = load_state(state_path)
    entry_prices: dict = prior.get("entry_prices", {})
    high_water_marks: dict = prior.get("high_water_marks", {})
    closed_trades: int = int(prior.get("closed_trades", 0))
    last_size_usd: float = float(prior.get("last_size_usd", 0.0))
    frozen_until_str = prior.get("frozen_until")
    frozen_until = datetime.fromisoformat(frozen_until_str) if frozen_until_str else None
    equity_samples = parse_equity_samples(prior.get("equity_samples"))

    now = datetime.now()
    equity_samples.append((now, equity))
    equity_samples = prune_equity_samples(equity_samples, EQUITY_SAMPLE_RETENTION_DAYS, now)

    log.info(f"[{log_name}] Restored state: {len(entry_prices)} positions, "
             f"{closed_trades} closed trades, last_size=${last_size_usd:.2f}, "
             f"{len(equity_samples)} equity samples, frozen_until={frozen_until}")

    def snapshot_state() -> dict:
        return {
            "entry_prices": entry_prices,
            "high_water_marks": high_water_marks,
            "closed_trades": closed_trades,
            "last_size_usd": last_size_usd,
            "frozen_until": frozen_until.isoformat() if frozen_until else None,
            "equity_samples": serialize_equity_samples(equity_samples),
            "updated": datetime.now().isoformat(),
        }

    def handle_exit(signum, frame):
        log.warning(f"[{log_name}] Shutting down. Positions held (trend = long hold).")
        save_state(state_path, snapshot_state())
        log.info(f"[{log_name}] State saved. Exited.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    log.info(f"[{log_name}] Entering main loop. Polling every {poll}s ({poll/3600:.1f}h).")

    today_iso = datetime.now().date().isoformat()
    closed_trades_today = 0
    realized_pnl_today = 0.0
    ma_states: dict = {}

    while True:
        try:
            # Sample equity each cycle (crypto polls hourly, so once per hour is fine)
            now = datetime.now()
            try:
                current_equity = broker.account_equity()
                equity_samples.append((now, current_equity))
                equity_samples = prune_equity_samples(equity_samples, EQUITY_SAMPLE_RETENTION_DAYS, now)
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
                        broker.buy_notional(symbol, size_usd)
                        entry_prices[symbol] = sig.price
                        high_water_marks[symbol] = sig.price
                        last_size_usd = size_usd
                        log.info(f"[{log_name}] BUY {symbol} ${size_usd:.2f} @ ~${sig.price:.2f}")
                        notify_buy(log_name, symbol, size_usd, sig.price)
                        save_state(state_path, snapshot_state())
                    except CoinbaseBrokerError as e:
                        log.error(f"[{log_name}] BUY {symbol} FAILED: {e}")

                elif sig.action == "SELL" and effective_qty > 0:
                    try:
                        broker.sell_all(symbol)
                        entry_price_for_pnl = entry_prices.get(symbol, sig.price)
                        pnl_pct = (sig.price - entry_price_for_pnl) / entry_price_for_pnl * 100 if entry_price_for_pnl else 0.0
                        pnl_usd = last_size_usd * pnl_pct / 100 if last_size_usd > 0 else 0.0
                        entry_prices.pop(symbol, None)
                        high_water_marks.pop(symbol, None)
                        closed_trades += 1
                        closed_trades_today += 1
                        realized_pnl_today += pnl_usd
                        log.info(f"[{log_name}] SELL {symbol} @ ~${sig.price:.2f}")
                        notify_sell(log_name, symbol, sig.price, pnl_pct, sig.reason.split("(")[0].strip())
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

            # Rollover daily counters at date boundary
            current_date_iso = datetime.now().date().isoformat()
            if current_date_iso != today_iso:
                today_iso = current_date_iso
                closed_trades_today = 0
                realized_pnl_today = 0.0

            time.sleep(poll)
        except CoinbaseBrokerError as e:
            log.error(f"[{log_name}] Broker error: {e}")
            time.sleep(poll)
        except Exception as e:
            log.exception(f"[{log_name}] Unexpected error: {e}")
            time.sleep(poll)


if __name__ == "__main__":
    main()
