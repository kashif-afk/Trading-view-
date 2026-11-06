"""Performance analytics, weekly learning digest, and change-control proposal generator
(Phase 4 page 5 & Phase 4.5 steps 5–6).

Mirrors `trade-performance-coach`, `weekly-performance-digest`, and `backtest-expert` (requiring
at least 30 trades of evidence before a rule change can be proposed or approved).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.storage.models import RuleChangeProposal, TradeJournal

MIN_EVIDENCE_TRADES = 30

RULE_DIFF_SUGGESTIONS: dict[str, str] = {
    "against_regime": (
        "--- a/docs/strategy-spec.md\n"
        "+++ b/docs/strategy-spec.md\n"
        "@@ -F2 HTF regime @@\n"
        "-F2 HTF regime (optional, default OFF)\n"
        "+F2 HTF regime (MANDATORY ON): require completed 4h close > 4h EMA200 for longs, < for shorts"
    ),
    "late_entry": (
        "--- a/tradingview/strategy.pine\n"
        "+++ b/tradingview/strategy.pine\n"
        "@@ -rsiOb / rsiOs @@\n"
        "-rsiOb = input.float(70, 'RSI Overbought')\n"
        "-rsiOs = input.float(30, 'RSI Oversold')\n"
        "+rsiOb = input.float(63, 'RSI Overbought')\n"
        "+rsiOs = input.float(37, 'RSI Oversold')"
    ),
    "stop_too_tight": (
        "--- a/tradingview/strategy.pine\n"
        "+++ b/tradingview/strategy.pine\n"
        "@@ -atrMult @@\n"
        "-atrMult = input.float(1.5, 'Stop ATR Multiplier')\n"
        "+atrMult = input.float(1.85, 'Stop ATR Multiplier')"
    ),
    "news_event": (
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@ -NEWS_BLACKOUT_MIN @@\n"
        "-NEWS_BLACKOUT_MIN=30\n"
        "+NEWS_BLACKOUT_MIN=45"
    ),
}


def compute_performance_metrics(db: Session) -> dict[str, Any]:
    trades = db.query(TradeJournal).order_by(TradeJournal.closed_at.asc()).all()
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "avg_r": 0.0,
            "max_drawdown_r": 0.0,
            "net_pnl_usd": 0.0,
            "by_symbol": {},
            "by_strategy": {},
            "mistake_counts": {},
        }

    rs = [float(t.r_multiple) for t in trades]
    pnls = [float(t.realized_pnl) for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    win_rate = len(wins) * 100.0 / len(rs)
    avg_r = sum(rs) / len(rs)
    pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else (99.0 if sum(wins) > 0 else 0.0)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_symbol: dict[str, dict[str, Any]] = {}
    by_strategy: dict[str, dict[str, Any]] = {}
    mistakes: Counter[str] = Counter()

    for t in trades:
        for bucket, key in ((by_symbol, t.symbol), (by_strategy, t.strategy)):
            b = bucket.setdefault(key, {"trades": 0, "wins": 0, "total_r": 0.0, "net_pnl": 0.0})
            b["trades"] += 1
            if float(t.r_multiple) > 0:
                b["wins"] += 1
            b["total_r"] = round(b["total_r"] + float(t.r_multiple), 2)
            b["net_pnl"] = round(b["net_pnl"] + float(t.realized_pnl), 2)
        for tag in (t.tags or []):
            mistakes[tag] += 1

    return {
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "expectancy_r": round(avg_r, 3),
        "profit_factor": round(pf, 2),
        "avg_r": round(avg_r, 3),
        "max_drawdown_r": round(max_dd, 2),
        "net_pnl_usd": round(sum(pnls), 2),
        "by_symbol": by_symbol,
        "by_strategy": by_strategy,
        "mistake_counts": dict(mistakes),
    }


def generate_weekly_learning_digest(db: Session) -> dict[str, Any]:
    metrics = compute_performance_metrics(db)
    mistakes = metrics["mistake_counts"]
    non_win = {k: v for k, v in mistakes.items() if k != "clean_win"}
    top_mistake = max(non_win, key=non_win.get) if non_win else None

    proposal_created = None
    if top_mistake and metrics["total_trades"] >= MIN_EVIDENCE_TRADES:
        diff_txt = RULE_DIFF_SUGGESTIONS.get(
            top_mistake,
            f"# Proposed filter adjustment for recurring mistake: {top_mistake}",
        )
        existing = (
            db.query(RuleChangeProposal)
            .filter_by(target_mistake_tag=top_mistake, status="pending_review")
            .one_or_none()
        )
        if not existing:
            prop = RuleChangeProposal(
                target_mistake_tag=top_mistake,
                evidence_trade_count=metrics["total_trades"],
                proposed_diff=diff_txt,
                backtest_summary={
                    "sample_size": metrics["total_trades"],
                    "current_avg_r": metrics["avg_r"],
                    "mistake_occurrences": non_win[top_mistake],
                },
                status="pending_review",
            )
            db.add(prop)
            db.flush()
            proposal_created = prop.id

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "top_recurring_mistake": top_mistake,
        "proposal_id": proposal_created,
    }


def approve_rule_change_proposal(db: Session, proposal_id: int, approver: str) -> RuleChangeProposal:
    prop = db.query(RuleChangeProposal).filter_by(id=proposal_id).one()
    if prop.evidence_trade_count < MIN_EVIDENCE_TRADES:
        raise ValueError(
            f"Change control requires >= {MIN_EVIDENCE_TRADES} trades of evidence (got {prop.evidence_trade_count})."
        )
    prop.status = "approved"
    prop.approved_by = approver
    prop.approved_at = datetime.now(timezone.utc)
    db.flush()
    return prop
