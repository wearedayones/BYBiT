# BYBiT Skill — Machine-Readable Manifest

**Skill ID:** `bybit-trading`
**Version:** 2.1.0
**Language:** Python 3.11+
**CLI entrypoint:** `bybit` (console script → `bybit_agent.cli:app`)
**Architecture:** Deterministic 24/7 service + durable AI event queue + universal CLI

---

## What This Skill Does

Autonomous Bybit sub-account management. A Python service runs 24/7 doing all continuous
work deterministically. The AI agent that installs the repo (Claude Code, Codex, OpenClaw,
Hermes, or any agent with shell access) is the brain — woken only via a durable event queue
for 4 trigger types. **No external LLM API. No in-process model calls. No `ANTHROPIC_API_KEY`.**

---

## Required Environment Variables

```
# Required
BYBIT_API_KEY=<sub-account API key — Read + Trade only, NEVER Withdraw>
DATABASE_URL=<Neon / Supabase / any Postgres URL>

# Authentication — set exactly ONE signing method
BYBIT_API_SECRET=<HMAC secret>          # OR
BYBIT_API_PRIVATE_KEY=<inline PEM>      # OR (preferred for RSA)
BYBIT_API_PRIVATE_KEY_PATH=<path>       # OR

# Notifications (strongly recommended — one or both)
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_CHAT_ID=<chat/user id>

# Optional
BYBIT_ENV=testnet                       # testnet (default) | mainnet
BYBIT_PROXY_URL=<Cloudflare Worker URL> # required if Bybit's CDN blocks your IP
NEWS_API_KEY=<newsapi.org key>          # headline sentiment (free tier)
GITHUB_TOKEN=<PAT>                      # for private repos / higher rate limits
LOG_LEVEL=info                          # debug | info | warn | error
REPORT_EMAIL=<gmail address>            # daily/weekly/monthly HTML reports
REPORT_EMAIL_APP_PASSWORD=<16-char>     # Gmail App Password (auto-disabled behind proxy)
```

**Security rules (non-negotiable):**
- NEVER hardcode any secret. Read from environment only.
- NEVER enable Withdraw — Read + Trade only. No exceptions.
- RSA private key contents must never appear in logs, output, or generated code.
- Key display rule: `AbCdE...x1y2` (first 5 + last 4 chars). Use `bybit_agent.core.logger.mask_secret()`.
- `BYBIT_API_SECRET` used only to sign requests in memory — never printed or stored.

---

## Install & Boot Sequence

```bash
git clone https://github.com/wearedayones/BYBiT.git
cd BYBiT
pip install -e .          # installs bybit CLI + all deps
bybit migrate             # apply all migrations (idempotent)
bybit doctor              # validate env + DB connectivity + JSONB round-trip
bybit run                 # launch 24/7 service (blocks; run in background/pm2/systemd)
```

---

## CLI Command Reference

All commands accept `--json` for machine-readable output.
The CLI is a **read + tune surface only** — it never places orders directly.

### Operations / Status

| Command | Description |
|---|---|
| `bybit migrate` | Apply all `migrations/*.sql` in order (idempotent) |
| `bybit doctor [--deep] [--dry-run] [--json]` | Validate env, DB over HTTPS:443, JSONB round-trip. `--deep` runs the live health Doctor (process, kill state, cycle freshness, drawdown, signal flow, event backlog), auto-fixes safe faults, and escalates judgment calls as events; `--dry-run` reports without acting |
| `bybit run [--testnet/--mainnet]` | Start the 24/7 trading service |
| `bybit status [--json]` | Agent state: env, equity, drawdown, status, kill flag |
| `bybit positions [--json]` | Open positions from the exchange |
| `bybit report [--period daily\|weekly\|monthly] [--json]` | Performance digest + signal analytics + auto-diagnosis (bottleneck + recommended fix) |
| `bybit watch [--interval N] [--no-restart] [--json]` | Doctor-mode watchdog: every N sec runs the full Doctor — auto-restarts a dead process, resets legacy killed-state, and escalates kill/drought/drawdown/backlog findings to the event queue. Any agent can run it as the on-call doctor |
| `bybit pause` | Set agent status = 'paused' (loop skips cycles) |
| `bybit resume` | Set agent status = 'running' |
| `bybit kill [--reason TEXT] [--force]` | Cancel all orders + flatten all positions + halt |

### Event Queue (AI drain loop)

| Command | Description |
|---|---|
| `bybit events [--kind K] [--severity S] [--limit N] [--json]` | List pending events |
| `bybit event <UUID> [--json]` | Full event detail: context, options, default_action |
| `bybit decide <UUID> --action <A> [--param k=v …] [--json]` | Resolve an ambiguous_decision |
| `bybit resolve <UUID> --action <A> [--json]` | Resolve any other event kind |

