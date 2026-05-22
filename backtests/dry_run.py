"""Dry run: fetch recent bars and show what the strategy would decide.
No orders are placed."""
from dotenv import load_dotenv
import yaml
from src.broker import Broker
from src.strategy import MomentumStrategy

load_dotenv()
cfg = yaml.safe_load(open("config.yaml"))

broker = Broker(paper=True)
strat = MomentumStrategy(
    momentum_threshold_pct=cfg["strategy"]["momentum_threshold_pct"],
    take_profit_pct=cfg["strategy"]["take_profit_pct"],
    stop_loss_pct=cfg["strategy"]["stop_loss_pct"],
)

print(f"Equity: ${broker.account_equity():,.2f}")
print(f"Market open now: {broker.market_is_open()}\n")

for symbol in cfg["symbols"]:
    bars = broker.recent_bars(symbol, cfg["strategy"]["lookback_minutes"])
    if bars is None or bars.empty:
        print(f"{symbol}: no bar data available")
        continue
    first = float(bars['close'].iloc[0])
    last = float(bars['close'].iloc[-1])
    pct = (last - first) / first * 100
    sig = strat.decide(bars, holding_qty=0, entry_price=None)
    print(f"{symbol}: bars={len(bars)} first=${first:.2f} last=${last:.2f} "
          f"change={pct:+.3f}%")
    print(f"   → DECISION: {sig.action} ({sig.reason})\n")
