"""Unit tests (Phase 5): sizer rounding & min-notional, risk-gate rules, circuit breaker,
live-mode guard, ledger math = broker balance, and security helpers."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.brokers.paper import PaperBroker
from app.core.config import LiveModeNotConfirmed, Settings, assert_mode_allowed
from app.core.security import RateLimiter, freshness_error, redact, secret_ok
from app.core.state import CircuitBreaker, KillSwitch
from app.engine.risk_gate import evaluate_risk_gate
from app.engine.sizer import calculate_position_size
from app.ledger.service import compute_waterfall, ensure_initial_deposit, record_entry
from app.storage.db import session_scope


def test_sizer_rounding_and_rejections() -> None:
    # $2,000 equity, 0.5% risk = $10 budget. Entry 4060, stop 4048.58 -> dist = 11.42 -> raw = 0.8756 -> floor 0.87
    res = calculate_position_size(
        equity=Decimal(2000),
        risk_pct=Decimal("0.5"),
        entry_price=Decimal("4060.00"),
        stop_price=Decimal("4048.58"),
        min_qty=Decimal("0.01"),
        qty_step=Decimal("0.01"),
    )
    assert res.allowed is True
    assert res.qty == Decimal("0.87")
    assert res.risk_usd <= Decimal("10.00")

    # Rejects when min_qty (1.0 oz) exceeds risk budget (never rounds up!)
    res_big_lot = calculate_position_size(
        equity=Decimal(2000),
        risk_pct=Decimal("0.5"),
        entry_price=Decimal("4060.00"),
        stop_price=Decimal("4045.00"),  # $15 stop -> raw qty 0.66 < 1.0
        min_qty=Decimal("1.0"),
        qty_step=Decimal("1.0"),
    )
    assert res_big_lot.allowed is False
    assert "min_qty" in res_big_lot.reason

    # Rejects naked entry (no stop)
    res_naked = calculate_position_size(
        equity=Decimal(2000),
        risk_pct=Decimal("0.5"),
        entry_price=Decimal(4060),
        stop_price=None,
    )
    assert res_naked.allowed is False
    assert "missing stop-loss" in res_naked.reason

    # Rejects below min_notional
    res_notional = calculate_position_size(
        equity=Decimal(2000),
        risk_pct=Decimal("0.5"),
        entry_price=Decimal(10),
        stop_price=Decimal(5),
        min_notional=Decimal(500),
    )
    assert res_notional.allowed is False
    assert "min_notional" in res_notional.reason


def test_risk_gate_and_circuit_breaker(test_settings: Settings) -> None:
    s = test_settings.model_copy(
        update={
            "session_start_utc": "07:00",
            "session_end_utc": "20:00",
            "friday_no_entry_after_utc": "18:00",
        }
    )
    wed_10am = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    wed_03am = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)

    # Valid entry in session
    ok = evaluate_risk_gate(
        action="buy",
        symbol="XAU_USD",
        entry_price=Decimal(4060),
        stop_price=Decimal(4050),
        signal_time=wed_10am,
        equity=Decimal(2000),
        open_positions_count=0,
        current_open_notional=Decimal(0),
        settings=s,
    )
    assert ok.allowed is True

    # Outside session rejected
    out_sess = evaluate_risk_gate(
        action="buy",
        symbol="XAU_USD",
        entry_price=Decimal(4060),
        stop_price=Decimal(4050),
        signal_time=wed_03am,
        equity=Decimal(2000),
        open_positions_count=0,
        current_open_notional=Decimal(0),
        settings=s,
    )
    assert out_sess.allowed is False
    assert "outside trading session" in out_sess.reason

    # Max positions rejected
    max_pos = evaluate_risk_gate(
        action="buy",
        symbol="XAU_USD",
        entry_price=Decimal(4060),
        stop_price=Decimal(4050),
        signal_time=wed_10am,
        equity=Decimal(2000),
        open_positions_count=3,
        current_open_notional=Decimal(1000),
        settings=s,
    )
    assert max_pos.allowed is False
    assert "open positions 3 >= max 3" in max_pos.reason

    # Daily loss circuit breaker trips at -2.0% ($1958 vs $2000 start = -2.1%)
    cb = CircuitBreaker(s.state_dir, s.max_daily_loss_pct)
    cb.update(Decimal(2000))
    brk_dec = evaluate_risk_gate(
        action="buy",
        symbol="XAU_USD",
        entry_price=Decimal(4060),
        stop_price=Decimal(4050),
        signal_time=wed_10am,
        equity=Decimal(1958),
        open_positions_count=0,
        current_open_notional=Decimal(0),
        settings=s,
    )
    assert brk_dec.allowed is False
    assert "daily loss 2.10% >= 2.0%" in brk_dec.reason

    # Exit is STILL allowed even when circuit breaker and kill switch are active
    KillSwitch(s.state_dir).engage("test_kill", "unit")
    exit_dec = evaluate_risk_gate(
        action="close_long",
        symbol="XAU_USD",
        entry_price=Decimal(4060),
        stop_price=None,
        signal_time=wed_03am,
        equity=Decimal(1900),
        open_positions_count=1,
        current_open_notional=Decimal(4000),
        settings=s,
    )
    assert exit_dec.allowed is True


def test_live_mode_guard_blocks_without_typed_confirmation(tmp_path: Path) -> None:
    s = Settings(execution_mode="live", data_dir=tmp_path)
    with pytest.raises(LiveModeNotConfirmed):
        assert_mode_allowed(s)


def test_ledger_double_entry_matches_broker_balance(test_settings: Settings, paper_broker: PaperBroker) -> None:
    with session_scope(test_settings) as db:
        ensure_initial_deposit(db, test_settings.account_size, "USD")
        record_entry(
            db,
            entry_type="deposit",
            direction="in",
            amount=Decimal("500.00"),
            ref="dep1",
            broker=paper_broker,
            sync_broker_cash=True,
        )
        record_entry(
            db,
            entry_type="withdrawal",
            direction="out",
            amount=Decimal("150.00"),
            ref="wd1",
            broker=paper_broker,
            sync_broker_cash=True,
        )
        record_entry(
            db,
            entry_type="funding",
            direction="out",
            amount=Decimal("2.25"),
            ref="fund1",
            broker=paper_broker,
            sync_broker_cash=True,
        )
        # Adjustment without note must raise
        with pytest.raises(ValueError, match="explanatory note"):
            record_entry(db, entry_type="adjustment", direction="in", amount=Decimal(10))

        record_entry(
            db,
            entry_type="adjustment",
            direction="in",
            amount=Decimal("12.25"),
            note="Broker fee rebate",
            broker=paper_broker,
            sync_broker_cash=True,
        )
        wf = compute_waterfall(db)

    bal = paper_broker.balances()
    assert abs(wf["ending_balance_exact"] - bal.cash_balance) <= Decimal("0.0001")
    assert wf["ending_balance"] == Decimal("2360.00")


def test_security_helpers() -> None:
    assert secret_ok("abc", "abc") is True
    assert secret_ok("abc", "abd") is False
    assert secret_ok(None, "abc") is False

    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    assert freshness_error("2026-11-06T11:59:30Z", 60, 10, now=now) is None
    assert "stale" in (freshness_error("2026-11-06T11:58:00Z", 60, 10, now=now) or "")
    assert "future" in (freshness_error("2026-11-06T12:01:00Z", 60, 10, now=now) or "")

    redacted = redact('{"secret":"my-top-secret","action":"buy"}')
    assert "my-top-secret" not in redacted
    assert "***" in redacted

    rl = RateLimiter(per_min=60, burst=2)
    assert rl.allow("1.2.3.4") is True
    assert rl.allow("1.2.3.4") is True
    assert rl.allow("1.2.3.4") is False
