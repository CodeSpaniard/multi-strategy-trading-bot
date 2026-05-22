#!/bin/bash
# Sends SIGINT to all running bots for clean shutdown. State is persisted on exit.
# Verifies Alpaca positions after stop — warns if any are still open.
cd "$(dirname "$0")"

for name in scanner-live scanner-paper crypto-coinbase; do
    pid_file="logs/state/${name}.pid"
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            kill -INT "$PID" && echo "SIGINT sent to ${name} (PID $PID)"
        else
            echo "${name} (PID $PID) already dead"
        fi
        rm -f "$pid_file"
    fi
done

sleep 10

PY=${PY:-python3}
"$PY" - <<'PYCHECK'
import os
from dotenv import load_dotenv
from src.broker import Broker, BrokerError

open_found = False
for env, paper in [('.env.paper', True), ('.env.live', False)]:
    if not os.path.exists(env):
        continue
    load_dotenv(env, override=True)
    try:
        b = Broker(paper=paper)
        positions = b.get_all_positions()
    except BrokerError as e:
        print(f"WARN [{'paper' if paper else 'live'}] cannot check positions: {e}")
        continue
    label = 'PAPER' if paper else 'LIVE'
    if positions:
        open_found = True
        print(f"WARN [{label}] {len(positions)} position(s) still open after stop:")
        for p in positions:
            print(f"  {p.symbol}: qty={p.qty} pl=${float(p.unrealized_pl):.4f}")

if not open_found:
    print("All Alpaca positions flat.")
PYCHECK
