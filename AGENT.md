# BYBiT Agent — AI Onboarding Guide

This file is the primary reference for any AI coding agent (or human developer) entering this
repository. Read it fully before making any changes.

## What This Project Does

A fully autonomous 24/7 AI trading agent for a Bybit sub-account. It trades spot and futures,
runs native Bybit bots (grid/DCA/martingale/combo), manages copy trading, and continuously
improves its own strategy weights through self-review. All state lives in Supabase (PostgreSQL)
so it survives full server migrations.

## Critical Rules

1. **NEVER hardcode API keys, secrets, or any credentials** — always read from `process.env`.
2. **NEVER enable Withdraw on the API key** — Read + Trade only (security rule from skill).
3. **All Bybit requests must be HMAC-signed** — follow `src/exchange/signer.ts` exactly.
4. **Rate limits must be respected** — all requests go through `src/exchange/rateLimiter.ts`.
5. **Every trade decision must be logged** to `decision_log` before execution.
6. **Kill switch** (`src/control/KillSwitch.ts`) must be callable from anywhere and must be idempotent.
7. **No float math for money** — use `decimal.js` for all PnL and sizing calculations.
8. **Bots and copy-trading endpoints are mainnet-only** — paper mode (`src/paper/PaperExecutor.ts`)
   handles them in testnet phase.
9. **The agent auto-promotes from testnet → mainnet** when performance criteria are met (see
   `src/control/PromotionManager.ts`). No manual intervention required.
10. **The agent self-updates** from GitHub (see `src/updater/RepoUpdater.ts`) and also updates the
    Bybit skill file (see `src/skill/skillUpdater.ts`).

## Environment Variables (minimum required)

| Variable | Purpose |
|---|---|
| `BYBIT_API_KEY` | Bybit sub-account API key (Read + Trade only) |
| `BYBIT_API_SECRET` | Bybit API secret |
| `DATABASE_URL` | Supabase/PostgreSQL connection string |
| `REPORT_EMAIL` | Gmail address for daily/weekly/monthly reports |
| `REPORT_EMAIL_APP_PASSWORD` | Gmail App Password (not your main password) |
| `GITHUB_TOKEN` | Optional — for private repos or higher GitHub API rate limit |

Copy `.env.example` → `.env` and fill these in. No other configuration needed.

## How to Run

```bash
npm install          # installs deps + fetches Bybit skill files
npm start            # build + run agent
npm run dev          # run without build (tsx, for development)
npm test             # run all tests
npm run migrate      # run DB migrations manually
npm run fetch-skill  # re-download Bybit skill files
```

## Module Map

```
src/
├── index.ts                  — entrypoint: boot sequence → agent loop
├── config/
│   ├── env.ts                — zod validation of environment variables
│   └── constants.ts          — rate limits, URLs, risk defaults
├── skill/
│   ├── skillLoader.ts        — reads skills/SKILL.md + modules/*, exposes config
│   └── skillUpdater.ts       — checks remote version, self-updates skill files
├── exchange/
│   ├── signer.ts             — HMAC-SHA256 signing (pure, unit-tested)
│   ├── rateLimiter.ts        — GET/POST rate limiting with backoff
│   ├── BybitClient.ts        — all Bybit REST calls (handles both response envelopes)
│   └── types.ts              — TypeScript types for all API shapes
├── market/
│   ├── MarketDataService.ts  — klines, tickers, orderbook, indicators, regime classification
│   └── indicators.ts         — adapter over technicalindicators library
├── strategy/
│   ├── Strategy.ts           — Strategy interface + Signal/Action types
│   ├── DecisionEngine.ts     — runs all strategies, weights signals, logs to DB
│   └── impl/
│       ├── TrendMomentum.ts  — EMA cross + MACD + ADX
│       ├── MeanReversion.ts  — Bollinger + RSI
│       ├── Breakout.ts       — range breakout
│       └── FundingHarvest.ts — delta-neutral funding rate harvesting
├── risk/
│   ├── RiskManager.ts        — per-trade cap, daily loss, kill-level, circuit breaker
│   └── sizing.ts             — compounding position size math (decimal.js)
├── portfolio/
│   └── PortfolioManager.ts   — wallet balance, positions, equity tracking, snapshots
├── bots/
│   └── BotManager.ts         — native grid/DCA bot lifecycle (real on mainnet, paper on testnet)
├── copy/
│   ├── CopyTradingManager.ts — leader discovery, follow/drop, performance tracking
│   └── leaderScoring.ts      — leader ranking math
├── review/
│   └── SelfReview.ts         — updates strategy weights + learned signals from trade history
├── control/
│   ├── AgentLoop.ts          — main cycle orchestrator
│   ├── KillSwitch.ts         — flatten all positions/orders/bots, halt loop
│   └── PromotionManager.ts   — auto-promotes testnet → mainnet on criteria pass
├── updater/
│   └── RepoUpdater.ts        — polls GitHub for new commits, pulls + rebuilds + restarts
├── reports/
│   ├── ReportBuilder.ts      — queries DB and builds HTML report
│   └── emailer.ts            — sends via nodemailer (Gmail SMTP)
└── persistence/
    └── db.ts                 — postgres connection pool
```

## Database Schema (key tables)

| Table | Purpose |
|---|---|
| `agent_state` | Singleton row: env, kill_engaged, equity, risk params, promotion state |
| `strategy_weights` | Per-strategy weight, enabled flag, performance stats (tuned by SelfReview) |
| `decision_log` | Every decision before execution: signal, confidence, regime, outcome |
| `orders` | Every order placed (real or paper), linked to decisions |
| `trades` | Closed trade records with realized PnL |
| `positions` | Open positions (reconciled from exchange) |
| `bot_instances` | Native bot configs and exchange IDs |
| `copy_leaders` | Copy trading leaders: status, score, investment |
| `equity_snapshots` | Equity curve over time |
| `learned_signals` | Learned win-rates per strategy/regime combo |
| `risk_events` | All risk limit hits, circuit breakers, kill switches |
| `repo_updates` | Record of every self-update from GitHub |
| `report_history` | Every email report sent |

## Key Behaviors

- **Testnet first**: agent starts in testnet, auto-promotes to mainnet when criteria met (72 cycles,
  Sharpe > 0.5, win rate > 50%, max drawdown < 5%).
- **Paper mode**: on testnet, bot and copy-trading calls are simulated using real market data.
- **Self-update**: every 15 minutes, checks GitHub for new commits and applies them automatically.
- **Kill switch**: set `kill_engaged = true` in `agent_state` to halt everything immediately. Clear
  it to resume. The loop checks this at the top of every cycle.
- **Reports**: daily/weekly/monthly HTML reports via Gmail. Stored in `report_history` even if email
  is not configured.

## Bybit Skill

The file `skills/SKILL.md` plus `skills/modules/*.md` are the authoritative reference for:
- API authentication and signing (follow `src/exchange/signer.ts`)
- Rate limits (follow `src/exchange/rateLimiter.ts`)
- Error codes and their meanings
- Endpoint parameters and constraints
- Security rules (never hardcode keys, never enable Withdraw)

The skill is auto-updated at startup via `src/skill/skillUpdater.ts`. If a breaking change is
detected, the agent pauses new entries and fires an email alert.
