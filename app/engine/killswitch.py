"""Kill switch controller (Phase 3).

Works even when PostgreSQL, the LLM, or the dashboard is down:
1. Writes the atomic file-backed kill switch flag (`data/state/KILL_SWITCH.json`).
2. Calls the broker adapter directly to cancel all open orders and flatten all positions.
3. Opportunistically syncs the DB if Postgres is reachable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.brokers.base import BrokerAdapter
from app.brokers.oanda import get_broker
from app.core.config import Settings, get_settings
from app.core.state import KillSwitch
from app.ledger.service import record_entry
from app.notify.telegram import get_notifier
from app.storage.db import db_healthy, session_scope
from app.storage.models import FillRecord, OrderRecord, PositionRecord


def trigger_kill_switch(
    reason: str = "manual_kill_switch",
    by: str = "operator",
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    ks = KillSwitch(s.state_dir)
    ks.engage(reason=reason, by=by)

    brk = broker or get_broker(s)
    cancelled = 0
    flattened = []
    broker_err = None
    try:
        cancelled = brk.cancel_all_orders()
        flattened = brk.flatten_all_positions()
    except Exception as e:
        broker_err = str(e)

    # Best-effort DB sync if Postgres is up
    if db_healthy(s):
        try:
            with session_scope(s) as db:
                for p in db.query(PositionRecord).filter_by(state="open").all():
                    match = next((r for r in flattened if r.symbol == p.symbol), None)
                    p.state = "closed"
                    p.closed_at = datetime.now(timezone.utc)
                    p.exit_reason = "kill_switch"
                    if match:
                        p.exit_price = match.fill_price
                        p.realized_pnl = match.realized_pnl
                        p.fees_paid = Decimal(str(p.fees_paid)) + match.fee
                        if match.realized_pnl != 0:
                            record_entry(
                                db,
                                entry_type="realized_pnl",
                                direction="in" if match.realized_pnl > 0 else "out",
                                amount=abs(match.realized_pnl),
                                ref=match.client_order_id,
                                note="Kill switch flatten P&L",
                                broker_txn_id=f"{match.broker_fill_id}-PNL",
                            )
                        if match.fee > 0:
                            record_entry(
                                db,
                                entry_type="fee",
                                direction="out",
                                amount=match.fee,
                                ref=match.client_order_id,
                                note="Kill switch flatten fee",
                                broker_txn_id=f"{match.broker_fill_id}-FEE",
                            )
                for o in db.query(OrderRecord).filter(OrderRecord.status.in_(["pending_approval", "submitted"])).all():
                    o.status = "cancelled"
                    o.reason = "kill_switch"
        except Exception:
            pass

    get_notifier(s).send("kill_switch", f"Kill switch engaged by {by}: {reason} (flattened={len(flattened)})")
    return {
        "active": True,
        "reason": reason,
        "by": by,
        "cancelled_orders": cancelled,
        "flattened_positions": len(flattened),
        "broker_error": broker_err,
    }


def reset_kill_switch(by: str = "operator", settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    ks = KillSwitch(s.state_dir)
    ks.reset(by=by)
    get_notifier(s).send("kill_switch_reset", f"Kill switch reset by {by}")
    return {"active": False, "reset_by": by}
