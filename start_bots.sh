#!/bin/bash
# Launches all three bots as background processes.
# Each bot is wrapped in `caffeinate -di` so macOS won't sleep or throttle
# the process — critical for not missing the end-of-day scan window.
#
# Layout:
#   logs/state/{bot}.pid          # process ID for stop script
#   logs/manual/{bot}.log         # stdout/stderr capture
#   logs/daily/YYYY-MM-DD.{bot}.log  # app-level log, rotated by date
#   logs/state/{bot}.state.json   # persisted strategy state
set -e
cd "$(dirname "$0")"
PY=${PY:-python3}

mkdir -p logs/state logs/manual logs/daily logs/archive

start_bot() {
    local name=$1
    local module=$2
    local config=$3
    local env_flag=$4
    nohup caffeinate -di "$PY" -m "$module" --config "$config" $env_flag \
        >> "logs/manual/${name}.log" 2>&1 &
    echo $! > "logs/state/${name}.pid"
    echo "Started ${name}: PID $(cat logs/state/${name}.pid)"
}

start_bot "scanner-live"     "src.scanner_main"         "configs/config.scanner.live.yaml"   "--env .env.live"
start_bot "scanner-paper"    "src.scanner_main"         "configs/config.scanner.paper.yaml"  "--env .env.paper"
start_bot "crypto-coinbase"  "src.crypto_coinbase_main" "configs/config.crypto.coinbase.yaml" ""
