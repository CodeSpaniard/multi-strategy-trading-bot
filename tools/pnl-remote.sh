#!/bin/bash
# Run the P&L dashboard on the remote droplet over SSH.
# Configure DROPLET and KEY as env vars, or edit the defaults below.
# Usage: DROPLET=bot@host KEY=~/.ssh/key ./tools/pnl-remote.sh [--compact]
DROPLET=${DROPLET:-bot@your-droplet-host}
KEY=${KEY:-~/.ssh/your-deploy-key}
ssh -i "$KEY" "$DROPLET" "cd /home/bot/trading-bot && source .venv/bin/activate && python tools/pnl.py $*"
