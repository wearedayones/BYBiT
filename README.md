# BYBiT — Autonomous AI Trading Agent

A fully autonomous 24/7 trading agent for your Bybit sub-account. It trades spot and futures,
runs native grid/DCA/martingale/combo bots, manages copy trading, and continuously improves
itself — all without any human input. Every decision is logged. A hard kill-switch protects
your capital at all times.

---

## Quick Start (10 steps)

```bash
# 1. Clone the repo
git clone https://github.com/wearedayones/BYBiT.git
cd BYBiT

# 2. Install dependencies (also fetches Bybit skill files)
npm install

# 3. Copy the example env file
cp .env.example .env

# 4. Fill in your credentials (open .env in any editor)
#    BYBIT_API_KEY=        ← your Bybit sub-account API key
#    BYBIT_API_SECRET=     ← your Bybit API secret
#    DATABASE_URL=         ← your Supabase connection string
#    REPORT_EMAIL=         ← your Gmail address
#    REPORT_EMAIL_APP_PASSWORD=  ← Gmail App Password

# 5. Start the agent
npm start
```

That's it. The agent:
- Runs migrations automatically on first start
- Starts in **Testnet mode** (safe, no real money at risk)
- Runs paper mode for bots and copy trading (real market data, virtual fills)
- Auto-promotes to **Mainnet** once performance criteria are met (no input needed)
- Sends you daily/weekly/monthly email reports
- Self-updates when you push new code to GitHub

---

## Credentials Setup

### Bybit API Key
1. Log into Bybit → **Account** → **API Management** → **Create New Key**
2. Enable **Read** + **Trade** permissions. **NEVER enable Withdraw**
3. Recommended: bind to your server's IP address
4. Use a **sub-account** with limited balance for safety

### Gmail App Password
1. Go to your Google Account → **Security** → **2-Step Verification** → **App Passwords**
2. Create a new app password (takes 30 seconds)
3. Paste it into `REPORT_EMAIL_APP_PASSWORD` in your `.env`

### Supabase
1. Create a project at [supabase.com](https://supabase.com)
2. Go to **Settings** → **Database** → **Connection string**
3. Paste the connection string into `DATABASE_URL` in your `.env`

---

## How It Works

- **Testnet phase**: trades testnet spot/futures with real Bybit testnet API. Bots and copy
  trading run in paper mode using real mainnet market prices.
- **Auto-promotion**: after 72 cycles (≈3 days) with Sharpe > 0.5, win rate > 50%, and max
  drawdown < 5%, the agent automatically switches to Mainnet. You'll get an email when it happens.
- **Self-learning**: the agent reviews its own trade history every cycle and adjusts strategy
  weights. Strategies with negative expectancy are disabled temporarily.
- **Self-updating**: every 15 minutes the agent checks GitHub for new commits. If found, it
  pulls, rebuilds, and restarts itself automatically.

---

## Risk Controls

| Control | Value | Description |
|---|---|---|
| Per-trade risk | ~1.5% | Max equity risked per position (self-adjusts) |
| Daily loss limit | 8% | Blocks new entries for the rest of the UTC day |
| Circuit breaker | 10% drawdown | Halves position sizes, pauses new bots |
| Kill level | 20% drawdown | Instantly flattens everything and halts the agent |

---

## Kill Switch

To immediately stop all trading and flatten all positions:

```sql
-- Connect to your Supabase database and run:
UPDATE agent_state SET kill_engaged = true, kill_reason = 'manual' WHERE id = 'singleton';
```

To resume:
```sql
UPDATE agent_state SET kill_engaged = false, kill_reason = null, status = 'running' WHERE id = 'singleton';
```

---

## Check Agent Status

```sql
-- Current state
SELECT env, status, kill_engaged, equity, peak_equity, promotion_cycle_count FROM agent_state;

-- Recent decisions
SELECT ts, symbol, action, strategy, confidence, outcome FROM decision_log ORDER BY ts DESC LIMIT 20;

-- Recent trades
SELECT symbol, strategy, realized_pnl, closed_at FROM trades ORDER BY closed_at DESC LIMIT 20;
```

---

## Scripts

```bash
npm run fetch-skill   # Re-download Bybit skill files
npm run migrate       # Run DB migrations manually
npm test              # Run all unit tests
npm run dev           # Dev mode (no build needed)
```

---

## Security

- API keys are read from environment variables only — never stored in code or logs
- Key display follows skill rules: first 5 + last 4 chars only
- All prompt-injection-risk fields (orderLinkId, nicknames) are treated as plain text
- The agent never enables Withdraw permission

---

*See [AGENT.md](./AGENT.md) for the full technical reference.*
