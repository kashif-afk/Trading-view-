"""Fallback & Chaos tests (Phase 5):
1. Kill Postgres mid-signal -> verify SQLite outbox queues signals, blocks new entries, executes EXITS
   immediately on the broker, and replays into DB with zero loss and zero duplicates when DB recovers.
2. Kill Broker API -> verify new entries are blocked, exits are queued in outbox, and an alert is sent.
3. Kill Embedding service -> verify trading continues and embeddings are backfilled later when recovered.
4. Restore last night's backup into a fresh DB and verify ledger waterfall totals match 100%.
5. Webhook burst (20 signals in <1s + duplicate burst) -> never double-orders or loses a fill.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.engine.reconciler import reconcile_state, replay_outbox
from app.main import create_app
from app.notify.telegram import get_notifier
from app.storage.db import session_scope
from app.storage.models import LessonRecord, SignalRecord
from app.storage.outbox import OutboxStore
from tools.alert_sim import build_payload
from tools.backup_restore import create_backup, restore_and_verify


def test_db_outage_outbox_replay_and_exit_priority(
    test_settings: Settings, paper_broker: PaperBroker
) -> None:
    app = create_app(settings=test_settings, start_scheduler=False)
    app.state.broker = paper_broker
    tc = TestClient(app)
    sec = test_settings.webhook_secret.get_secret_value()

    # Open a position while DB is healthy
    r_open = tc.post("/webhook", json=build_payload(secret=sec, action="buy", price=4060.0, stop=4050.0, signal_id="db-up-buy"))
    assert r_open.json()["status"] == "accepted"
    assert len(paper_broker.positions()) == 1

    # Simulate DB outage
    app.state.force_db_down = True

    # New entry during DB outage -> rejected & queued in outbox; duplicate during outage ignored
    entry_down = build_payload(secret=sec, action="buy", price=4062.0, stop=4052.0, signal_id="db-down-entry-1")
    r_ed1 = tc.post("/webhook", json=entry_down)
    assert r_ed1.json()["status"] == "rejected_db_down"
    r_ed_dup = tc.post("/webhook", json=entry_down)
    assert r_ed_dup.json()["status"] == "duplicate"

    # Exit during DB outage -> STILL EXECUTES on broker immediately and queues for DB replay!
    exit_down = build_payload(secret=sec, action="close_long", price=4070.0, stop=None, target=None, signal_id="db-down-exit-1")
    r_ex = tc.post("/webhook", json=exit_down)
    assert r_ex.json()["status"] == "executed_via_outbox"
    assert len(paper_broker.positions()) == 0

    # Restore DB and replay outbox -> zero loss, zero duplicates
    app.state.force_db_down = False
    rep = replay_outbox(test_settings, paper_broker)
    assert rep["replayed_signals"] == 2
    assert OutboxStore(test_settings.outbox_path).pending_count() == 0

    with session_scope(test_settings) as db:
        ids = {s.signal_id for s in db.query(SignalRecord).all()}
        assert {"db-up-buy", "db-down-entry-1", "db-down-exit-1"}.issubset(ids)


def test_broker_outage_blocks_entries_queues_exits_and_alerts(
    test_settings: Settings, paper_broker: PaperBroker
) -> None:
    app = create_app(settings=test_settings, start_scheduler=False)
    app.state.broker = paper_broker
    tc = TestClient(app)
    sec = test_settings.webhook_secret.get_secret_value()
    notifier = get_notifier(test_settings)
    before_alerts = len(notifier.sent_messages)

    paper_broker.simulated_outage = True

    # Entry is blocked
    r_buy = tc.post("/webhook", json=build_payload(secret=sec, action="buy", price=4060.0, stop=4050.0, signal_id="brk-down-buy"))
    assert r_buy.json()["status"] == "rejected"
    assert "broker unreachable" in r_buy.json()["reason"]

    # Exit is queued in outbox
    r_exit = tc.post(
        "/webhook",
        json=build_payload(secret=sec, action="close_long", price=4065.0, stop=None, target=None, signal_id="brk-down-exit"),
    )
    assert r_exit.json()["status"] == "queued_exit"
    assert OutboxStore(test_settings.outbox_path).pending_count() >= 1
    assert len(notifier.sent_messages) > before_alerts

    paper_broker.simulated_outage = False


def test_embedding_service_outage_and_backfill(
    test_settings: Settings, paper_broker: PaperBroker
) -> None:
    s_ollama = test_settings.model_copy(update={"embed_provider": "ollama", "ollama_url": "http://127.0.0.1:9"})
    app = create_app(settings=s_ollama, start_scheduler=False)
    app.state.broker = paper_broker
    app.state.embedder.simulated_outage = True
    tc = TestClient(app)
    sec = s_ollama.webhook_secret.get_secret_value()

    # Open and close trade while embedding service is down -> trading succeeds!
    tc.post("/webhook", json=build_payload(secret=sec, action="buy", price=4060.0, stop=4050.0, signal_id="emb-buy-1"))
    r_close = tc.post(
        "/webhook",
        json=build_payload(secret=sec, action="close_long", price=4072.0, stop=None, target=None, signal_id="emb-close-1"),
    )
    assert r_close.json()["status"] == "accepted"

    outbox = OutboxStore(s_ollama.outbox_path)
    assert len(outbox.pending_embeddings()) == 1

    # Recover embedding service and backfill
    app.state.embedder.simulated_outage = False
    with session_scope(s_ollama) as db:
        done = app.state.embedder.backfill_pending(db)
        assert done == 1
        lesson = db.query(LessonRecord).one()
        assert len(lesson.embedding_json) == s_ollama.embed_dim


def test_backup_and_restore_into_fresh_db(
    test_settings: Settings, paper_broker: PaperBroker, tmp_path: Path
) -> None:
    app = create_app(settings=test_settings, start_scheduler=False)
    app.state.broker = paper_broker
    tc = TestClient(app)
    sec = test_settings.webhook_secret.get_secret_value()

    tc.post("/webhook", json=build_payload(secret=sec, action="buy", price=4060.0, stop=4050.0, signal_id="bk-buy"))
    tc.post("/webhook", json=build_payload(secret=sec, action="close_long", price=4075.0, stop=None, target=None, signal_id="bk-close"))

    backup_path = create_backup(test_settings, tmp_path / "backups")
    assert backup_path.exists()

    fresh_db = tmp_path / "restored.db"
    fresh_settings = test_settings.model_copy(update={"database_url": f"sqlite:///{fresh_db.as_posix()}"})
    verify = restore_and_verify(backup_path, fresh_settings)
    assert verify["matched"] is True
    assert Decimal(verify["restored_ending_balance"]) > Decimal(2000)


def test_chaos_webhook_burst_20_in_1s_and_duplicates(
    test_settings: Settings, paper_broker: PaperBroker
) -> None:
    app = create_app(settings=test_settings, start_scheduler=False)
    app.state.broker = paper_broker
    tc = TestClient(app)
    sec = test_settings.webhook_secret.get_secret_value()

    # Fire 20 webhooks in a burst: 10 duplicates of the same signal_id + 10 distinct signals
    payloads = [
        build_payload(
            secret=sec,
            action="buy",
            price=4060.0,
            stop=4048.0,
            target=4084.0,
            signal_id="burst-dup-1" if i < 10 else f"burst-unique-{i}",
        )
        for i in range(20)
    ]

    def _post(p: dict) -> dict:
        r = tc.post("/webhook", json=p)
        assert r.status_code == 200
        return r.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_post, payloads))

    accepted = [r for r in results if r["status"] == "accepted"]
    duplicates = [r for r in results if r["status"] == "duplicate"]
    # PaperBroker allows 1 position per symbol (`XAU_USD`), so exactly 1 entry fills and 9 of the 10
    # identical `burst-dup-1` calls are caught as duplicates. Zero double-orders, ledger reconciles.
    assert len(accepted) == 1
    assert len(duplicates) == 9
    assert len(paper_broker.positions()) == 1
    assert reconcile_state(test_settings, paper_broker)["matched"] is True
