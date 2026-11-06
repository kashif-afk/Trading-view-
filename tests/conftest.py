from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.main import create_app
from app.storage.db import init_db


def valid_session_iso() -> str:
    """Returns a fresh timestamp if currently inside 07:00-18:00 UTC Mon-Thu,
    or a valid session timestamp when testing risk gate directly."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def test_settings(tmp_path: Path) -> Settings:
    db_file = tmp_path / "test_app.db"
    s = Settings(
        execution_mode="paper",
        broker="paper",
        webhook_secret="test-secret-123",
        session_secret="test-session-secret-456",
        database_url=f"sqlite:///{db_file.as_posix()}",
        data_dir=tmp_path / "data",
        account_size=Decimal(2000),
        risk_per_trade_pct=Decimal("0.5"),
        max_daily_loss_pct=Decimal("2.0"),
        max_open_positions=3,
        session_start_utc="00:00",
        session_end_utc="23:59",
        friday_no_entry_after_utc="23:59",
        rate_limit_per_min=120,
        rate_limit_burst=60,
        embed_provider="hash",
    )
    for d in (s.data_dir, s.raw_dir, s.state_dir):
        d.mkdir(parents=True, exist_ok=True)
    init_db(s)
    return s


@pytest.fixture()
def paper_broker(test_settings: Settings) -> PaperBroker:
    return PaperBroker(test_settings)


@pytest.fixture()
def client(test_settings: Settings, paper_broker: PaperBroker) -> TestClient:
    app = create_app(settings=test_settings, start_scheduler=False)
    app.state.broker = paper_broker
    return TestClient(app)
