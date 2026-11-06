"""SQLAlchemy 2.0 ORM models for signals, orders, fills, positions, double-entry ledger,
users/settings, config_versions, trade_journal, and lessons (with pgvector support on Postgres
and JSON float-array fallback on SQLite so unit/chaos tests run identically)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class ConfigVersion(Base):
    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    risk_hash: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    strategy_version: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    approved_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_prefs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeSetting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SignalRecord(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    signal_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    broker_symbol: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    stop: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    target: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    timeframe: Mapped[str] = mapped_column(String(16))
    signal_time: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True)  # accepted | rejected | pending_approval | executed
    reason: Mapped[str] = mapped_column(String(256))
    risk_decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    advisory: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_version_id: Mapped[int | None] = mapped_column(ForeignKey("config_versions.id"), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    signal_id: Mapped[str] = mapped_column(String(128), ForeignKey("signals.signal_id"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))  # buy | sell | close_long | close_short
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    requested_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    stop: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    target: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)  # pending_approval | submitted | filled | cancelled | rejected
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FillRecord(Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), index=True)
    broker_fill_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    slippage: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    fee: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PositionRecord(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64), default="XAU_EMA_RSI_v0.1")
    direction: Mapped[str] = mapped_column(String(8))  # long | short
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    stop: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    target: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    initial_risk_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    entry_signal_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(16), index=True, default="open")  # open | closed
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    fees_paid: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    high_watermark: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low_watermark: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LedgerEntry(Base):
    """Double-entry style cash-flow ledger: one row per money movement."""

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    type: Mapped[str] = mapped_column(
        String(32), index=True
    )  # deposit | withdrawal | realized_pnl | fee | funding | interest | dividend | transfer | adjustment
    direction: Mapped[str] = mapped_column(String(8))  # in | out
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))  # always non-negative; signed via direction
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_txn_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)


class TradeJournal(Base):
    """Phase 4.5 trade journal record (one per closed trade, mirrors trader-memory-core)."""

    __tablename__ = "trade_journal"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    position_id: Mapped[str] = mapped_column(String(36), ForeignKey("positions.id"), unique=True, index=True)
    signal_id: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))  # long | short
    regime_bucket: Mapped[str] = mapped_column(String(32), index=True)  # bull | bear | mixed
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    stop_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    r_multiple: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    mae_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    mfe_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    slippage_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    fees_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    holding_seconds: Mapped[int] = mapped_column(Integer, default=0)
    exit_reason: Mapped[str] = mapped_column(String(64))
    signal_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    indicators_at_entry: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    regime_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_gate_decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    nearby_events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    lesson: Mapped[str] = mapped_column(Text, default="")
    manual_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class LessonRecord(Base):
    """Embedded lesson record for top-k similarity recall before each new signal.
    On Postgres with pgvector enabled, a native `vector(768)` column + index is also maintained
    via migration/DDL helper; `embedding_json` keeps portable float array across Postgres & SQLite."""

    __tablename__ = "lessons"
    __table_args__ = (
        Index("ix_lessons_lookup", "strategy", "direction", "regime_bucket"),
        UniqueConstraint("journal_id", name="uq_lessons_journal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    journal_id: Mapped[str] = mapped_column(String(36), ForeignKey("trade_journal.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    regime_bucket: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8), index=True)
    outcome: Mapped[str] = mapped_column(String(16))  # win | loss | scratch
    r_multiple: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    setup_text: Mapped[str] = mapped_column(Text)
    lesson_text: Mapped[str] = mapped_column(Text)
    embedding_json: Mapped[list[float]] = mapped_column(JSON, default=list)
    model_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReconcileReport(Base):
    __tablename__ = "reconcile_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    matched: Mapped[bool] = mapped_column(Boolean)
    db_balance: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    broker_balance: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    diff_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    discrepancies: Mapped[list[str]] = mapped_column(JSON, default=list)


class RuleChangeProposal(Base):
    """Phase 4.5 change-control record: requires >= 30 trades of evidence and explicit user approval."""

    __tablename__ = "rule_change_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    target_mistake_tag: Mapped[str] = mapped_column(String(64))
    evidence_trade_count: Mapped[int] = mapped_column(Integer)
    proposed_diff: Mapped[str] = mapped_column(Text)
    backtest_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending_review")  # pending_review | approved | rejected
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
