"""Webhook execution pipeline (Phase 3 + Phase 4.5 recall/journal integration).

Flow:
  TradingView JSON -> verify secret -> freshness check -> dedupe on `signal_id`
  -> if DB down: queue in SQLite outbox; if signal is an EXIT, still execute on broker immediately!
  -> pre-trade recall (`recall_similar_setups`)
  -> risk gate (`evaluate_risk_gate`)
  -> sizer (`calculate_position_size`, with optional caution-rule 50% size cut)
  -> approval gate (if `execution_mode == 'approval'` or caution_action == 'approval')
  -> broker adapter (`place_order`) -> fills, positions, double-entry ledger, and postmortem journal.
"""
from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.brokers.base import BrokerAdapter, BrokerError
from app.brokers.oanda import get_broker
from app.core.config import Settings, get_settings
from app.core.security import parse_ts
from app.core.state import CircuitBreaker
from app.engine.risk_gate import evaluate_risk_gate
from app.engine.sizer import calculate_position_size
from app.ledger.service import ensure_initial_deposit, record_entry
from app.memory.embeddings import EmbeddingService
from app.memory.journal import record_closed_trade_journal
from app.memory.recall import recall_similar_setups
from app.notify.telegram import get_notifier
from app.storage.db import db_healthy, ensure_config_version, session_scope
from app.storage.models import FillRecord, OrderRecord, PositionRecord, SignalRecord
from app.storage.outbox import OutboxStore


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=1)
    signal_id: str = Field(min_length=3, max_length=128)
    strategy: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    exchange: str = Field(default="OANDA", max_length=32)
    action: Literal["buy", "sell", "close_long", "close_short"]
    price: Decimal = Field(gt=Decimal(0))
    stop: Decimal | None = None
    target: Decimal | None = None
    timeframe: str = Field(default="15m", max_length=16)
    time: str = Field(min_length=4, max_length=64)
    reason: str = Field(default="entry", max_length=64)
    indicators: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_stops(self) -> WebhookPayload:
        if self.action in ("buy", "sell") and self.stop is None:
            raise ValueError("Entry signals (buy/sell) must include a non-null stop price.")
        return self


_PIPELINE_LOCK = threading.RLock()