**Event kinds:** `scheduled_review` · `risk_escalation` · `market_event` · `ambiguous_decision`
**Severities:** `info` · `warning` · `critical`
**Status values:** `pending` · `claimed` · `resolved` · `auto_resolved`

Every event carries `default_action` + `expires_at`. If undrained, the loop applies the safe
default automatically (ambiguous → reject, risk → acknowledge). The AI's absence never blocks trading.

### Tuning

| Command | Description |
|---|---|
| `bybit tune [--list] [--json]` | Show all tunable params with current values and bounds |
| `bybit tune --set KEY=VALUE [--set KEY2=V2 …] [--json]` | Validate + write to `agent_config` |
| `bybit weights [--json]` | Show strategy weights |
| `bybit weights --set strategy=weight [--json]` | Set a weight (0.0–2.0) |
| `bybit weights --toggle strategy [--json]` | Enable / disable a strategy |
| `bybit review` | Run an immediate SelfReview pass |

### Memory, P&L & Learning

| Command | Description |
|---|---|
| `bybit brain [--show] [--path P] [--json]` | Render the DB-backed `brain.md` memory ledger (account state, queue, weights, learned priors, lessons) from Postgres. `--show` prints to stdout instead of writing |
| `bybit brain --note "TEXT" [--category lesson\|decision\|directive\|observation]` | Persist a durable note to the `brain_notes` table (survives restarts; never lost) |
| `bybit pl [--json]` | Real-time P&L for open positions (live + paper) |
| `bybit signal <SYMBOL> [--category linear] [--json]` | Run the full signal pipeline for one symbol and print decisions (no execution) |
| `bybit train [--window DAYS] [--json]` | Trigger a full batch retrain of `learned_signals` from trade history |

> **brain.md is rendered FROM the DB — never parsed back into config.** Postgres stays the
> single source of truth; the 60-second trading loop never reads or writes `brain.md`. Config
> changes still flow only through `bybit tune` / `bybit weights`.

### Tunable Parameters (PARAM_WHITELIST)

| Param | Range | Description |
|---|---|---|
| `maxRiskPct` | 0.001–0.03 | Max fraction of equity risked per trade |
| `dailyLossLimit` | 0.01–0.15 | Daily loss limit as fraction of day-start equity |
| `killLevelPct` | 0.05–0.40 | Peak-to-current drawdown that triggers kill switch |
| `circuitBreakerPct` | 0.03–0.20 | Drawdown that halves position sizing |
| `makerWaitMs` | 0–30000 | Milliseconds to wait for maker fill before taker fallback |
| `makerOffsetPct` | 0.0–0.005 | Fraction inside the touch to rest the maker limit |
| `breakevenAtR` | 0.5–3.0 | R-multiple to move stop to break-even |
| `trailStartR` | 1.0–5.0 | R-multiple at which ATR trailing begins |
| `partialTpAtR` | 0.5–3.0 | R-multiple for partial TP (closes 50% of position) |
| `maxHoldCycles` | 10–200 | Cycles before a stale flat position is closed |
| `defaultLeverage` | 1–20 | Exchange leverage applied to new positions (1 = none). Lets small accounts reach minimum lot sizes; risk budget is still computed from real equity |

For accounts under **$50**, discovery automatically falls back to a micro-capital symbol
universe (DOGE, SHIB, PEPE, FLOKI, BONK, WIF, ADA, XRP, TRX, LTC) so minimum lot sizes are
affordable. Combine with `defaultLeverage` to make $1–$10 accounts tradable.

Changes take effect the next cycle — no restart required.

---

## JSON Output Shapes

### `bybit status --json`
```json
{
  "env": "testnet",
  "status": "running",
  "kill_engaged": false,
  "kill_reason": null,
  "equity": 1234.56,
  "drawdown_pct": 1.23,
  "open_positions": 2,
  "last_cycle_at": "2025-01-15T10:30:00+00:00",
  "promotion_cycle_count": 18
}
```

### `bybit events --json` (one item)
```json
{
  "id": "uuid",
  "kind": "ambiguous_decision",
  "severity": "info",
  "status": "pending",
  "symbol": "BTCUSDT",
  "title": "Ambiguous signal: trend_momentum on BTCUSDT (score=0.54)",
  "summary": "...",
  "default_action": "reject",
  "expires_at": "2025-01-15T11:00:00+00:00",
  "options": [
    {"action": "approve", "label": "Approve — execute this signal next cycle"},
    {"action": "reject",  "label": "Reject — discard this signal"},
    {"action": "hold",    "label": "Hold — re-evaluate next cycle"}
  ],
  "context": {"signal": {...}, "score": 0.54, "strategy": "trend_momentum"}
}
```

