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

- **Per-trade sizing** (`src/sizer.py`) — base size = equity ÷ divisor; a training-wheel cap applies to the first N closed trades; per-trade size growth is capped at 1.5× the previous size; a 7-day peak-to-trough drawdown of ≥10% triggers a sizing freeze. Worth being precise about that last one: it is a **growth cap, not a brake** — while frozen the bot keeps trading at its prior size. It has fired twice, on 2026-06-30 (−20.04%) and 2026-07-07 (−19.32%).
- **Daily realized-loss brake** (`src/daily_loss.py`) — once a UTC day's *realized* losses reach the configured limit, the live scanner opens nothing further that day. Exits, stops and monitoring continue; only new entries stop. The accumulator is persisted with the day it belongs to, so restarting cannot clear a limit that has been reached — the failure mode of the earlier design it replaced. Live on the equity account at $15/day; deliberately disabled on the paper account, whose far larger notional makes that threshold meaningless.
- **Per-position stops** — hard stop, trailing stop, and (for the crypto trend strategy) a take-profit target.
- **Stage-gated risk parameters** — paper and live each have their own YAML config; live always runs with smaller caps than paper by design.

## Operational layer

All three bots are instrumented. What that consists of:

- **Pushover** alerts on order failures and unexpected loop crashes (`src/notifier.py`)
- **Healthchecks.io** dead-man's-switch via a 1-minute heartbeat — the bot pings a unique URL once per healthy loop iteration; if pings stop for >3 minutes, Healthchecks alerts. Around 384,000 healthy loop iterations logged across the three bots as at 2026-09-26.
- **Dedicated `HEARTBEAT` log line** independent of market or position state — silent stalls are caught the same way as crashes.

The split between event alerts and liveness pings was deliberate:

> notifier.py is for user-facing alert events. A healthcheck ping is a machine-facing liveness signal that happens every minute whether anything interesting occurred or not. Different purpose, different cadence — keeping them separate ages better.

## Verification

The parts of this system that can lose money are tested, and the tests are
themselves checked against deliberately broken code — a suite that passes is
worthless if it cannot fail.

- **Mutation testing.** Every safety-critical module has its tests run against
  intentionally defective versions of itself. The sizing tests are checked
  against six defects (threshold loosened, lookback ignored, growth cap removed,
  freeze comparison inverted, drawdown measured from the wrong point); the
  daily-loss brake against ten. All are caught.
- **Controls, not just assertions.** The concurrency test for atomic state
  writes is paired with a control that runs the *old* implementation through the
  same harness and asserts it **does** tear. Without that, a passing test could
  mean the harness had gone blind rather than the code being correct.
- **Measured, not assumed.** State was previously persisted with
  truncate-then-write. A reader hitting that window saw a partial document —
  measured at 156 torn reads in 6,397 (~2.4%) against a document the size of the
  real state file. Writing to a temp file and `os.replace`-ing it into position
  took that to 0 in 7,502.
- **Fail closed.** Where a control cannot establish its own constraint — an
  unreadable state file, a malformed risk limit — it refuses new entries rather
  than assuming it is safe to proceed. What it never does is stop the process:
  stops are enforced inside the poll loop rather than resting at the broker, so
  a bot that will not start is not a halted bot, it is an unmanaged position.

## Status

All three bots are live on the droplet under `systemd`, independently
restartable, with state that survives restart atomically.

Figures as of **2026-09-26**, taken from the brokers rather than from the bot's
own bookkeeping. Percentages are return on deployed capital; absolute balances
are deliberately not published.

| | equity leg (Alpaca) | crypto leg (Coinbase) |
|---|---|---|
| return | **+7.91%** | +5.19% |
| closed trades | **107** | 4 |
| open positions | 1 | 1 |
| win rate | 60.4% (58W/38L over the 96 trades with per-day records) | — |

Period is **2026-04-23 → 2026-09-26**, measured from the first live trade;
benchmark figures run to the 25th, the last close available.

Against the benchmark, as at 2026-09-26 the equity strategy is **behind**. Over
the same window
**SPY returned +9.44%** and **QQQ +14.53%**, so the strategy trails the broad
market by about 1.5 points and the tech-heavy index by about 6.6. On 2026-09-18
it was ahead of SPY by 1.4 points; three trailing stops on 2026-09-22 closed
that gap and then some. Both numbers are in this README's history, which is the
honest way to carry it — a strategy that beats its benchmark in some windows and
trails in others is the normal case, and quoting only the flattering window
would be the dishonest one.

Caveats, because the figures mean little without them. They are
**mark-to-market** — positions are usually open, so the numbers move with them.
The crypto leg's deployed capital was reconstructed rather than
deposit-verified, which makes that percentage soft. And 107 trades over five
months of a generally rising market is not a sample that supports annualizing:
on the recorded curve the equity leg ranged between about +10.4% and +1.1% of
deployed capital.

A useful cross-check: over the window where both accounts are recorded, the
paper account returned +2.13% and the live account +2.45% running the same
strategy at roughly 160× the notional. That closeness suggests the live result
is not an artifact of small-account fills.

**These figures are refreshed occasionally, not continuously.** The as-of date
above is the date they were true; they are not updated on any schedule, so do not
read them as describing the date you happen to be reading this.

This README reflects current state, not aspiration.

## Tech stack

Python 3.12 · pandas · [`alpaca-py`](https://github.com/alpacahq/alpaca-py) · [`coinbase-advanced-py`](https://github.com/coinbase/coinbase-advanced-py) · `systemd` on Ubuntu LTS (DigitalOcean) · Pushover · Healthchecks.io

## Repo layout

```
├── src/              bot source — brokers, strategies, main loops, sizer, notifier, healthcheck
├── configs/          per-bot YAML configs (paper / live / crypto variants)
├── backtests/        backtest scripts for each strategy
├── tools/            P&L dashboard, read-only status reporter, and the test suites
├── start_bots.sh     local launcher for development only — the bots run on the droplet
├── stop_bots.sh      clean SIGINT shutdown
└── requirements.txt
```

## Disclaimer

This repository is shared for portfolio purposes. The code is not investment advice, and trading carries substantial risk of loss; live deployment of any version is at the operator's own risk.

---

Ariel Sama · McCombs MBA '26 · Austin, TX
[github.com/CodeSpaniard](https://github.com/CodeSpaniard)
