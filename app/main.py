"""FastAPI application entrypoint (`app.main:app`)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request

from app.api.dashboard import router as dashboard_router
from app.api.webhook import router as webhook_router
from app.brokers.oanda import get_broker
from app.core.config import Settings, assert_mode_allowed, get_settings
from app.core.security import RateLimiter, setup_logging
from app.core.state import CircuitBreaker, KillSwitch
from app.engine.reconciler import reconcile_state
from app.memory.embeddings import EmbeddingService
from app.storage.db import db_healthy, init_db
from app.storage.outbox import OutboxStore
from app.storage.raw_log import RawWebhookLogger


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    s = settings or get_settings()
    setup_logging()
    assert_mode_allowed(s)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(s)
        try:
            reconcile_state(s, app.state.broker)
        except Exception:
            pass
        sched: BackgroundScheduler | None = None
        if start_scheduler:
            sched = BackgroundScheduler(timezone="UTC")
            sched.add_job(
                lambda: reconcile_state(s, app.state.broker),
                "interval",
                seconds=s.reconcile_interval_sec,
                id="reconciler",
                replace_existing=True,
            )
            sched.start()
        try:
            yield
        finally:
            if sched:
                sched.shutdown(wait=False)

    app = FastAPI(title="XAUUSD Automated Execution & Cash-Flow Engine", version="0.1.0", lifespan=lifespan)
    app.state.settings = s
    app.state.rate_limiter = RateLimiter(per_min=s.rate_limit_per_min, burst=s.rate_limit_burst)
    app.state.raw_logger = RawWebhookLogger(s.raw_dir, retention_days=s.raw_log_retention_days)
    app.state.broker = get_broker(s)
    app.state.embedder = EmbeddingService(s, OutboxStore(s.outbox_path))
    app.state.force_db_down = False

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        cfg: Settings = request.app.state.settings
        db_ok = (not request.app.state.force_db_down) and db_healthy(cfg)
        ks = KillSwitch(cfg.state_dir)
        cb = CircuitBreaker(cfg.state_dir, cfg.max_daily_loss_pct)
        outbox_pending = OutboxStore(cfg.outbox_path).pending_count()
        return {
            "status": "ok" if db_ok else "degraded_outbox_only",
            "mode": cfg.execution_mode,
            "broker": cfg.broker,
            "db_healthy": db_ok,
            "outbox_pending": outbox_pending,
            "kill_switch": ks.active(),
            "circuit_breaker": cb.status().get("tripped", False),
        }

    app.include_router(webhook_router)
    app.include_router(dashboard_router)
    return app


app = create_app(start_scheduler=False)
