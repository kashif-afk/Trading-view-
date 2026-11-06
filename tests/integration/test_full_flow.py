"""Integration tests (Phase 5): full flow against PaperBroker (signal -> order -> fill -> ledger ->
dashboard -> journal -> reconciliation) plus approval mode workflow."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.engine.reconciler import reconcile_state
from app.main import create_app
from app.storage.db import session_scope
from app.storage.models import LessonRecord, TradeJournal
from tools.alert_sim import build_payload


def test_full_paper_lifecycle_and_reconciliation(
    client: TestClient, test_settings: Settings, paper_broker: PaperBroker
) -> None:
    sec = test_settings.webhook_secret.get_secret_value()

    # 1. Entry signal (buy XAUUSD @ 4060, SL 4050, TP 4080)
    buy_sig = build_payload(secret=sec, action="buy", price=4060.0, stop=4050.0, target=4080.0, signal_id="int-buy-1")
    r_buy = client.post("/webhook", json=buy_sig)
    assert r_buy.status_code == 200
    assert r_buy.json()["status"] == "accepted"
    assert len(paper_broker.positions()) == 1

    # 2. Close signal (close_long @ 4075 -> profitable close)
    close_sig = build_payload(
        secret=sec, action="close_long", price=4075.0, stop=None, target=None, reason="target", signal_id="int-close-1"
    )
    r_close = client.post("/webhook", json=close_sig)
    assert r_close.status_code == 200
    assert r_close.json()["status"] == "accepted"
    assert len(paper_broker.positions()) == 0

    # 3. Verify journal record and embedded lesson created automatically
    with session_scope(test_settings) as db:
        journals = db.query(TradeJournal).all()
        lessons = db.query(LessonRecord).all()
        assert len(journals) == 1
        assert len(lessons) == 1
        assert float(journals[0].r_multiple) > 0

    # 4. Reconciler verifies ledger sum == broker cash_balance
    rec = reconcile_state(test_settings, paper_broker)
    assert rec["matched"] is True
    assert rec["discrepancies"] == []


def test_approval_mode_workflow(test_settings: Settings, paper_broker: PaperBroker) -> None:
    s_app = test_settings.model_copy(update={"execution_mode": "approval"})
    app = create_app(settings=s_app, start_scheduler=False)
    app.state.broker = paper_broker
    tc = TestClient(app)

    sec = s_app.webhook_secret.get_secret_value()
    sig = build_payload(secret=sec, action="buy", price=4060.0, stop=4050.0, target=4080.0, signal_id="app-buy-1")
    r = tc.post("/webhook", json=sig)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending_approval"
    order_id = data["order_id"]
    assert len(paper_broker.positions()) == 0

    # Log in to dashboard and approve order
    tc.post("/login", data={"username": "admin", "password": "changeme123!"})
    r_dec = tc.post(f"/orders/{order_id}/decision", data={"decision": "approve"}, follow_redirects=False)
    assert r_dec.status_code == 303
    assert len(paper_broker.positions()) == 1
