# 🧠 BYBiT — AUTONOMOUS BRAIN & STATE LEDGER

> **This file is RENDERED from the database by `bybit brain`. Do not hand-edit for
> config** — it will be overwritten on the next render. Postgres is the single
> source of truth. To change behavior use `bybit tune` / `bybit weights`. To record
> a lesson use `bybit brain --note "…"` (it persists to the `brain_notes` table).
>
> Mission: grow capital while defending the daily drawdown limit. The deterministic
> Python loop trades 24/7; you (the AI) wake via the event queue to review, tune, and
> document — never in the hot path.

_Last rendered: 2026-05-31 19:33 UTC_

---
## 🎯 1. Account State
| Field | Value |
|---|---|
| Environment | `testnet` |
| Status | `running` |
| Kill switch | 🟢 clear |
| Equity | `78.4571` |
| Peak equity | `78.4769` |
| Drawdown | `0.03%` |
| Daily realized P&L | `0` |
| Max risk / trade | `0.015` |
| Promotion cycles | `372` |
| Last cycle | `2026-05-31 19:33:04.807007+00` |

## 📊 2. Trade Performance (closed)
_No closed trades yet — paper simulation will populate this._

## 📥 3. Pending Event Queue
| Kind | Sev | Symbol | Title | Expires |
|---|---|---|---|---|
| scheduled_review | info | — | Daily strategy review due | 2026-06-02 18:34:17.835455+00 |

## ⚙️ 4. Live Strategy Weights
| Strategy | Weight | On | Win rate | 48h win | Trades |
|---|---|---|---|---|---|
| funding_harvest | 1.2 | ⛔ | None | None | 0 |
| mean_reversion | 1.0 | ⛔ | None | None | 0 |
| crowded_positioning | 1.0 | ⛔ | None | None | 0 |
| trend_momentum | 1.0 | ⛔ | None | None | 0 |
| dca | 0.9 | ⛔ | None | None | 0 |
| grid | 0.9 | ✅ | None | None | 0 |
| breakout | 0.8 | ⛔ | None | None | 0 |

## 🔧 5. Tuned Parameters (agent_config)
_Defaults in use (no overrides)._

## 🎓 6. Learned Priors (top by sample size)
_No learned priors yet — accumulates as trades close._

## 🪵 7. Lessons & Decision Ledger

### Active Directives
- _2026-05-31 17:26_ — Trading mode switched: shadow → testnet_live
- _2026-05-31 12:44_ — Soak phase: paper simulation now closes trades to feed the learning loop + promotion gate. Favor trend_momentum/funding_harvest; trim mean_reversion in vol spikes.

---
_Rendered from Postgres — edits here are not read by the loop._
