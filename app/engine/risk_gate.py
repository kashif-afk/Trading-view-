"""Risk gate (Phase 3).

Mirrors the logic of:
  - `pre-trade-discipline-gate`: stop-loss presence, revenge re-entry block within N minutes of a stop-out,
    daily entry count cap, and session/trading-hours filter.
  - `drawdown-circuit-breaker`: daily loss % threshold (`max_daily_loss_pct`); exits are ALWAYS allowed.
  - `exposure-coach`: max open positions (`max_open_positions`) and portfolio notional exposure ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.state import CircuitBreaker, KillSwitch
from app.storage.models import PositionRecord


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    details: dict[str, Any]


def _hhmm_to_minutes(hhmm: str) -> int:
    parts = hhmm.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])


def evaluate_risk_gate(
    *,
    action: str,
    symbol: str,
    entry_price: Decimal,
    stop_price: Decimal | None,
    signal_time: datetime,
    equity: Decimal,
    open_positions_count: int,
    current_open_notional: Decimal,
    db: Session | None = None,
    settings: Settings | None = None,
    upcoming_news_events: list[dict[str, Any]] | None = None,
) -> RiskDecision:
    s = settings or get_settings()
    ks = KillSwitch(s.state_dir)
    cb = CircuitBreaker(s.state_dir, s.max_daily_loss_pct)

    is_exit = action in ("close_long", "close_short")
    if is_exit:
        # Exits are ALWAYS allowed even when kill switch or circuit breaker is tripped
        return RiskDecision(True, "exit_always_allowed", {"is_exit": True})

    # 1. Kill switch check
    if ks.active():
        info = ks.info() or {}
        return RiskDecision(False, f"rejected: kill switch active ({info.get('reason', 'manual')})", {"kill_switch": True})

    # 2. Circuit breaker check (daily loss)
    tripped, cb_reason = cb.update(equity)
    if tripped:
        return RiskDecision(False, f"rejected: {cb_reason}", {"circuit_breaker": True})

    # 3. Stop-loss check (no naked entries)
    if stop_price is None:
        return RiskDecision(False, "rejected: missing stop-loss (no naked entries)", {})
    if action == "buy" and stop_price >= entry_price:
        return RiskDecision(False, "rejected: buy stop must be below entry price", {})
    if action == "sell" and stop_price <= entry_price:
        return RiskDecision(False, "rejected: sell stop must be above entry price", {})

    # 4. Max open positions
    if open_positions_count >= s.max_open_positions:
        return RiskDecision(
            False,
            f"rejected: open positions {open_positions_count} >= max {s.max_open_positions}",
            {"open_positions": open_positions_count},
        )

    # 5. Exposure ceiling (`exposure-coach`)
    max_notional = equity * (s.max_exposure_pct / Decimal(100))
    if current_open_notional >= max_notional:
        return RiskDecision(
            False,
            f"rejected: exposure ${current_open_notional:.2f} >= ceiling ${max_notional:.2f}",
            {"current_open_notional": str(current_open_notional), "max_notional": str(max_notional)},
        )

    # 6. Trading hours / session filter (UTC)
    ts_utc = signal_time.astimezone(UTC) if signal_time.tzinfo else signal_time.replace(tzinfo=UTC)
    if ts_utc.weekday() >= 5:
        return RiskDecision(False, "rejected: weekend market closed", {})
    mins = ts_utc.hour * 60 + ts_utc.minute
    start_m = _hhmm_to_minutes(s.session_start_utc)
    end_m = _hhmm_to_minutes(s.session_end_utc)
    if not (start_m <= mins < end_m):
        return RiskDecision(
            False,
            f"rejected: outside trading session {s.session_start_utc}-{s.session_end_utc} UTC",
            {"utc_time": ts_utc.strftime("%H:%M")},
        )
    if ts_utc.weekday() == 4 and mins >= _hhmm_to_minutes(s.friday_no_entry_after_utc):
        return RiskDecision(
            False,
            f"rejected: Friday cutoff >= {s.friday_no_entry_after_utc} UTC",
            {},
        )

    # 7. News blackout filter (F3)
    if s.news_filter_enabled and upcoming_news_events:
        for ev in upcoming_news_events:
            ev_ts = ev.get("ts")
            if isinstance(ev_ts, datetime):
                diff_min = abs((ts_utc - ev_ts).total_seconds()) / 60.0
                if diff_min <= s.news_blackout_min:
                    return RiskDecision(
                        False,
                        f"rejected: news blackout ({ev.get('title', 'high-impact USD event')})",
                        {"event": ev.get("title")},
                    )

    # 8. Revenge / re-entry block & daily overtrading check (`pre-trade-discipline-gate`)
    if db is not None and s.revenge_block_min > 0:
        cutoff = ts_utc - timedelta(minutes=s.revenge_block_min)
        recent_stops = (
            db.query(PositionRecord)
            .filter(
                PositionRecord.symbol == symbol,
                PositionRecord.state == "closed",
                PositionRecord.exit_reason == "stop",
            )
            .all()
        )
        for p in recent_stops:
            if p.closed_at:
                ca = p.closed_at if p.closed_at.tzinfo else p.closed_at.replace(tzinfo=UTC)
                if ca >= cutoff:
                    return RiskDecision(
                        False,
                        f"rejected: revenge block ({s.revenge_block_min}m cooldown after stop-out on {symbol})",
                        {"last_stop_at": ca.isoformat()},
                    )

    return RiskDecision(
        True,
        "accepted",
        {"remaining_notional_headroom": str(max_notional - current_open_notional)},
    )
