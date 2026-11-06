"""Trade journal writer + lesson embedder (Phase 4.5, steps 1–3).
Builds on `trader-memory-core` and `signal-postmortem`."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.memory.embeddings import EmbeddingService
from app.memory.postmortem import classify_trade
from app.storage.models import LessonRecord, PositionRecord, SignalRecord, TradeJournal


def regime_bucket_from_indicators(ind: dict[str, Any]) -> str:
    b = int(ind.get("htf_bias", 0))
    return "bull" if b == 1 else ("bear" if b == -1 else "mixed")


def setup_descriptor(
    *,
    symbol: str,
    timeframe: str,
    strategy: str,
    direction: str,
    regime_bucket: str,
    indicators: dict[str, Any],
) -> str:
    rsi = indicators.get("rsi", 50)
    atr = indicators.get("atr", 10)
    return (
        f"strategy={strategy} symbol={symbol} tf={timeframe} dir={direction} "
        f"regime={regime_bucket} rsi={rsi} atr={atr}"
    )


def record_closed_trade_journal(
    db: Session,
    *,
    pos: PositionRecord,
    signal: SignalRecord | None,
    slippage_usd: Decimal = Decimal(0),
    nearby_events: list[dict[str, Any]] | None = None,
    entries_today: int = 1,
    settings: Settings | None = None,
    embedder: EmbeddingService | None = None,
) -> tuple[TradeJournal, LessonRecord]:
    s = settings or get_settings()
    emb_svc = embedder or EmbeddingService(s)

    existing = db.query(TradeJournal).filter_by(position_id=pos.id).one_or_none()
    if existing:
        lesson_row = db.query(LessonRecord).filter_by(journal_id=existing.id).one()
        return existing, lesson_row

    payload = (signal.payload if signal else {}) or {}
    ind = payload.get("indicators") or {}
    regime_bucket = regime_bucket_from_indicators(ind)
    timeframe = signal.timeframe if signal else "15m"
    risk_usd = Decimal(str(pos.initial_risk_usd)) if pos.initial_risk_usd and pos.initial_risk_usd > 0 else Decimal(10)
    net_pnl = Decimal(str(pos.realized_pnl)) - Decimal(str(pos.fees_paid))
    r_mult = (net_pnl / risk_usd).quantize(Decimal("0.0001"))

    opened = pos.opened_at if pos.opened_at.tzinfo else pos.opened_at.replace(tzinfo=UTC)
    closed = (pos.closed_at or datetime.now(UTC))
    if not closed.tzinfo:
        closed = closed.replace(tzinfo=UTC)
    hold_sec = max(0, int((closed - opened).total_seconds()))

    entry_px = Decimal(str(pos.avg_entry_price))
    exit_px = Decimal(str(pos.exit_price or pos.avg_entry_price))
    hi = Decimal(str(pos.high_watermark or max(entry_px, exit_px)))
    lo = Decimal(str(pos.low_watermark or min(entry_px, exit_px)))
    qty = Decimal(str(pos.qty))
    if pos.direction == "long":
        mfe_usd = max(Decimal(0), (hi - entry_px) * qty)
        mae_usd = max(Decimal(0), (entry_px - lo) * qty)
    else:
        mfe_usd = max(Decimal(0), (entry_px - lo) * qty)
        mae_usd = max(Decimal(0), (hi - entry_px) * qty)

    tags, lesson_text = classify_trade(
        direction=pos.direction,
        r_multiple=r_mult,
        exit_reason=pos.exit_reason or "close",
        holding_seconds=hold_sec,
        slippage_usd=slippage_usd,
        initial_risk_usd=risk_usd,
        mae_usd=mae_usd,
        mfe_usd=mfe_usd,
        indicators=ind,
        nearby_events=nearby_events or [],
        entries_today=entries_today,
    )

    journal = TradeJournal(
        position_id=pos.id,
        signal_id=pos.entry_signal_id,
        symbol=pos.symbol,
        strategy=pos.strategy,
        timeframe=timeframe,
        direction=pos.direction,
        regime_bucket=regime_bucket,
        entry_price=entry_px,
        exit_price=exit_px,
        stop_price=Decimal(str(pos.stop)),
        target_price=Decimal(str(pos.target)) if pos.target is not None else None,
        qty=qty,
        realized_pnl=net_pnl,
        r_multiple=r_mult,
        mae_usd=mae_usd,
        mfe_usd=mfe_usd,
        slippage_usd=slippage_usd,
        fees_usd=Decimal(str(pos.fees_paid)),
        holding_seconds=hold_sec,
        exit_reason=pos.exit_reason or "close",
        signal_payload=payload,
        indicators_at_entry=ind,
        regime_scores={"regime_bucket": regime_bucket, "htf_bias": ind.get("htf_bias", 0)},
        risk_gate_decision=signal.risk_decision if signal else {},
        nearby_events=nearby_events or [],
        tags=tags,
        lesson=lesson_text,
        config_version_id=signal.config_version_id if signal else None,
        closed_at=closed,
    )
    db.add(journal)
    db.flush()

    setup_txt = setup_descriptor(
        symbol=pos.symbol,
        timeframe=timeframe,
        strategy=pos.strategy,
        direction=pos.direction,
        regime_bucket=regime_bucket,
        indicators=ind,
    )
    combined_text = f"{setup_txt} | {lesson_text}"
    vec, model_ver = emb_svc.embed(
        combined_text,
        journal_id_for_backfill=journal.id,
        metadata_for_backfill={"symbol": pos.symbol, "strategy": pos.strategy, "allow_hash_backfill": True},
    )
    outcome = "win" if r_mult > Decimal("0.05") else ("loss" if r_mult < Decimal("-0.05") else "scratch")
    lesson = LessonRecord(
        journal_id=journal.id,
        symbol=pos.symbol,
        timeframe=timeframe,
        strategy=pos.strategy,
        regime_bucket=regime_bucket,
        direction=pos.direction,
        outcome=outcome,
        r_multiple=r_mult,
        tags=tags,
        setup_text=setup_txt,
        lesson_text=lesson_text,
        embedding_json=vec,
        model_version=model_ver,
    )
    db.add(lesson)
    db.flush()
    return journal, lesson
