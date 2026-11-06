"""CLI control plane (`python -m cli.tradectl <command>`).

Works even if the web dashboard, LLM, or PostgreSQL database is down:
  - `kill`:          engages kill switch, cancels open broker orders, flattens positions directly via broker adapter
  - `reset-kill`:    resets kill switch
  - `reset-breaker`: resets daily-loss circuit breaker
  - `status`:        prints kill switch, circuit breaker, broker balances, and open positions
  - `replay-outbox`: replays queued SQLite outbox signals & embeddings into Postgres
  - `reconcile`:     runs ledger-vs-broker reconciliation
  - `set-live`:      requires interactive typed confirmation `I UNDERSTAND THIS WILL TRADE REAL MONEY`
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from app.brokers.oanda import get_broker
from app.core.config import LIVE_CONFIRM_PHRASE, get_settings
from app.core.state import CircuitBreaker, KillSwitch
from app.engine.killswitch import reset_kill_switch, trigger_kill_switch
from app.engine.reconciler import reconcile_state, replay_outbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradectl", description="XAUUSD Engine Safety & Control CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_kill = sub.add_parser("kill", help="Cancel all orders, flatten positions, and block new entries")
    p_kill.add_argument("--reason", default="cli_kill_switch")
    p_kill.add_argument("--by", default="cli_operator")

    p_rk = sub.add_parser("reset-kill", help="Reset the kill switch after inspection")
    p_rk.add_argument("--by", default="cli_operator")

    sub.add_parser("reset-breaker", help="Reset the daily drawdown circuit breaker")
    sub.add_parser("status", help="Show broker & safety state (works without Postgres)")
    sub.add_parser("replay-outbox", help="Replay queued SQLite outbox signals & embeddings")
    sub.add_parser("reconcile", help="Run DB vs Broker balance/position reconciliation")
    sub.add_parser("set-live", help="Interactive typed confirmation required before live mode can start")

    args = parser.parse_args(argv)
    s = get_settings()

    if args.cmd == "kill":
        res = trigger_kill_switch(reason=args.reason, by=args.by, settings=s)
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "reset-kill":
        res = reset_kill_switch(by=args.by, settings=s)
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "reset-breaker":
        cb = CircuitBreaker(s.state_dir, s.max_daily_loss_pct)
        cb.reset()
        print(json.dumps({"breaker_reset": True, "status": cb.status()}, indent=2))
        return 0

    if args.cmd == "status":
        ks = KillSwitch(s.state_dir)
        cb = CircuitBreaker(s.state_dir, s.max_daily_loss_pct)
        brk = get_broker(s)
        bal = brk.balances()
        pos = brk.positions()
        out = {
            "mode": s.execution_mode,
            "broker": s.broker,
            "kill_switch_active": ks.active(),
            "kill_switch_info": ks.info(),
            "circuit_breaker": cb.status(),
            "cash_balance": str(bal.cash_balance),
            "equity": str(bal.equity),
            "open_positions": [p.__dict__ for p in pos],
        }
        print(json.dumps(out, indent=2, default=str))
        return 0

    if args.cmd == "replay-outbox":
        print(json.dumps(replay_outbox(s), indent=2))
        return 0

    if args.cmd == "reconcile":
        res = reconcile_state(s)
        print(json.dumps(res, indent=2))
        return 0 if res.get("matched") else 2

    if args.cmd == "set-live":
        print(f"WARNING: You are authorizing live trading for risk hash {s.risk_hash()}.")
        print("Verify that your broker API key is TRADE-ONLY with WITHDRAWALS DISABLED.")
        typed = input(f'Type "{LIVE_CONFIRM_PHRASE}" to confirm: ').strip()
        if typed != LIVE_CONFIRM_PHRASE:
            print("Confirmation phrase did not match. Live mode remains BLOCKED.", file=sys.stderr)
            return 1
        s.live_confirm_path.parent.mkdir(parents=True, exist_ok=True)
        s.live_confirm_path.write_text(
            json.dumps(
                {
                    "confirmed_phrase": typed,
                    "risk_hash": s.risk_hash(),
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )
        print(f"Live confirmation written to {s.live_confirm_path}.")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
