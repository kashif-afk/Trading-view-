"""Memory & learning layer tests (Phase 4.5 & Phase 5):
1. Seeded past trades return expected similar setups.
2. Advisory recall NEVER blocks an exit.
3. Optional caution rule only fires when enabled AND sample size >= caution_min_samples (20).
4. Change-control requires >= 30 trades of evidence before a rule proposal can be approved.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.memory.digest import (
    approve_rule_change_proposal,
    generate_weekly_learning_digest,
)
from app.memory.journal import record_closed_trade_journal
from app.memory.recall import recall_similar_setups
from app.storage.db import session_scope
from app.storage.models import PositionRecord, RuleChangeProposal, SignalRecord


def _seed_closed_trade(
    db,
    settings: Settings,
    idx: int,
    *,
    pnl: Decimal,
    htf_bias: int = 1,
    rsi: float = 55.0,
) -> None:
    sig_id = f"seed-sig-{idx}"
    sig = SignalRecord(
        signal_id=sig_id,
        strategy="XAU_EMA_RSI_v0.1",
        symbol="XAUUSD",
        broker_symbol="XAU_USD",
        action="buy",
        price=Decimal(4060),
        stop=Decimal(4050),
        target=Decimal(4080),
        timeframe="15m",
        signal_time="2026-11-06T10:00:00Z",
        payload={"indicators": {"rsi": rsi, "atr": 8.0, "htf_bias": htf_bias}},
        status="accepted",
        reason="accepted",
    )
    db.add(sig)
    pos = PositionRecord(
        symbol="XAU_USD",
        strategy="XAU_EMA_RSI_v0.1",
        direction="long",
        qty=Decimal("1.0"),
        avg_entry_price=Decimal(4060),
        stop=Decimal(4050),
        target=Decimal(4080),
        initial_risk_usd=Decimal(10),
        entry_signal_id=sig_id,
        state="closed",
        exit_price=Decimal(4060) + pnl,
        exit_reason="stop" if pnl < 0 else "target",
        realized_pnl=pnl,
        fees_paid=Decimal(0),
    )
    db.add(pos)
    db.flush()
    record_closed_trade_journal(db, pos=pos, signal=sig, settings=settings)


def test_memory_recall_caution_rule_and_exit_bypass(test_settings: Settings) -> None:
    with session_scope(test_settings) as db:
        # Seed 15 losing trades in bull regime (-0.8R each, late RSI=68)
        for i in range(15):
            _seed_closed_trade(db, test_settings, i, pnl=Decimal("-8.0"), htf_bias=1, rsi=68.0)

        # 1. Recall returns seeded setups, identifies 'late_entry' tag, but sample size 15 < 20 -> caution FALSE
        s_caution_on = test_settings.model_copy(
            update={"caution_rule_enabled": True, "caution_min_samples": 20, "caution_avg_r_threshold": Decimal("-0.3")}
        )
        adv_15 = recall_similar_setups(
            db,
            symbol="XAU_USD",
            timeframe="15m",
            strategy="XAU_EMA_RSI_v0.1",
            action="buy",
            indicators={"rsi": 67.5, "atr": 8.0, "htf_bias": 1},
            settings=s_caution_on,
        )
        assert adv_15["count"] == 15
        assert adv_15["avg_r"] == -0.8
        assert adv_15["top_mistake_tag"] == "late_entry"
        assert adv_15["caution_triggered"] is False

        # 2. Add 6 more losing trades (total 21 >= 20): caution rule fires ONLY when enabled
        for i in range(15, 21):
            _seed_closed_trade(db, test_settings, i, pnl=Decimal("-8.0"), htf_bias=1, rsi=68.0)

        adv_off = recall_similar_setups(
            db,
            symbol="XAU_USD",
            timeframe="15m",
            strategy="XAU_EMA_RSI_v0.1",
            action="buy",
            indicators={"rsi": 67.5, "atr": 8.0, "htf_bias": 1},
            settings=test_settings,  # caution_rule_enabled=False by default
        )
        assert adv_off["caution_triggered"] is False

        adv_on = recall_similar_setups(
            db,
            symbol="XAU_USD",
            timeframe="15m",
            strategy="XAU_EMA_RSI_v0.1",
            action="buy",
            indicators={"rsi": 67.5, "atr": 8.0, "htf_bias": 1},
            settings=s_caution_on,
        )
        assert adv_on["caution_triggered"] is True
        assert adv_on["caution_action"] == "halve"

        # 3. Exits ALWAYS bypass recall / caution
        adv_exit = recall_similar_setups(
            db,
            symbol="XAU_USD",
            timeframe="15m",
            strategy="XAU_EMA_RSI_v0.1",
            action="close_long",
            indicators={"rsi": 67.5, "atr": 8.0, "htf_bias": 1},
            settings=s_caution_on,
        )
        assert adv_exit["caution_triggered"] is False
        assert adv_exit["note"] == "exit_signal_bypass"


def test_change_control_requires_30_trades(test_settings: Settings) -> None:
    with session_scope(test_settings) as db:
        for i in range(32):
            _seed_closed_trade(db, test_settings, 100 + i, pnl=Decimal("-6.0"), htf_bias=-1, rsi=55.0)
        digest = generate_weekly_learning_digest(db)
        assert digest["metrics"]["total_trades"] >= 30
        assert digest["top_recurring_mistake"] == "against_regime"
        assert digest["proposal_id"] is not None

        prop = approve_rule_change_proposal(db, digest["proposal_id"], approver="qa_lead")
        assert prop.status == "approved"

        # Under-30 evidence proposal cannot be approved
        under = RuleChangeProposal(
            target_mistake_tag="late_entry",
            evidence_trade_count=12,
            proposed_diff="diff",
            status="pending_review",
        )
        db.add(under)
        db.flush()
        with pytest.raises(ValueError, match="30 trades"):
            approve_rule_change_proposal(db, under.id, approver="qa_lead")
