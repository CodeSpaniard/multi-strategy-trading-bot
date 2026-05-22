# Multi-strategy trading bot

A live automated trading system running on a DigitalOcean droplet against modest real capital. Two complementary strategies — an intraday equity dip-scanner (Alpaca) and a daily MA-crossover crypto trend follower (Coinbase) — coordinated by shared risk logic and a deliberately staged operational layer.

## How this was built

This codebase is the artifact of a deliberate dual-agent AI workflow:

- **Codex CLI (OpenAI)** sets the plan, reviews work, and prompts the next step
- **Claude Code (Anthropic)** edits the files, runs the checks, and deploys to the droplet
- I sit between them — deciding when an idea ships, when it gets revised, and which agent's read to trust when they disagree

For a trading system that touches real money, having a second AI critique changes before they ship has been worth the small overhead. The architectural decisions are mine; the line-level Python is largely AI-generated and AI-reviewed.

## What it does

Three bot processes, each running independently under `systemd` on the droplet:

- **`scanner-paper`** (Alpaca) — equities dip detection on a basket of US large-caps. Buys on ≥5% intraday pullback with bounce confirmation; exits on trailing stop or hard stop. Paper account.
- **`scanner-live`** (Alpaca) — same strategy on a live account with deliberately smaller position caps.
- **`crypto-coinbase`** (Coinbase) — daily MA20 / MA100 trend follower on BTC-USDC and ETH-USDC. Long-only. Holds through bullish trends; exits on death cross, hard stop, or trailing stop.

Each bot polls on its own cadence (60s for equities, hourly for crypto), maintains its own state file, and is independently restartable.

## Risk management

Risk is enforced in code, not in commentary. The pieces that matter:

- **Per-trade sizing** (`src/sizer.py`) — base size = equity ÷ divisor; a training-wheel cap applies to the first N closed trades; per-trade size growth is capped at 1.5× the previous successful size; a 7-day peak-to-trough drawdown of ≥10% triggers a position-sizing freeze.
- **Per-position stops** — hard stop, trailing stop, and (for the crypto trend strategy) a take-profit target.
- **Stage-gated risk parameters** — paper and live each have their own YAML config; live always runs with smaller caps than paper by design.

## Operational layer

The paper bot has a first operational layer in place. Promotion to live and crypto is staged, not parallel:

- **Pushover** alerts on order failures and unexpected loop crashes (`src/notifier.py`)
- **Healthchecks.io** dead-man's-switch via a 1-minute heartbeat — the bot pings a unique URL once per healthy loop iteration; if pings stop for >3 minutes, Healthchecks alerts. Around 40,000 successful pings to date.
- **Dedicated `HEARTBEAT` log line** independent of market or position state — silent stalls are caught the same way as crashes.

The split between event alerts and liveness pings was deliberate:

> notifier.py is for user-facing alert events. A healthcheck ping is a machine-facing liveness signal that happens every minute whether anything interesting occurred or not. Different purpose, different cadence — keeping them separate ages better.

## Status

**Deployed (paper bot):** alerting + dead-man's-switch fully live.
**On the runway:** promote the same pattern to `scanner-live`, then to `crypto-coinbase`. Restart-rate alerts and a daily summary come after.

This README reflects current state, not aspiration.

## Tech stack

Python 3.10 · pandas · [`alpaca-py`](https://github.com/alpacahq/alpaca-py) · [`coinbase-advanced-py`](https://github.com/coinbase/coinbase-advanced-py) · `systemd` on Ubuntu LTS (DigitalOcean) · Pushover · Healthchecks.io

## Repo layout

```
├── src/              bot source — brokers, strategies, main loops, sizer, notifier, healthcheck
├── configs/          per-bot YAML configs (paper / live / crypto variants)
├── backtests/        backtest scripts for each strategy
├── tools/            P&L dashboard and remote-status helpers
├── start_bots.sh     local launcher (laptop mode, with caffeinate wrapping)
├── stop_bots.sh      clean SIGINT shutdown
└── requirements.txt
```

## Disclaimer

This repository is shared for portfolio purposes. The code is not investment advice, and trading carries substantial risk of loss; live deployment of any version is at the operator's own risk.

---

Ariel Sama · McCombs MBA '26 · Austin, TX
[github.com/CodeSpaniard](https://github.com/CodeSpaniard)
