# BYBiT — Claude Code Operating Guide

> For general agent operating instructions, read AGENT.md.
> This file covers Claude Code–specific behavior: hooks, permissions, session structure.

---

## What This Repo Is

A Python trading skill. A 24/7 deterministic service runs continuously (`bybit run`). You are
the brain — woken via a Postgres-backed event queue when judgment is needed. You operate the
skill through the `bybit` CLI. No external LLM API. No in-process model calls. You ARE the
intelligence.

---

## Security Rules (Claude Code Specific)

These are in addition to the six unconditional rules in AGENT.md.

- **Never write `.env` values into code.** Always `os.environ.get(...)` or pydantic-settings.
- **Never commit `.env`.** It is in `.gitignore`. If you accidentally stage it, un-stage it.
- **Never echo secrets in tool output.** Use `mask_secret()` from `bybit_agent.core.logger`.
- **Never use `--no-verify`** on git commits. The pre-commit hooks exist for a reason.
- **Never push to `main` or `master`.** All development goes to a feature branch.

---

## Session Start Checklist

Every Claude Code session touching this repo:

```bash
bybit status --json          # confirm loop is running, equity > 0, kill_engaged = false
bybit events --json          # see what's pending before doing anything else
```

If `kill_engaged = true`: do NOT restart the loop without understanding why. Read
`bybit status --json` for `kill_reason`, then check recent `decision_log` entries.

---

## Install (one-time)

```bash
git clone https://github.com/wearedayones/BYBiT.git
cd BYBiT
pip install -e .
bybit migrate
bybit doctor
```

Then collect credentials per AGENT.md Step 2 and launch `bybit run`.

---

## Recurring Session Job (copy-paste this)

```bash
bybit status --json
bybit events --json
# for each pending event:
bybit event <UUID> --json
bybit decide <UUID> --action approve   # or reject / hold
bybit resolve <UUID> --action reviewed # for scheduled_review / market_event
bybit report --period daily --json
bybit tune --list --json
# if report shows a performance pattern:
bybit tune --set maxRiskPct=0.010
bybit weights --json
bybit weights --set trend_momentum=1.1
```

Never place orders directly. Never write to `agent_state` or `agent_config` outside `bybit tune`.

---

## Making Code Changes

All logic lives under `bybit_agent/`. The CLI is read-only from the agent perspective — it
reads DB state, resolves events, and writes to `agent_config` / `strategy_weights`.

Before any commit:

```bash
pytest                        # all 104 tests must pass
bybit doctor                  # env + DB + signing still valid
```

Commit to the feature branch. Push. Do not PR unless the user explicitly asks.

---

## Key Design Invariants (do not break these)

| Invariant | Where enforced |
|---|---|
| CLI never places orders directly | `cli.py` only calls DB, never `BybitClient.place_order()` |
| `is_paper = True` until Phase 6 cutover | `DecisionEngine(paper=True)` in shadow mode |
| Signing is byte-identical to TS reference | `signer.py` + signing golden-vector tests |
| NeonHttpClient uses HTTPS:443 only | `db.py` — TCP 5432/6543 blocked in cloud |
| PARAM_WHITELIST bounds always enforced | `validate_param()` in `config/tuning.py` |
| Every event has `default_action` + `expires_at` | `events/queue.py` — loop never blocks on AI |
| `bybit kill` is idempotent | `kill_switch.py` — second call is a no-op |

---

## Permissions You Will Need

Claude Code permission prompts to expect during normal operation:

| Tool | Why |
|---|---|
| `Bash: bybit …` | CLI commands — always approve |
| `Bash: pytest` | Run test suite |
| `Bash: git …` | Commits, pushes, status |
| `Bash: pip install -e .` | After dependency changes |
| `Read: .env` | Reading credentials to verify (show masked only) |
| `Edit: bybit_agent/**` | Code changes |

Deny any tool call that:
- Writes to `.env` with raw secret values visible in the diff
- Calls `BybitClient` methods directly from a script (not through the service)
- Enables Withdraw permissions on any API key

---

## Event Response Playbook

| Event kind | Default | When to override |
|---|---|---|
| `scheduled_review` | auto-acknowledge after 48h | `bybit resolve <id> --action reviewed` after tuning |
| `risk_escalation` | acknowledge | `bybit pause` if you want to investigate manually |
| `market_event` | hold | `bybit resolve <id> --action reduce_weight` on significant spike |
| `ambiguous_decision` | reject | `bybit decide <id> --action approve` if EV + context look good |

Read the full context with `bybit event <UUID> --json` before deciding. The `context` field
contains the raw signal, score, and strategy inputs that generated the event.

---

## Emergency

```bash
bybit kill --reason "Claude Code manual review"
```

This cancels all open orders, flattens all positions, and halts the loop. Idempotent.

To restart after investigating:
```bash
# Verify the root cause is resolved, then:
bybit resume   # if only paused
# If kill was engaged, clear via SQL (see AGENT.md Emergency Actions section)
bybit run
```

---

## Tests

```bash
pytest -x                     # stop on first failure
pytest tests/test_signing.py  # signing parity only
pytest -k "kill"              # filter by name
```

The test suite has 104 tests. All must pass before any commit that touches `bybit_agent/`.

---

## What NOT to Do

- Do not add `ANTHROPIC_API_KEY` to `.env` or any config. This repo has no in-process AI.
- Do not call external LLM APIs from any module. You (the installing agent) are the AI.
- Do not place orders by constructing `BybitClient.place_order()` calls in ad-hoc scripts.
- Do not modify `agent_state` directly in DB except for the kill-clear SQL in AGENT.md.
- Do not change `decision_engine.py`'s `paper` flag without the user's explicit instruction.
- Do not push to `main` or `master` without an explicit pull request request from the user.
- Do not install additional AI/ML packages — the indicators are hand-ported for parity.