def _redacted_payload(p: WebhookPayload, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    d = p.model_dump(mode="json")
    d["secret"] = "***"
    if extra:
        d.update(extra)
    return d


def process_webhook_signal(
    payload: WebhookPayload,
    *,
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
    embedder: EmbeddingService | None = None,
    force_db_down: bool = False,
    upcoming_news_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with _PIPELINE_LOCK:
        return _process_webhook_signal_locked(
            payload,
            settings=settings,
            broker=broker,
            embedder=embedder,
            force_db_down=force_db_down,
            upcoming_news_events=upcoming_news_events,
        )


def _process_webhook_signal_locked(
    payload: WebhookPayload,
    *,
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
    embedder: EmbeddingService | None = None,
    force_db_down: bool = False,
    upcoming_news_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    brk = broker or get_broker(s)
    outbox = OutboxStore(s.outbox_path)
    notifier = get_notifier(s)
    broker_sym = s.broker_symbol(payload.symbol)
    is_exit = payload.action in ("close_long", "close_short")

    # --- Fallback path when PostgreSQL is unreachable ---
    if force_db_down or not db_healthy(s):
        mode_tag = "exit_when_db_down" if is_exit else "entry_blocked_db_down"
        inserted = outbox.enqueue_signal(payload.signal_id, _redacted_payload(payload, {"_outbox_mode": mode_tag}))
        if not inserted:
            return {"status": "duplicate", "signal_id": payload.signal_id, "reason": "duplicate_in_outbox"}
        if is_exit:
            try:
                res = brk.place_order(
                    client_order_id=f"ord-{payload.signal_id}",
                    symbol=broker_sym,
                    side=payload.action,
                    qty=Decimal(0),
                    price_hint=payload.price,
                    stop=None,
                    target=None,
                )
                return {
                    "status": "executed_via_outbox",
                    "signal_id": payload.signal_id,
                    "reason": "db_down_exit_executed_and_queued",
                    "broker_order_id": res.broker_order_id,
                }
            except BrokerError as e:
                notifier.send("error", f"DB+Broker error on exit {payload.signal_id}: {e}")
                return {"status": "queued_exit", "signal_id": payload.signal_id, "reason": f"db_and_broker_down: {e}"}
        return {
            "status": "rejected_db_down",
            "signal_id": payload.signal_id,
            "reason": "rejected: primary DB unreachable (queued to outbox; new entries blocked until replay)",
        }

    # --- Normal path with healthy DB ---
    cv_id = ensure_config_version(s)
    with session_scope(s) as db:
        ensure_initial_deposit(db, s.account_size, s.base_currency)

        existing = db.query(SignalRecord).filter_by(signal_id=payload.signal_id).one_or_none()
        if existing is not None:
            return {
                "status": "duplicate",
                "signal_id": payload.signal_id,
                "reason": f"duplicate of {existing.status} signal",
            }

        # Pre-trade advisory recall (Phase 4.5)
        advisory = recall_similar_setups(
            db,
            symbol=broker_sym,
            timeframe=payload.timeframe,
            strategy=payload.strategy,
            action=payload.action,
            indicators=payload.indicators,
            settings=s,
            embedder=embedder,
        )

        # Query broker state (or handle broker outage safely)
        try:
            bal = brk.balances()
            b_positions = brk.positions()
            equity = bal.equity
            open_count = len(b_positions)
            open_notional = sum((p.qty * p.avg_price for p in b_positions), Decimal(0))
        except BrokerError as e:
            notifier.send("broker_error", f"Broker unreachable on {payload.signal_id}: {e}")
            if is_exit:
                outbox.enqueue_signal(payload.signal_id, _redacted_payload(payload))
                sig = SignalRecord(
                    signal_id=payload.signal_id,
                    strategy=payload.strategy,
                    symbol=payload.symbol,
                    broker_symbol=broker_sym,
                    action=payload.action,
                    price=payload.price,
                    stop=payload.stop,
                    target=payload.target,
                    timeframe=payload.timeframe,
                    signal_time=payload.time,
                    payload=_redacted_payload(payload),
                    status="queued_exit",
                    reason=f"queued: broker unreachable ({e})",
                    advisory=advisory,
                    config_version_id=cv_id,
                )
                db.add(sig)
                return {"status": "queued_exit", "signal_id": payload.signal_id, "reason": sig.reason}
            sig = SignalRecord(
                signal_id=payload.signal_id,
                strategy=payload.strategy,
                symbol=payload.symbol,
                broker_symbol=broker_sym,
                action=payload.action,
                price=payload.price,
                stop=payload.stop,
                target=payload.target,
                timeframe=payload.timeframe,
                signal_time=payload.time,
                payload=_redacted_payload(payload),
                status="rejected",
                reason=f"rejected: broker unreachable ({e})",
                advisory=advisory,
                config_version_id=cv_id,
            )
            db.add(sig)
            return {"status": "rejected", "signal_id": payload.signal_id, "reason": sig.reason}

        sig_dt = parse_ts(payload.time)
        gate = evaluate_risk_gate(
            action=payload.action,
            symbol=broker_sym,
            entry_price=payload.price,
            stop_price=payload.stop,
            signal_time=sig_dt,
            equity=equity,
            open_positions_count=open_count,
            current_open_notional=open_notional,
            db=db,
            settings=s,
            upcoming_news_events=upcoming_news_events,
        )

        if not gate.allowed:
            if gate.details.get("circuit_breaker"):
                notifier.send("circuit_breaker", f"Circuit breaker tripped: {gate.reason}")
            sig = SignalRecord(
                signal_id=payload.signal_id,
                strategy=payload.strategy,
                symbol=payload.symbol,
                broker_symbol=broker_sym,
                action=payload.action,
                price=payload.price,
                stop=payload.stop,
                target=payload.target,
                timeframe=payload.timeframe,
                signal_time=payload.time,
                payload=_redacted_payload(payload),
                status="rejected",
                reason=gate.reason,
                risk_decision=gate.details,
                advisory=advisory,
                config_version_id=cv_id,
            )
            db.add(sig)
            return {"status": "rejected", "signal_id": payload.signal_id, "reason": gate.reason}

        # Sizing (for entries) or position lookup (for exits)
        size_mult = Decimal("0.5") if (advisory.get("caution_triggered") and advisory.get("caution_action") == "halve") else Decimal("1.0")
        if is_exit:
            open_pos = (
                db.query(PositionRecord)
                .filter_by(symbol=broker_sym, state="open")
                .order_by(PositionRecord.opened_at.asc())
                .first()
            )
            qty = Decimal(str(open_pos.qty)) if open_pos else Decimal(0)
            risk_usd = Decimal(0)
        else:
            max_add_notional = max(Decimal(0), equity * (s.max_exposure_pct / Decimal(100)) - open_notional)
            sz = calculate_position_size(
                equity=equity,
                risk_pct=s.risk_per_trade_pct,
                entry_price=payload.price,
                stop_price=payload.stop,
                min_qty=s.paper_min_qty,
                qty_step=s.paper_qty_step,
                max_additional_notional=max_add_notional,
                size_multiplier=size_mult,
            )
            if not sz.allowed:
                sig = SignalRecord(
                    signal_id=payload.signal_id,
                    strategy=payload.strategy,
                    symbol=payload.symbol,
                    broker_symbol=broker_sym,
                    action=payload.action,
                    price=payload.price,
                    stop=payload.stop,
                    target=payload.target,
                    timeframe=payload.timeframe,
                    signal_time=payload.time,
                    payload=_redacted_payload(payload),
                    status="rejected",
                    reason=sz.reason,
                    risk_decision=gate.details,
                    advisory=advisory,
                    config_version_id=cv_id,
                )
                db.add(sig)
                return {"status": "rejected", "signal_id": payload.signal_id, "reason": sz.reason}
            qty = sz.qty
            risk_usd = sz.risk_usd

        needs_approval = (not is_exit) and (
            s.execution_mode == "approval"
            or (advisory.get("caution_triggered") and advisory.get("caution_action") == "approval")
        )

        sig = SignalRecord(
            signal_id=payload.signal_id,
            strategy=payload.strategy,
            symbol=payload.symbol,
            broker_symbol=broker_sym,
            action=payload.action,
            price=payload.price,
            stop=payload.stop,
            target=payload.target,
            timeframe=payload.timeframe,
            signal_time=payload.time,
            payload=_redacted_payload(payload),
            status="pending_approval" if needs_approval else "accepted",
            reason="pending operator approval" if needs_approval else "accepted",
            risk_decision={**gate.details, "sized_qty": str(qty), "risk_usd": str(risk_usd)},
            advisory=advisory,
            config_version_id=cv_id,
        )
        db.add(sig)
        db.flush()

        ord_rec = OrderRecord(
            signal_id=payload.signal_id,
            client_order_id=f"ord-{payload.signal_id}",
            symbol=broker_sym,
            side=payload.action,
            qty=qty,
            requested_price=payload.price,
            stop=payload.stop,
            target=payload.target,
            status="pending_approval" if needs_approval else "submitted",
            reason=payload.reason,
        )
        db.add(ord_rec)
        db.flush()

        if needs_approval:
            notifier.send("approval_needed", f"Signal {payload.signal_id} ({payload.action} {qty} {broker_sym}) awaiting approval")
            return {
                "status": "pending_approval",
                "signal_id": payload.signal_id,
                "order_id": ord_rec.id,
                "qty": str(qty),
            }

        return _execute_order_in_session(
            db,
            sig=sig,
            ord_rec=ord_rec,
            brk=brk,
            settings=s,
            embedder=embedder,
            risk_usd=risk_usd,
        )


def _execute_order_in_session(
    db: Session,
    *,
    sig: SignalRecord,
    ord_rec: OrderRecord,
    brk: BrokerAdapter,
    settings: Settings,
    embedder: EmbeddingService | None = None,
    risk_usd: Decimal = Decimal(10),
) -> dict[str, Any]:
    notifier = get_notifier(settings)
    try:
        res = brk.place_order(
            client_order_id=ord_rec.client_order_id,
            symbol=ord_rec.symbol,
            side=ord_rec.side,
            qty=Decimal(str(ord_rec.qty)),
            price_hint=Decimal(str(ord_rec.requested_price)),
            stop=Decimal(str(ord_rec.stop)) if ord_rec.stop is not None else None,
            target=Decimal(str(ord_rec.target)) if ord_rec.target is not None else None,
        )
    except BrokerError as e:
        ord_rec.status = "rejected"
        ord_rec.reason = f"broker_error: {e}"
        sig.status = "rejected"
        sig.reason = f"rejected: broker error ({e})"
        notifier.send("broker_error", f"Order failed for {sig.signal_id}: {e}")
        return {"status": "rejected", "signal_id": sig.signal_id, "reason": sig.reason}

    ord_rec.broker_order_id = res.broker_order_id
    ord_rec.fill_price = res.fill_price
    ord_rec.status = res.status
    sig.status = "accepted"

    existing_fill = db.query(FillRecord).filter_by(broker_fill_id=res.broker_fill_id).one_or_none()
    if not existing_fill and res.qty > 0:
        fill = FillRecord(
            order_id=ord_rec.id,
            broker_fill_id=res.broker_fill_id,
            symbol=res.symbol,
            side=res.side,
            qty=res.qty,
            price=res.fill_price,
            slippage=res.slippage,
            fee=res.fee,
            realized_pnl=res.realized_pnl,
            filled_at=res.filled_at,
        )
        db.add(fill)
        db.flush()

        if res.fee > 0:
            record_entry(
                db,
                entry_type="fee",
                direction="out",
                amount=res.fee,
                currency=settings.base_currency,
                ref=ord_rec.client_order_id,
                note=f"Trading fee ({res.side} {res.qty} {res.symbol})",
                broker_txn_id=f"{res.broker_fill_id}-FEE",
            )

        if res.side in ("buy", "sell"):
            pos = PositionRecord(
                symbol=res.symbol,
                strategy=sig.strategy,
                direction="long" if res.side == "buy" else "short",
                qty=res.qty,
                avg_entry_price=res.fill_price,
                stop=Decimal(str(ord_rec.stop)),
                target=Decimal(str(ord_rec.target)) if ord_rec.target is not None else None,
                initial_risk_usd=risk_usd if risk_usd > 0 else Decimal(10),
                entry_signal_id=sig.signal_id,
                state="open",
                fees_paid=res.fee,
                high_watermark=res.fill_price,
                low_watermark=res.fill_price,
            )
            db.add(pos)
            db.flush()
            notifier.send("fill", f"Opened {pos.direction} {pos.qty} {pos.symbol} @ {pos.avg_entry_price} (SL={pos.stop})")
        else:
            open_pos = (
                db.query(PositionRecord)
                .filter_by(symbol=res.symbol, state="open")
                .order_by(PositionRecord.opened_at.asc())
                .first()
            )
            if open_pos:
                open_pos.state = "closed"
                open_pos.exit_price = res.fill_price
                open_pos.exit_reason = ord_rec.reason or "exit"
                open_pos.realized_pnl = res.realized_pnl
                open_pos.fees_paid = Decimal(str(open_pos.fees_paid)) + res.fee
                open_pos.closed_at = res.filled_at
                if res.realized_pnl != 0:
                    record_entry(
                        db,
                        entry_type="realized_pnl",
                        direction="in" if res.realized_pnl > 0 else "out",
                        amount=abs(res.realized_pnl),
                        currency=settings.base_currency,
                        ref=ord_rec.client_order_id,
                        note=f"Closed {open_pos.direction} {open_pos.symbol} ({open_pos.exit_reason})",
                        broker_txn_id=f"{res.broker_fill_id}-PNL",
                    )
                entry_sig = db.query(SignalRecord).filter_by(signal_id=open_pos.entry_signal_id).one_or_none()
                record_closed_trade_journal(
                    db,
                    pos=open_pos,
                    signal=entry_sig or sig,
                    slippage_usd=res.slippage,
                    settings=settings,
                    embedder=embedder,
                )
                # Update circuit breaker after realized P&L
                try:
                    bal = brk.balances()
                    CircuitBreaker(settings.state_dir, settings.max_daily_loss_pct).update(bal.equity)
                except Exception:
                    pass
                notifier.send(
                    "stop_hit" if open_pos.exit_reason == "stop" else "fill",
                    f"Closed {open_pos.direction} {open_pos.qty} {open_pos.symbol} @ {open_pos.exit_price} PnL=${open_pos.realized_pnl:.2f}",
                )

    return {
        "status": "accepted",
        "signal_id": sig.signal_id,
        "order_id": ord_rec.id,
        "broker_order_id": res.broker_order_id,
        "fill_price": str(res.fill_price),
        "qty": str(res.qty),
    }


def approve_pending_order(
    order_id: str,
    *,
    approve: bool,
    operator: str = "operator",
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    brk = broker or get_broker(s)
    with session_scope(s) as db:
        ord_rec = db.query(OrderRecord).filter_by(id=order_id).one_or_none()
        if not ord_rec or ord_rec.status != "pending_approval":
            raise ValueError("Order not found or not pending approval")
        sig = db.query(SignalRecord).filter_by(signal_id=ord_rec.signal_id).one()
        if not approve:
            ord_rec.status = "rejected"
            ord_rec.reason = f"rejected by {operator}"
            sig.status = "rejected"
            sig.reason = f"rejected by {operator} in approval mode"
            return {"status": "rejected", "order_id": order_id}
        risk_usd = Decimal(str((sig.risk_decision or {}).get("risk_usd", "10")))
        return _execute_order_in_session(db, sig=sig, ord_rec=ord_rec, brk=brk, settings=s, risk_usd=risk_usd)
