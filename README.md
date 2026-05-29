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
  Send the user a single message asking for all 5 values:

    a) Bybit API Key       → BYBIT_API_KEY
       Where to get it: Bybit → API Management → sub-account key
       Permissions: Read + Trade ONLY. NEVER enable Withdraw.

    b) Bybit API Secret    → BYBIT_API_SECRET

    c) Supabase URL        → DATABASE_URL
       Where to get it: supabase.com → project → Settings → Database → Connection string
       Format: postgresql://user:password@host:5432/dbname

    d) Gmail address       → REPORT_EMAIL
       (for daily/weekly/monthly performance reports)

    e) Gmail App Password  → REPORT_EMAIL_APP_PASSWORD
       Where to get it: Google Account → Security → 2-Step Verification → App Passwords
       This is NOT your Gmail password — it is a 16-character app-specific code.

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

-- Strategy performance (PnL by strategy)
SELECT strategy,
  ROUND(SUM(realized_pnl)::numeric, 2)  AS total_pnl,
  COUNT(*)                               AS trades,
  ROUND(AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END)::numeric, 2) AS win_rate
FROM trades GROUP BY strategy ORDER BY total_pnl DESC;

-- Equity curve (last 50 snapshots)
SELECT ts, total_equity, drawdown_pct FROM equity_snapshots ORDER BY ts DESC LIMIT 50;

-- Full performance report (complete HTML — one row covers everything)
SELECT subject, html_body, ts FROM report_history ORDER BY ts DESC LIMIT 1;

-- Recent risk events
SELECT ts, type, severity, detail FROM risk_events ORDER BY ts DESC LIMIT 20;

-- Current market sentiment
SELECT ts, fear_greed_index, fear_greed_label, global_sentiment, trending_symbols
FROM market_sentiment ORDER BY ts DESC LIMIT 1;

-- Strategy weights (what the agent is prioritising right now)
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

# 3. Configure — copy and fill 5 values
cp .env.example .env

# 4. Start
npm start
```

The agent auto-runs migrations, starts in testnet mode, and promotes itself to mainnet
when performance criteria are met. No further setup required.

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

| Layer | Instruments | Frequency |
|---|---|---|
| Spot + Futures | BTCUSDT, ETHUSDT, SOLUSDT | Every cycle (~60s adaptive) |
| Native Bots | Grid, DCA, Martingale, Combo | Activated by market regime |
| Copy Trading | Top-scored leaders | Auto-followed and dropped by performance |

### Decision Intelligence

Every cycle the agent combines:

- **Technical indicators** — EMA, RSI, MACD, ADX, Bollinger, ATR (local math, zero AI tokens)
- **Market regime** — trending / ranging / high_volatility / crisis (ADX + ATR + funding rate)
- **News & sentiment** — Fear & Greed Index, CoinGecko trending, headline keyword scoring
- **Learned priors** — historical win rates per signal fingerprint, updated each cycle

Final score: `confidence × strategy_weight × regime_fit × sentiment × learned_prior`

All factors logged to `decision_log.inputs` for full auditability.

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