### `bybit tune --list --json` (one item)
```json
{
  "param": "maxRiskPct",
  "db_key": "max_risk_pct",
  "type": "float",
  "min": 0.001,
  "max": 0.03,
  "current": 0.015,
  "desc": "Max fraction of equity risked per trade (0.001–0.03)"
}
```

---

## Operating Contract

1. **The loop never blocks on the AI.** Every event has `default_action` + `expires_at`.
2. **The AI never places orders directly.** Only the loop executes. `approve` resolves an
   `ambiguous_decision` event; the loop reads the resolution next cycle and decides.
3. **`bybit tune` is the only write path for params.** Bounds are enforced; no unchecked
   values reach `agent_config`.
4. **`bybit kill` is the instant rollback lever.** Cancel-all + flatten + halt. Idempotent.
5. **Signing is byte-identical to the TypeScript reference.** HMAC-SHA256 or RSA-SHA256 PKCS#1 v1.5.
6. **DB is the source of truth.** The loop reads `agent_state` every cycle; tune/weights/pause
   changes take effect without any restart or process signal.
7. **Paper mode is always-on until Phase 6 cutover.** `decision_log.is_paper = true` while in
   shadow mode. Flip `agent_state.env = 'mainnet'` to switch to live execution.

---

## The AI's Recurring Session Job

```
bybit status            ← check equity, drawdown, kill flag
bybit events            ← list pending (kind/severity filter as needed)
bybit event <id>        ← read each event's context + options
bybit decide/resolve    ← drain the queue (approve/reject/acknowledge/pause)
bybit report --period daily  ← pull performance digest
bybit tune --list       ← see current params vs bounds
bybit tune --set KEY=V  ← adjust within bounds based on report findings
bybit weights           ← review strategy allocation
bybit weights --set/--toggle  ← rebalance if data supports it
```

Escalate via `bybit pause` or `bybit kill` on critical risk signals.
The agent **never** pushes code, calls external APIs, or places orders directly.

---

## Official Bybit API Reference (embedded)

The full official Bybit Exchange AI skill is embedded in `skills/`. These markdown
modules are the authoritative API reference for all Bybit v5 endpoints. Any AI agent
can read them to look up exact request/response shapes before making manual API calls.

| Module | Topic |
|---|---|
| `market` | Klines, tickers, open interest, L/S ratio, historical volatility |
| `derivatives` | Perpetual futures orders, positions, order history, executions |
| `spot` | Spot orders, margin trading |
| `account` | Balances, transfers, sub-accounts, unified account management |
| `trading-bot` | Spot/futures grid bots, DCA bot, martingale |
| `strategy` | TWAP, Iceberg, Chase, POV algorithmic execution orders |
| `copy-trading` | Leader discovery, follower binding, copy settings |
| `earn` | Savings, staking, liquidity mining, flexible products |
| `advanced` | WebSocket streams, institutional loans, RFQ block trades |
| `alpha-trade` | DEX token swaps and on-chain token access |
| `fiat` | P2P trading, fiat conversion, bank transfers |
| `tradfi` | Tokenised equities, commodities, MT5 copy trading |

```
bybit skill list                   ← list all embedded modules
bybit skill show derivatives       ← print derivatives API reference
bybit skill show strategy          ← print algo order (TWAP/Iceberg) spec
bybit skill version                ← show embedded version
bybit skill refresh                ← pull latest from bybit-exchange/skills on GitHub
```

The autonomous service implements the full `market`, `derivatives`, `spot`, `account`,
`trading-bot`, `strategy`, and `copy-trading` modules in Python. The remaining modules
(`earn`, `advanced`, `alpha-trade`, `fiat`, `tradfi`) are embedded as reference only —
use `bybit skill show <module>` to read endpoint specs for manual/interactive calls.

### Additional CLI Commands (v2.1.0)

| Command | Description |
|---|---|
| `bybit executions [--symbol S] [--limit N] [--json]` | Recent trade fills from `/v5/execution/list` |
| `bybit algo list [--symbol S] [--json]` | Active TWAP/Iceberg/Chase/POV orders |
| `bybit skill list [--json]` | List embedded official skill modules |
| `bybit skill show <module> [--json]` | Print a module's full API reference |
| `bybit skill refresh [--json]` | Update modules from `bybit-exchange/skills` on GitHub |
| `bybit skill version [--json]` | Show embedded version |
