"""Terminal P&L dashboard.

Usage (from project root):
    ./tools/pnl.py                 # full report
    ./tools/pnl.py --compact       # one-line per account

Shows all 3 accounts: Alpaca live, Alpaca paper, Coinbase.
Equity, today's P&L vs last_equity, open positions, recent scanner summary lines.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Make src importable from project root.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv


def colorize(s: str, color: str) -> str:
    if not sys.stdout.isatty():
        return s
    codes = {"green": "\033[32m", "red": "\033[31m", "yellow": "\033[33m",
             "blue": "\033[34m", "bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m"}
    return f"{codes.get(color, '')}{s}{codes['reset']}"


def pct_color(pct: float) -> str:
    return "green" if pct > 0 else ("red" if pct < 0 else "yellow")


def query_alpaca(env_path: str, paper: bool):
    if not Path(env_path).exists():
        return None
    load_dotenv(env_path, override=True)
    from src.broker import Broker, BrokerError
    try:
        b = Broker(paper=paper)
        acct = b.trading.get_account()
        positions = b.get_all_positions()
    except BrokerError as e:
        return {"error": str(e)}
    equity = float(acct.equity)
    last_eq = float(acct.last_equity)
    cash = float(acct.cash)
    chg = equity - last_eq
    chg_pct = (chg / last_eq * 100) if last_eq else 0.0
    pos_rows = []
    for p in positions:
        pos_rows.append({
            "symbol": p.symbol, "qty": float(p.qty),
            "avg": float(p.avg_entry_price), "cur": float(p.current_price),
            "mv": float(p.market_value),
            "pnl": float(p.unrealized_pl), "pnl_pct": float(p.unrealized_plpc) * 100,
        })
    return {
        "equity": equity, "cash": cash, "last_equity": last_eq,
        "chg_today": chg, "chg_pct": chg_pct, "positions": pos_rows,
    }


def query_coinbase():
    env = ".env.coinbase"
    if not Path(env).exists():
        return None
    load_dotenv(env, override=True)
    try:
        from src.coinbase_broker import CoinbaseBroker
        b = CoinbaseBroker()
        equity = b.account_equity()
        symbols = ["BTC-USDC", "ETH-USDC"]
        pos_rows = []
        for s in symbols:
            qty = b.get_position_qty(s)
            if qty > 0:
                px = b.get_current_price(s)
                pos_rows.append({
                    "symbol": s, "qty": qty, "avg": 0.0, "cur": px,
                    "mv": qty * px, "pnl": 0.0, "pnl_pct": 0.0,
                })
        return {"equity": equity, "cash": equity, "positions": pos_rows}
    except Exception as e:
        return {"error": str(e)}


def read_summary_tail(n: int = 5) -> list:
    from src.logger import SUMMARY_LOG
    p = Path(SUMMARY_LOG)
    if not p.exists():
        return []
    lines = p.read_text().strip().splitlines()
    return lines[-n:]


def read_state(bot: str) -> dict:
    from src.logger import state_path as state_path_for
    p = Path(state_path_for(bot))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def print_account_block(name: str, data: dict | None, show_today_change: bool = True):
    print(colorize(f"╔══ {name} " + "═" * (66 - len(name)), "blue"))
    if data is None:
        print("  (account not configured)")
        print()
        return
    if "error" in data:
        print(colorize(f"  ERROR: {data['error']}", "red"))
        print()
        return
    eq = data["equity"]
    cash = data.get("cash", 0.0)
    print(f"  {'Equity:':<14} ${eq:>12,.2f}    Cash: ${cash:>12,.2f}")
    if show_today_change and "chg_today" in data:
        chg = data["chg_today"]
        chg_pct = data["chg_pct"]
        line = f"  {'Today:':<14} {chg:+12,.2f}    ({chg_pct:+.2f}% vs last close)"
        print(colorize(line, pct_color(chg)))
    positions = data.get("positions", [])
    if positions:
        print(f"  {'Positions:':<14} {len(positions)} open")
        for p in positions:
            line = (f"    {p['symbol']:<10} qty={p['qty']:<14.4f} "
                    f"avg=${p['avg']:>8.2f} cur=${p['cur']:>8.2f} "
                    f"mv=${p['mv']:>8.2f} pnl={p['pnl']:+.2f} ({p['pnl_pct']:+.2f}%)")
            print(colorize(line, pct_color(p["pnl_pct"])))
    else:
        print(f"  {'Positions:':<14} none (all cash)")
    print()


def print_scaling_block(bot: str):
    s = read_state(bot)
    if not s:
        return
    closed = s.get("closed_trades", 0)
    last = s.get("last_size_usd", 0.0)
    frozen = s.get("frozen_until")
    print(f"  {colorize('Scaling:', 'dim')}  closed_trades={closed}  last_size=${last:.2f}" +
          (f"  FROZEN_UNTIL={frozen}" if frozen else ""))


def compact_mode(alpaca_live, alpaca_paper, coinbase):
    for name, d in [("alpaca-live", alpaca_live), ("alpaca-paper", alpaca_paper), ("coinbase", coinbase)]:
        if d is None or "error" in (d or {}):
            print(f"{name:<14} N/A")
            continue
        chg = d.get("chg_today", 0.0)
        chg_pct = d.get("chg_pct", 0.0)
        print(f"{name:<14} equity=${d['equity']:>10,.2f}  "
              f"today={chg:+.2f} ({chg_pct:+.2f}%)  "
              f"pos={len(d.get('positions', []))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compact", action="store_true", help="one-line-per-account summary")
    args = ap.parse_args()

    alpaca_live = query_alpaca(".env.live", paper=False)
    alpaca_paper = query_alpaca(".env.paper", paper=True)
    coinbase = query_coinbase()

    if args.compact:
        compact_mode(alpaca_live, alpaca_paper, coinbase)
        return

    print()
    print(colorize("  trading-bot P&L dashboard  ", "bold"), end="")
    print(colorize(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "dim"))
    print()

    print_account_block("ALPACA LIVE", alpaca_live)
    print_scaling_block("scanner-live")
    print()
    print_account_block("ALPACA PAPER", alpaca_paper)
    print_scaling_block("scanner-paper")
    print()
    print_account_block("COINBASE", coinbase, show_today_change=False)
    print_scaling_block("crypto-coinbase")
    print()

    # Totals (real money only, skip paper)
    totals = []
    if alpaca_live and "equity" in alpaca_live:
        totals.append(("Alpaca live", alpaca_live["equity"]))
    if coinbase and "equity" in coinbase:
        totals.append(("Coinbase", coinbase["equity"]))
    if totals:
        total_real = sum(e for _, e in totals)
        print(colorize("═" * 68, "blue"))
        print(f"  Real-money total: ${total_real:,.2f}  ({' + '.join(f'{n}: ${e:,.2f}' for n, e in totals)})")
        print()

    # Recent summary log lines
    summary = read_summary_tail(6)
    if summary:
        print(colorize("  Recent daily summaries:", "bold"))
        for line in summary:
            print(f"    {line}")
        print()


if __name__ == "__main__":
    main()
