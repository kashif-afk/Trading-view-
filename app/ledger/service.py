"""Double-entry style cash-flow ledger service (Phase 4).

Every movement of money in or out is recorded as a row in `ledger_entries`:
  - deposit        (in)
  - withdrawal     (out)
  - realized_pnl   (in | out)
  - fee            (out)
  - funding        (in | out)
  - interest       (in)
  - dividend       (in)
  - transfer       (in | out)
  - adjustment     (in | out, note required)

Invariant verified by reconciler and unit tests:
    sum(in) - sum(out) == broker cash_balance (±1e-4 rounding tolerance)
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.brokers.base import BrokerAdapter
from app.brokers.paper import PaperBroker
from app.storage.models import LedgerEntry

VALID_LEDGER_TYPES = {
    "deposit",
    "withdrawal",
    "realized_pnl",
    "fee",
    "funding",
    "interest",
    "dividend",
    "transfer",
    "adjustment",
}


def ensure_initial_deposit(db: Session, initial_amount: Decimal, currency: str = "USD") -> None:
    existing = db.query(LedgerEntry).filter_by(broker_txn_id="INIT-DEPOSIT").one_or_none()
    if existing is None:
        db.add(
            LedgerEntry(
                type="deposit",
                direction="in",
                amount=Decimal(str(initial_amount)),
                currency=currency,
                ref="initial_capital",
                note="Initial paper account funding",
                broker_txn_id="INIT-DEPOSIT",
            )
        )
        db.flush()


def record_entry(
    db: Session,
    *,
    entry_type: str,
    direction: str,
    amount: Decimal,
    currency: str = "USD",
    ref: str | None = None,
    note: str | None = None,
    broker_txn_id: str | None = None,
    ts: datetime | None = None,
    broker: BrokerAdapter | None = None,
    sync_broker_cash: bool = False,
) -> LedgerEntry:
    if entry_type not in VALID_LEDGER_TYPES:
        raise ValueError(f"Invalid ledger type: {entry_type}")
    if direction not in ("in", "out"):
        raise ValueError(f"Invalid direction: {direction}")
    if entry_type == "adjustment" and not (note and note.strip()):
        raise ValueError("Manual adjustments require an explanatory note.")
    amt = abs(Decimal(str(amount))).quantize(Decimal("0.000001"))

    if broker_txn_id:
        dup = db.query(LedgerEntry).filter_by(broker_txn_id=broker_txn_id).one_or_none()
        if dup:
            return dup

    row = LedgerEntry(
        ts=ts or datetime.now(timezone.utc),
        type=entry_type,
        direction=direction,
        amount=amt,
        currency=currency,
        ref=ref,
        note=note,
        broker_txn_id=broker_txn_id,
    )
    db.add(row)
    db.flush()

    if sync_broker_cash and isinstance(broker, PaperBroker):
        signed = amt if direction == "in" else -amt
        broker.adjust_cash(signed)

    return row


def compute_waterfall(
    db: Session,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> dict[str, Decimal]:
    rows = db.query(LedgerEntry).order_by(LedgerEntry.ts.asc()).all()

    def _signed(r: LedgerEntry) -> Decimal:
        return Decimal(str(r.amount)) if r.direction == "in" else -Decimal(str(r.amount))

    def _norm(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    starting_balance = Decimal("0")
    buckets: dict[str, Decimal] = {
        "deposits": Decimal("0"),
        "withdrawals": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "funding": Decimal("0"),
        "interest_dividend": Decimal("0"),
        "transfers": Decimal("0"),
        "adjustments": Decimal("0"),
    }

    for r in rows:
        rts = _norm(r.ts)
        if start_ts and rts < _norm(start_ts):
            starting_balance += _signed(r)
            continue
        if end_ts and rts > _norm(end_ts):
            continue
        s = _signed(r)
        if r.type == "deposit":
            buckets["deposits"] += s
        elif r.type == "withdrawal":
            buckets["withdrawals"] += s
        elif r.type == "realized_pnl":
            buckets["realized_pnl"] += s
        elif r.type == "fee":
            buckets["fees"] += s
        elif r.type == "funding":
            buckets["funding"] += s
        elif r.type in ("interest", "dividend"):
            buckets["interest_dividend"] += s
        elif r.type == "transfer":
            buckets["transfers"] += s
        elif r.type == "adjustment":
            buckets["adjustments"] += s

    ending = starting_balance + sum(buckets.values(), Decimal("0"))
    return {
        "starting_balance": starting_balance.quantize(Decimal("0.01")),
        **{k: v.quantize(Decimal("0.01")) for k, v in buckets.items()},
        "ending_balance": ending.quantize(Decimal("0.01")),
        "ending_balance_exact": ending.quantize(Decimal("0.000001")),
    }


def monthly_in_out(db: Session) -> list[dict[str, Any]]:
    rows = db.query(LedgerEntry).order_by(LedgerEntry.ts.asc()).all()
    by_m: dict[str, dict[str, float]] = {}
    for r in rows:
        m = r.ts.strftime("%Y-%m")
        by_m.setdefault(m, {"month": m, "in": 0.0, "out": 0.0})
        by_m[m][r.direction] = round(by_m[m][r.direction] + float(r.amount), 2)
    return list(by_m.values())


def export_ledger_csv(rows: list[LedgerEntry]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "timestamp", "type", "direction", "amount", "currency", "ref", "note", "broker_txn_id"])
    for r in rows:
        writer.writerow([r.id, r.ts.isoformat(), r.type, r.direction, f"{r.amount:.6f}", r.currency, r.ref or "", r.note or "", r.broker_txn_id or ""])
    return buf.getvalue()
