"""Contract tests (Phase 5): webhook schema validation with valid, malformed, stale,
duplicate, missing-stop, extra-field, and wrong-secret payloads, plus raw JSONL audit verification."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.config import Settings
from tools.alert_sim import build_payload


def test_webhook_contract_cases(client: TestClient, test_settings: Settings) -> None:
    sec = test_settings.webhook_secret.get_secret_value()

    # 1. Valid entry payload -> 200 accepted
    valid = build_payload(secret=sec, action="buy", price=4060.0, stop=4048.0, target=4084.0, signal_id="sig-valid-1")
    r1 = client.post("/webhook", json=valid)
    assert r1.status_code == 200
    assert r1.json()["status"] == "accepted"

    # 2. Duplicate signal_id -> 200 duplicate (idempotent, never double-orders)
    r_dup = client.post("/webhook", json=valid)
    assert r_dup.status_code == 200
    assert r_dup.json()["status"] == "duplicate"

    # 3. Wrong secret -> 401
    wrong_sec = build_payload(secret="wrong-secret", signal_id="sig-wrong-sec")
    r_sec = client.post("/webhook", json=wrong_sec)
    assert r_sec.status_code == 401

    # 4. Stale payload (> 60s old) -> 400
    old_ts = (datetime.now(UTC) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = build_payload(secret=sec, signal_id="sig-stale", ts_iso=old_ts)
    r_stale = client.post("/webhook", json=stale)
    assert r_stale.status_code == 400
    assert "stale" in r_stale.json()["detail"]

    # 5. Malformed JSON -> 400
    r_mal = client.post("/webhook", content=b'{"secret":"test", trailing_comma: }', headers={"Content-Type": "application/json"})
    assert r_mal.status_code == 400

    # 6. Naked entry (missing stop on buy) -> 422
    no_stop = build_payload(secret=sec, action="buy", stop=None, signal_id="sig-no-stop")
    r_nostop = client.post("/webhook", json=no_stop)
    assert r_nostop.status_code == 422

    # 7. Unknown extra field -> 422 (extra="forbid")
    extra = build_payload(secret=sec, signal_id="sig-extra")
    extra["unexpected_field"] = "bad"
    r_extra = client.post("/webhook", json=extra)
    assert r_extra.status_code == 422

    # 8. Raw JSONL file recorded every attempt BEFORE processing, with secrets redacted
    raw_files = list(test_settings.raw_dir.glob("webhooks-*.jsonl"))
    assert len(raw_files) == 1
    lines = raw_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 7
    assert sec not in raw_files[0].read_text(encoding="utf-8")
