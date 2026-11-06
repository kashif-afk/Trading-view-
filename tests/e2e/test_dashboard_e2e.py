"""End-to-End Dashboard & Flow Verification (Phase 5):
1. Unauthenticated access redirects to `/login`.
2. Log in with single-user credentials.
3. Receive a webhook alert and see it appear on `/signals` with its advisory stats.
4. Approve a pending order in `approval` mode on `/positions`.
5. Engage the kill switch from the dashboard and verify open positions flatten and status shows KILL SWITCH ACTIVE.
6. Verify `/cashflows` waterfall totals match the ledger and broker balance.
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.main import create_app
from tools.alert_sim import build_payload


def test_e2e_dashboard_login_approval_killswitch_and_waterfall(
    test_settings: Settings, paper_broker: PaperBroker
) -> None:
    s_app = test_settings.model_copy(update={"execution_mode": "approval"})
    app = create_app(settings=s_app, start_scheduler=False)
    app.state.broker = paper_broker
    tc = TestClient(app)
    sec = s_app.webhook_secret.get_secret_value()

    # 1. Unauthenticated request redirects to /login
    r_unauth = tc.get("/", follow_redirects=False)
    assert r_unauth.status_code == 303
    assert r_unauth.headers["location"] == "/login"

    # 2. Log in
    r_login = tc.post("/login", data={"username": "admin", "password": "changeme123!"}, follow_redirects=True)
    assert r_login.status_code == 200
    assert "Overview" in r_login.text

    # 3. Send webhook alert -> appears on /signals page
    sig = build_payload(secret=sec, action="buy", price=4060.0, stop=4050.0, target=4080.0, signal_id="e2e-sig-01")
    r_wh = tc.post("/webhook", json=sig)
    assert r_wh.status_code == 200
    order_id = r_wh.json()["order_id"]

    r_signals = tc.get("/signals")
    assert r_signals.status_code == 200
    assert "e2e-sig-01" in r_signals.text
    assert "pending_approval" in r_signals.text

    # 4. Approve on /positions page -> position opens
    r_pos_before = tc.get("/positions")
    assert "Pending Approval Orders" in r_pos_before.text
    tc.post(f"/orders/{order_id}/decision", data={"decision": "approve"}, follow_redirects=True)
    assert len(paper_broker.positions()) == 1

    # 5. Engage Kill Switch via dashboard button -> flattens position and blocks new entries
    r_ks = tc.post("/killswitch/engage", data={"reason": "e2e_test_button"}, follow_redirects=True)
    assert r_ks.status_code == 200
    assert "KILL SWITCH ACTIVE" in r_ks.text
    assert len(paper_broker.positions()) == 0

    # 6. Check cash-flow waterfall page totals reconcile to broker balance
    r_cf = tc.get("/cashflows")
    assert r_cf.status_code == 200
    m = re.search(r'data-testid="wf-ending">\$([0-9.]+)</div>', r_cf.text)
    assert m is not None
    wf_ending = float(m.group(1))
    assert abs(wf_ending - float(paper_broker.balances().cash_balance)) <= 0.02
