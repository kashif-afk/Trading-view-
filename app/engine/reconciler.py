"""Reconciler & Outbox replay job (Phase 3 & Phase 5).

Runs every 30–60 seconds and on startup:
1. If Postgres is healthy and the SQLite outbox has pending signals, replays them in strict FIFO order
   with zero loss and zero duplicates.
2. Backfills any queued lesson embeddings once the embedding provider recovers.
3. Reconciles sum(ledger) vs broker `cash_balance` and open DB positions vs broker positions; logs a
   `ReconcileReport` row and sends a Telegram alert on any mismatch.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.brokers.base import BrokerAdapter
from app.brokers.oanda import get_broker
from app.core.config import Settings, get_settings
from app.engine.pipeline import WebhookPayload, process_webhook_signal
from app.ledger.service import compute_waterfall, ensure_initial_deposit
from app.memory.embeddings import EmbeddingService
from app.notify.telegram import get_notifier
from app.storage.db import db_healthy, session_scope
from app.storage.models import PositionRecord, ReconcileReport, SignalRecord
from app.storage.outbox import OutboxStore


def replay_outbox(
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
    embedder: EmbeddingService | None = None,
) -> dict[str, int]:
    s = settings or get_settings()
    if not db_healthy(s):
        return {"replayed_signals": 0, "backfilled_embeddings": 0}

    outbox = OutboxStore(s.outbox_path)
    brk = broker or get_broker(s)
    emb = embedder or EmbeddingService(s, outbox)

    replayed = 0
    for item in outbox.pending_signals():
        raw = dict(item["payload"])
        raw["secret"] = s.webhook_secret.get_secret_value() or "REPLAY"
        wp = WebhookPayload(**raw)
        with session_scope(s) as db:
            already = db.query(SignalRecord).filter_by(signal_id=wp.signal_id).one_or_none()
        if already is None:
            process_webhook_signal(wp, settings=s, broker=brk, embedder=emb)
        outbox.mark_signal_replayed(item["id"])
        replayed += 1

    with session_scope(s) as db:
        backfilled = emb.backfill_pending(db)

    return {"replayed_signals": replayed, "backfilled_embeddings": backfilled}


def reconcile_state(
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
    tolerance: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    s = settings or get_settings()
    brk = broker or get_broker(s)
    if not db_healthy(s):
        return {"matched": False, "error": "db_unreachable"}

    replay_outbox(s, brk)

    bal = brk.balances()
    b_pos = {p.symbol: p for p in brk.positions()}

    discrepancies: list[str] = []
    with session_scope(s) as db:
        ensure_initial_deposit(db, s.account_size, s.base_currency)
        wf = compute_waterfall(db)
        db_cash = wf["ending_balance_exact"]
        diff = abs(db_cash - bal.cash_balance)
        if diff > tolerance:
            discrepancies.append(f"cash mismatch: ledger={db_cash:.4f} vs broker={bal.cash_balance:.4f}")

        db_open = {p.symbol: p for p in db.query(PositionRecord).filter_by(state="open").all()}
        for sym, dp in db_open.items():
            bp = b_pos.get(sym)
            if not bp:
                discrepancies.append(f"position {sym} open in DB but flat at broker")
            elif abs(Decimal(str(dp.qty)) - bp.qty) > Decimal("0.0001"):
                discrepancies.append(f"position {sym} qty mismatch: DB={dp.qty} vs broker={bp.qty}")
        for sym in b_pos:
            if sym not in db_open:
                discrepancies.append(f"position {sym} open at broker ({b_pos[sym].qty}) but missing in DB")

        matched = len(discrepancies) == 0
        rep = ReconcileReport(
            matched=matched,
            db_balance=db_cash,
            broker_balance=bal.cash_balance,
            diff_usd=diff,
            discrepancies=discrepancies,
        )
        db.add(rep)
        db.flush()

    if not matched:
        get_notifier(s).send("reconcile_mismatch", "; ".join(discrepancies))

    return {
        "matched": matched,
        "db_balance": str(db_cash.quantize(Decimal("0.01"))),
        "broker_balance": str(bal.cash_balance.quantize(Decimal("0.01"))),
        "diff_usd": str(diff.quantize(Decimal("0.0001"))),
        "discrepancies": discrepancies,
    }
