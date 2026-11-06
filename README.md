# XAUUSD TradingView Signals → Automated Execution → Cash-Flow Dashboard

End-to-end automated trading execution engine, double-entry cash-flow ledger, HTMX/Chart.js web dashboard, and post-trade learning memory layer built around the `.claude/skills/` (`claude-trading-skills`) analytical toolkit.

## Architecture
```
TradingView alert (Pine v6) ──HTTPS JSON──▶ POST /webhook
  ──▶ append-only raw JSONL log
  ──▶ constant-time secret + freshness (<=60s) + rate-limit + idempotency (signal_id)
  ──▶ SQLite outbox fallback (if Postgres down: queues signals, blocks entries, still executes exits!)
  ──▶ pre-trade recall advisory (pgvector / cosine similarity over past trade lessons)
  ──▶ risk gate (kill switch, daily loss breaker, max positions, exposure ceiling, session, news blackout, revenge cooldown)
  ──▶ position sizer (qty = floor((equity * risk_pct) / |entry - stop|), never rounds up)
  ──▶ broker adapter (PaperBroker / OANDA v20 REST with attached stop-loss bracket)
  ──▶ double-entry cash-flow ledger + postmortem journal + HTMX dashboard
```

## Quick Start (Local Python — No Docker Required)

```powershell
# 1. Copy environment config (starts in safe `paper` mode)
Copy-Item .env.example .env

# 2. Start the FastAPI server + dashboard on http://127.0.0.1:8000
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. In a second terminal, send a simulated TradingView buy alert
.venv\Scripts\python tools\alert_sim.py --action buy --price 4060.0 --stop 4048.0 --target 4084.0 --secret replace-with-strong-32-byte-secret

# 4. Open http://127.0.0.1:8000 (default login: admin / changeme123!)
```

## One-Command Start with Docker Compose (PostgreSQL 16 + pgvector + API + Worker)

```powershell
Copy-Item .env.example .env
docker compose up --build
```

## CLI Safety Controls (`tradectl`)
Works even if PostgreSQL, the LLM/embedding service, or the web dashboard is down:
```powershell
# Emergency kill switch: cancels all open orders, flattens positions, and blocks new entries
.venv\Scripts\python -m cli.tradectl kill --reason "emergency"

# Check broker balances, open positions, kill switch, and circuit breaker status
.venv\Scripts\python -m cli.tradectl status

# Reset kill switch or daily-loss circuit breaker
.venv\Scripts\python -m cli.tradectl reset-kill
.venv\Scripts\python -m cli.tradectl reset-breaker

# Replay SQLite outbox and reconcile DB ledger vs broker balance
.venv\Scripts\python -m cli.tradectl replay-outbox
.venv\Scripts\python -m cli.tradectl reconcile
```

## Running the Test Suite (Phase 5 QA)
```powershell
.venv\Scripts\python -m pytest -v
.venv\Scripts\python -m ruff check app cli tools tests
.venv\Scripts\python -m mypy app cli tools --ignore-missing-imports
```

## Key Documents
- [`docs/strategy-spec.md`](docs/strategy-spec.md) — Phase 1 regime analysis, exact strategy rules, sizing feasibility, and prototype backtest results.
- [`tradingview/strategy.pine`](tradingview/strategy.pine) — Phase 2 Pine Script v6 non-repainting strategy & webhook alert generator.
- [`docs/tradingview-setup.md`](docs/tradingview-setup.md) — Step-by-step TradingView chart, secret input, Cloudflared tunnel, and webhook setup.
- [`docs/strategy-tester-comparison.md`](docs/strategy-tester-comparison.md) — Comparison guide between TradingView Strategy Tester and the Phase 1 backtest.
- [`docs/go-live-checklist.md`](docs/go-live-checklist.md) — Pre-live verification checklist and two-step manual confirmation rules.
