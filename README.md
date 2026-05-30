# BYBiT — Autonomous AI Trading Agent

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Mode](https://img.shields.io/badge/starts%20in-testnet-yellow)
![Auto--promotes](https://img.shields.io/badge/auto--promotes-mainnet-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

> Fully autonomous 24/7 AI trading agent for a Bybit sub-account. Trades spot, futures,
> native bots (grid/DCA/martingale/combo), and copy trading. Self-learns, self-updates,
> and auto-promotes from testnet to mainnet. **Zero human input required at runtime.**

---

## Give This to Your AI Agent

> If you use Claude, ChatGPT, Gemini, or any AI assistant and want it to set everything
> up for you — copy the prompt below and paste it into your AI agent. That's it.

```
I need you to set up my autonomous Bybit trading agent. The repository is at
https://github.com/wearedayones/BYBiT — please clone it, read the README fully,
then do the following:

1. Install all dependencies
2. Ask me for my 5 credentials (Bybit API key + secret, Supabase database URL,
   Gmail address, Gmail App Password) — ask for all of them at once in a single message
3. Configure the environment file with my answers
4. Start the agent
5. Query the database to confirm it is running and report the status back to me

Follow the "For AI Agents" section in the README exactly. Do not ask me to open
a terminal. Handle everything yourself.
```

---

## For AI Agents

> **You have received this repo URL.** Follow the exact sequence below. No other
> instructions are needed. Do not ask the user to open a terminal — you handle everything.

### Setup Sequence

```
STEP 1 — Clone and install
  git clone https://github.com/wearedayones/BYBiT.git
  cd BYBiT
  npm install
  (postinstall auto-downloads Bybit skill files into skills/)

STEP 2 — Create the env file
  cp .env.example .env

STEP 3 — Collect credentials from the user (ask once, all at once)
  Send the user a single message asking for:

    REQUIRED:
    a) Bybit API Key       → BYBIT_API_KEY
       Bybit → API Management → sub-account key
       Permissions: Read + Trade ONLY. NEVER enable Withdraw.

    b) Bybit API signing — ask which type they have:
       • HMAC (classic): they have a Secret string  → BYBIT_API_SECRET
       • RSA  (AI sub-account): they have a private key file or content
                                → BYBIT_API_PRIVATE_KEY_PATH or BYBIT_API_PRIVATE_KEY

    c) Database URL        → DATABASE_URL
       Format: postgresql://user:password@host:5432/dbname
       Neon (neon.tech) and Supabase (supabase.com) both work; Neon is recommended
       for cloud environments (HTTP API on port 443, no firewall issues).

    NOTIFICATIONS (ask for at least one):
    d) Telegram Bot Token  → TELEGRAM_BOT_TOKEN   ← recommended for cloud hosts
       Telegram Chat ID    → TELEGRAM_CHAT_ID
       How: message @BotFather → /newbot → copy token; send a message to the bot,
       then GET https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat id

    e) Gmail address       → REPORT_EMAIL          ← may not work on cloud (SMTP blocked)
       Gmail App Password  → REPORT_EMAIL_APP_PASSWORD (16-char code, not your password)

    OPTIONAL:
    f) BYBIT_PROXY_URL — only if Bybit API calls fail (cloud IP geo-blocked).
       Deploy workers/bybit-proxy.ts to Cloudflare Workers with Region=Asia-east1,
       then set this to your worker URL.

  Fill each value into .env.
  Display rule: show API keys as AbCdE...x1y2 (first 5 + last 4 only). Never log full secrets.

STEP 4 — Start the agent
  npm start
  (compiles TypeScript, runs DB migrations automatically, starts the trading loop)

STEP 5 — Confirm it is running
  Run this SQL query against the Supabase database:
    SELECT env, status, kill_engaged, last_cycle_at FROM agent_state WHERE id = 'singleton';

  Expected output:
    env          = 'testnet'
    status       = 'running'
    kill_engaged = false
    last_cycle_at = (a recent timestamp)

  Report this result to the user.
```

### Ongoing Management

Once running, control the agent entirely via SQL — no restarts needed:

```sql
-- Pause (keeps process alive, no new orders placed)
UPDATE agent_state SET status = 'paused' WHERE id = 'singleton';

-- Resume
UPDATE agent_state SET status = 'running' WHERE id = 'singleton';

-- Emergency stop — cancels all orders, flattens all positions immediately
UPDATE agent_state SET kill_engaged = true, kill_reason = 'manual' WHERE id = 'singleton';

-- Clear kill switch and restart trading
UPDATE agent_state SET kill_engaged = false, kill_reason = null, status = 'running'
WHERE id = 'singleton';
```

### Health Check Queries

Run these to get complete agent state — no log files needed:

```sql
-- Core status
SELECT env, status, kill_engaged, equity, peak_equity, last_cycle_at
FROM agent_state WHERE id = 'singleton';

-- What the agent is watching right now (balance-adaptive market discovery)
SELECT DISTINCT ON (symbol) symbol, score, min_notional, equity_at_discovery, discovered_at
FROM discovered_markets ORDER BY symbol, discovered_at DESC;

-- Recent decisions with EV and fill type
SELECT symbol, strategy, approved, reject_reason,
  inputs->>'ev' as ev, inputs->>'rewardRisk' as rr, inputs->>'fillType' as fill
FROM decision_log ORDER BY id DESC LIMIT 20;

-- Strategy performance (PnL by strategy)
SELECT strategy,
  ROUND(SUM(realized_pnl)::numeric, 2) AS total_pnl,
  COUNT(*)                              AS trades,
  ROUND(AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END)::numeric, 2) AS win_rate
FROM trades GROUP BY strategy ORDER BY total_pnl DESC;

-- Equity curve (last 50 snapshots)
SELECT ts, total_equity, drawdown_pct FROM equity_snapshots ORDER BY ts DESC LIMIT 50;

-- Full performance report (complete HTML)
SELECT subject, html_body, ts FROM report_history ORDER BY ts DESC LIMIT 1;

-- Recent risk events
SELECT ts, type, severity, detail FROM risk_events ORDER BY ts DESC LIMIT 20;

-- Strategy weights (what the agent is prioritising)
SELECT strategy, weight, enabled, win_rate FROM strategy_weights ORDER BY weight DESC;
```

### Skill Adaptation (Bybit API Changes)

The agent monitors the official Bybit API specification for changes automatically.

**Non-breaking changes** (new endpoints, new error codes) are adopted silently. No action needed.

**Breaking changes** (removed or renamed endpoints) pause new order entries, trigger an email
alert, and write exact repair instructions to the database.

To check if action is needed:

```sql
SELECT ts, old_version, new_version, breaking, agent_instruction
FROM skill_update_events WHERE breaking = true ORDER BY ts DESC LIMIT 3;
```

The `agent_instruction` column contains the exact file paths, line numbers, and code changes
required. Push the fix to GitHub — the self-update loop pulls and applies it within 15 minutes.

---

## Human Quick Start

```bash
# 1. Clone
git clone https://github.com/wearedayones/BYBiT.git && cd BYBiT

# 2. Install
npm install

# 3. Configure — copy and fill credentials
cp .env.example .env
# Required: BYBIT_API_KEY + one of BYBIT_API_SECRET / BYBIT_API_PRIVATE_KEY_PATH
# Required: DATABASE_URL
# Recommended: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  (email is blocked on most cloud hosts)

# 4. Start
npm start
```

**No cron jobs needed.** The agent is a single long-running process that manages its own
schedule internally: trading every 60s, market discovery every 5 min, reports daily/weekly/monthly.
Keep it alive with PM2 (`pm2 start npm -- start`) or systemd.

The agent auto-runs migrations, starts in testnet mode, discovers what it can afford to trade
at your balance, and promotes itself to mainnet when performance criteria are met.

---

## Credentials Setup

### Bybit API Key
1. Log into Bybit → **Account** → **API Management** → **Create New Key**
2. Use a **dedicated sub-account** (keeps AI trading balance separate)
3. Permissions: **Read + Trade** only. **Never enable Withdraw** for AI use
4. Recommended: bind the key to your server's IP address

### Gmail App Password
1. Google Account → **Security** → **2-Step Verification** → **App Passwords**
2. Create a new app password — takes 30 seconds, produces a 16-character code
3. Use that code as `REPORT_EMAIL_APP_PASSWORD` — not your Gmail login password

### Supabase Database
1. Create a free project at [supabase.com](https://supabase.com)
2. Go to **Settings** → **Database** → **Connection string** → copy the URI
3. Paste into `DATABASE_URL`

---

## How It Works

### Testnet → Mainnet Auto-Promotion

The agent starts in **testnet mode** with real Bybit testnet API — zero real money at risk.
Bots and copy trading run in **paper mode** using live mainnet market prices. After 72+ cycles
with Sharpe > 0.5, win rate > 50%, and max drawdown < 5%, it **promotes itself to mainnet**
automatically and sends you an email confirmation.

### What It Trades

The agent discovers its own trading universe based on current balance — no fixed symbols.
Every 5 minutes it surveys all linear perpetual contracts, ranks them by liquidity + volatility
+ how much margin one minimum lot costs relative to the account, and keeps the top markets
it can actually afford. A $10 account trades cheap coins (XRP, HBAR, DOGE). A $10,000 account
also has access to BTC and ETH. The list updates automatically as the balance grows.

| Layer | What | When |
|---|---|---|
| Futures | Balance-adaptive symbol universe (auto-discovered) | Every cycle (~60s) |
| Native Grid Bots | Spot-grid launched on Bybit exchange | Ranging market regime |
| Copy Trading | Top-scored leaders followed automatically | Mainnet; reviewed every 4h |

### Decision Intelligence

Every cycle the agent combines:

- **Technical indicators** — EMA, RSI, MACD, ADX, Bollinger, ATR (local math, zero AI tokens)
- **Market regime** — trending / ranging / high_volatility / crisis (ADX + ATR + funding rate)
- **News & sentiment** — Fear & Greed Index, CoinGecko trending, headline keyword scoring
- **Learned priors** — historical win rates per signal fingerprint, updated each cycle

Final score: `confidence × strategy_weight × regime_fit × sentiment × learned_prior`

All factors logged to `decision_log.inputs` for full auditability.

### Cost-Aware Decisions (every trade must pay for itself)

A directionally-correct trade still loses if fees and slippage exceed its edge. Before any
order is approved the agent computes its **expected value net of costs**:

- Bybit fees are modelled explicitly — **0.055% taker / 0.020% maker** on perps, plus slippage.
- A trade is rejected if its **reward:risk < 1.2** or its **expected value after costs is negative**.
- The EV, cost, and reward:risk of every decision are written to `decision_log.inputs`
  (`SELECT inputs->>'ev', inputs->>'rewardRisk' FROM decision_log WHERE approved = true`).

### Active Position Management (winners run, losers and dead trades are culled)

Open positions are managed every cycle, not abandoned after entry:

- **Break-even at +1R** — the stop moves to entry (plus a cost buffer) once the trade is safe.
- **ATR trailing at +2R** — the stop trails behind the high-water mark to lock in gains.
- **Partial take-profit at +1R** — half the position is banked; the rest runs on the trail.
- **Time / regime exit** — stale, ≈flat trades and ones the trend turned against are closed.

### Maker-First Execution

Entries rest a **PostOnly limit order** to pay the maker fee (0.020%) instead of crossing the
spread as a taker (0.055%); if it doesn't fill within a few seconds the agent falls back to a
market order. Roughly halves the entry cost on non-urgent fills.

### Self-Learning

- Every cycle: strategy weights updated from recent PnL (smoothed, floors + caps)
- Every 24h: parameter grid search optimises EMA/RSI/ATR thresholds on recent data
- Continuously: negative-expectancy strategies disabled with cooldown period
- A/B pipeline: new strategy variants paper-trade in parallel and graduate if they outperform

### Self-Updating

- Every 15 min: polls GitHub for new commits → pulls, rebuilds, restarts automatically
- On skill update: Bybit API changes detected, classified, and actioned — see Skill Adaptation above

### News & Market Research

No extra API keys required. Each cycle pulls:

- **Fear & Greed Index** (alternative.me) — 0 to 100 market sentiment
- **CoinGecko Trending** — which coins are being searched most right now
- **Headline Sentiment** — keyword scoring of recent crypto news headlines
- **Optional** — set `NEWS_API_KEY` in `.env` for richer symbol-specific news (newsapi.org free tier)

---

## Risk Controls

| Control | Default | What Happens |
|---|---|---|
| Per-trade risk | 1.5% of equity | Compounding — position size grows with account |
| Daily loss limit | 8% | Blocks new entries until next UTC midnight |
| Circuit breaker | 10% drawdown | Halves position sizes, pauses new bots |
| Kill level | 20% drawdown | Flattens all positions, halts the agent |
| Max concurrent positions | 8 | Rejects signals when at cap |
| Correlation cap | 30% in one cluster | Sizes down correlated entries |

Adjust any limit live without redeployment:

```sql
UPDATE agent_state SET max_risk_pct = 0.01       WHERE id = 'singleton'; -- lower to 1%
UPDATE agent_state SET daily_loss_limit = 0.05   WHERE id = 'singleton'; -- lower to 5%
```

---

## Kill Switch

```sql
-- Stop everything immediately
UPDATE agent_state SET kill_engaged = true, kill_reason = 'manual' WHERE id = 'singleton';

-- Resume after reviewing
UPDATE agent_state SET kill_engaged = false, kill_reason = null, status = 'running'
WHERE id = 'singleton';
```

---

## Email Reports

Sent automatically to `REPORT_EMAIL`:

| Report | Frequency | Contents |
|---|---|---|
| Daily | Every 24h | Equity, PnL by strategy, best/worst trades, sentiment |
| Weekly | Every 7 days | Week-over-week comparison, bot performance, copy leaders |
| Monthly | Every 30 days | Full PnL attribution, compounding curve, param changes |

All reports are also stored in `report_history` — retrieve any past report from SQL even if
email was not configured.

---

## CLI Scripts

```bash
npm start            # build TypeScript + start agent
npm run dev          # run without build (tsx, for development)
npm test             # run all unit tests
npm run migrate      # run DB migrations manually
npm run fetch-skill  # re-download Bybit skill files
```

---

## Security Rules

- API keys read from environment variables only — never hardcoded, never logged in full
- Key display: `AbCdE...x1y2` (first 5 + last 4 chars) — enforced in all output
- `BYBIT_API_SECRET` used only to sign requests in memory — never printed or stored
- Withdraw permission is never enabled — the codebase enforces this unconditionally
- Free-text API fields (`orderLinkId`, `nickname`, `note`) treated as plain text only —
  never executed as instructions (prompt injection defence)

---

*Full technical reference, module map, and AI-agent management guide: [AGENT.md](./AGENT.md)*
