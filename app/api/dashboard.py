"""Dashboard routes & JSON API endpoints (Phase 4 & Phase 4.5).

Pages:
1. `/`               Overview (equity curve, cash balance, net deposits vs trading P&L, open risk, breaker, kill switch)
2. `/cashflows`      Cash flows waterfall, monthly in/out bar chart, filterable ledger table + CSV export
3. `/positions`      Live positions with unrealized P&L, stop/target, pending/open orders, approve/reject controls
4. `/signals`        Webhook audit trail + advisory recall stats from similar past setups
5. `/performance`    Win rate, expectancy, profit factor, avg R, max DD, per-symbol & per-strategy breakdown
6. `/lessons`        Phase 4.5 searchable trade lessons, mistake taxonomy counts, and rule-change proposals
7. `/settings`       Risk parameters, mode display (live read-only), manual cash-flow entry form
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.auth import create_session_token, require_user, verify_credentials
from app.brokers.oanda import get_broker
from app.core.config import Settings
from app.core.state import CircuitBreaker, KillSwitch
from app.engine.killswitch import reset_kill_switch, trigger_kill_switch
from app.engine.pipeline import approve_pending_order
from app.ledger.service import (
    compute_waterfall,
    ensure_initial_deposit,
    export_ledger_csv,
    monthly_in_out,
    record_entry,
)
from app.memory.digest import (
    approve_rule_change_proposal,
    compute_performance_metrics,
    generate_weekly_learning_digest,
)
from app.storage.db import db_healthy, session_scope
from app.storage.models import (
    LedgerEntry,
    LessonRecord,
    OrderRecord,
    PositionRecord,
    RuleChangeProposal,
    SignalRecord,
    TradeJournal,
)

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _common_ctx(request: Request, user: str) -> dict[str, Any]:
    s: Settings = request.app.state.settings
    ks = KillSwitch(s.state_dir)
    cb = CircuitBreaker(s.state_dir, s.max_daily_loss_pct)
    return {
        "request": request,
        "user": user,
        "mode": s.execution_mode,
        "broker_name": s.broker,
        "kill_active": ks.active(),
        "kill_info": ks.info(),
        "breaker": cb.status(),
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(""),
) -> Response:
    s: Settings = request.app.state.settings
    with session_scope(s) as db:
        u = verify_credentials(db, username, password, totp_code or None)
    if not u:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials or TOTP code"},
            status_code=401,
        )
    token = create_session_token(u.username, s)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("session_token", token, httponly=True, secure=s.dashboard_secure_cookies, samesite="lax")
    return resp


@router.post("/logout")
def logout() -> Response:
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session_token")
    return resp


@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request, user: str = Depends(require_user)) -> Response:
    s: Settings = request.app.state.settings
    brk = getattr(request.app.state, "broker", None) or get_broker(s)
    bal = brk.balances()
    b_pos = brk.positions()

    with session_scope(s) as db:
        ensure_initial_deposit(db, s.account_size, s.base_currency)
        wf = compute_waterfall(db)
        open_pos = db.query(PositionRecord).filter_by(state="open").all()
        open_risk = sum((Decimal(str(p.initial_risk_usd)) for p in open_pos), Decimal("0"))
        ledger_rows = db.query(LedgerEntry).order_by(LedgerEntry.ts.asc()).all()

    eq_points: list[dict[str, Any]] = []
    running = Decimal("0")
    for r in ledger_rows:
        running += Decimal(str(r.amount)) if r.direction == "in" else -Decimal(str(r.amount))
        eq_points.append({"ts": r.ts.strftime("%Y-%m-%d %H:%M"), "balance": float(running)})

    net_deposits = wf["deposits"] + wf["withdrawals"]
    trading_pnl = wf["realized_pnl"] + wf["fees"] + wf["funding"]

    ctx = {
        **_common_ctx(request, user),
        "page": "overview",
        "balance": bal,
        "waterfall": wf,
        "net_deposits": net_deposits,
        "trading_pnl": trading_pnl,
        "open_risk_usd": open_risk.quantize(Decimal("0.01")),
        "open_positions_count": len(b_pos),
        "eq_points": eq_points,
    }
    return templates.TemplateResponse(request, "overview.html", ctx)


@router.get("/cashflows", response_class=HTMLResponse)
def cashflows_page(
    request: Request,
    ledger_type: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    start_ts = datetime.fromisoformat(start_date) if start_date else None
    end_ts = datetime.fromisoformat(end_date) if end_date else None

    with session_scope(s) as db:
        ensure_initial_deposit(db, s.account_size, s.base_currency)
        wf = compute_waterfall(db, start_ts=start_ts, end_ts=end_ts)
        monthly = monthly_in_out(db)
        q = db.query(LedgerEntry).order_by(LedgerEntry.ts.desc())
        if ledger_type:
            q = q.filter_by(type=ledger_type)
        entries = q.limit(500).all()

    ctx = {
        **_common_ctx(request, user),
        "page": "cashflows",
        "waterfall": wf,
        "monthly": monthly,
        "entries": entries,
        "selected_type": ledger_type or "",
    }
    return templates.TemplateResponse(request, "cashflows.html", ctx)


@router.get("/cashflows/export.csv")
def export_cashflows_csv(
    request: Request,
    ledger_type: str | None = Query(default=None),
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    with session_scope(s) as db:
        q = db.query(LedgerEntry).order_by(LedgerEntry.ts.asc())
        if ledger_type:
            q = q.filter_by(type=ledger_type)
        csv_text = export_ledger_csv(q.all())
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ledger_export.csv"},
    )


@router.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request, user: str = Depends(require_user)) -> Response:
    s: Settings = request.app.state.settings
    brk = getattr(request.app.state, "broker", None) or get_broker(s)
    live_positions = brk.positions()
    quotes = {p.symbol: brk.quote(p.symbol, fallback_price=p.avg_price) for p in live_positions}

    with session_scope(s) as db:
        orders = db.query(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(100).all()
        pending_orders = [o for o in orders if o.status == "pending_approval"]

    ctx = {
        **_common_ctx(request, user),
        "page": "positions",
        "live_positions": live_positions,
        "quotes": quotes,
        "orders": orders,
        "pending_orders": pending_orders,
    }
    return templates.TemplateResponse(request, "positions.html", ctx)


@router.post("/orders/{order_id}/decision")
def decide_pending_order(
    order_id: str,
    request: Request,
    decision: str = Form(...),
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    brk = getattr(request.app.state, "broker", None) or get_broker(s)
    approve_pending_order(order_id, approve=(decision == "approve"), operator=user, settings=s, broker=brk)
    return RedirectResponse(url="/positions", status_code=303)


@router.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request, user: str = Depends(require_user)) -> Response:
    s: Settings = request.app.state.settings
    with session_scope(s) as db:
        signals = db.query(SignalRecord).order_by(SignalRecord.received_at.desc()).limit(200).all()
    ctx = {**_common_ctx(request, user), "page": "signals", "signals": signals}
    return templates.TemplateResponse(request, "signals.html", ctx)


@router.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request, user: str = Depends(require_user)) -> Response:
    s: Settings = request.app.state.settings
    with session_scope(s) as db:
        perf = compute_performance_metrics(db)
    ctx = {**_common_ctx(request, user), "page": "performance", "perf": perf}
    return templates.TemplateResponse(request, "performance.html", ctx)


@router.post("/performance/run-digest")
def run_digest_endpoint(request: Request, user: str = Depends(require_user)) -> Response:
    s: Settings = request.app.state.settings
    with session_scope(s) as db:
        generate_weekly_learning_digest(db)
    return RedirectResponse(url="/lessons", status_code=303)


@router.get("/lessons", response_class=HTMLResponse)
def lessons_page(
    request: Request,
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    with session_scope(s) as db:
        perf = compute_performance_metrics(db)
        lessons_q = db.query(LessonRecord).order_by(LessonRecord.created_at.desc())
        lessons_all = lessons_q.limit(200).all()
        if q:
            ql = q.lower()
            lessons_all = [
                row
                for row in lessons_all
                if ql in (row.lesson_text or "").lower() or ql in (row.setup_text or "").lower()
            ]
        if tag:
            lessons_all = [row for row in lessons_all if tag in (row.tags or [])]
        proposals = db.query(RuleChangeProposal).order_by(RuleChangeProposal.created_at.desc()).all()

    ctx = {
        **_common_ctx(request, user),
        "page": "lessons",
        "lessons": lessons_all,
        "mistake_counts": perf["mistake_counts"],
        "proposals": proposals,
        "search_q": q or "",
        "selected_tag": tag or "",
    }
    return templates.TemplateResponse(request, "lessons.html", ctx)


@router.post("/lessons/proposals/{proposal_id}/approve")
def approve_proposal_route(
    proposal_id: int,
    request: Request,
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    with session_scope(s) as db:
        try:
            approve_rule_change_proposal(db, proposal_id, approver=user)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(url="/lessons", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(require_user)) -> Response:
    s: Settings = request.app.state.settings
    ctx = {
        **_common_ctx(request, user),
        "page": "settings",
        "risk_params": s.risk_fingerprint(),
        "risk_hash": s.risk_hash(),
    }
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings/cashflow")
def add_manual_cashflow(
    request: Request,
    entry_type: str = Form(...),
    direction: str = Form(...),
    amount: str = Form(...),
    ref: str = Form(""),
    note: str = Form(""),
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    brk = getattr(request.app.state, "broker", None) or get_broker(s)
    with session_scope(s) as db:
        ensure_initial_deposit(db, s.account_size, s.base_currency)
        try:
            record_entry(
                db,
                entry_type=entry_type,
                direction=direction,
                amount=Decimal(amount),
                currency=s.base_currency,
                ref=ref or f"manual:{user}",
                note=note,
                broker=brk,
                sync_broker_cash=True,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(url="/cashflows", status_code=303)


@router.post("/killswitch/engage")
def web_engage_killswitch(
    request: Request,
    reason: str = Form("dashboard_button"),
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    brk = getattr(request.app.state, "broker", None) or get_broker(s)
    trigger_kill_switch(reason=reason, by=user, settings=s, broker=brk)
    return RedirectResponse(url="/", status_code=303)


@router.post("/killswitch/reset")
def web_reset_killswitch(
    request: Request,
    user: str = Depends(require_user),
) -> Response:
    s: Settings = request.app.state.settings
    reset_kill_switch(by=user, settings=s)
    return RedirectResponse(url="/", status_code=303)
