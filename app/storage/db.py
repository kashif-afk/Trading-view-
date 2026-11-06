"""Database engine, session factory, health probe, and pgvector DDL helper."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.storage.models import Base, ConfigVersion

_ENGINE_CACHE: dict[str, Engine] = {}


def get_engine(settings: Settings | None = None) -> Engine:
    s = settings or get_settings()
    url = s.database_url
    if url not in _ENGINE_CACHE:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _ENGINE_CACHE[url] = create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)
    return _ENGINE_CACHE[url]


def init_db(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    eng = get_engine(s)
    if eng.dialect.name == "postgresql":
        with eng.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(eng)
    if eng.dialect.name == "postgresql":
        with eng.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE lessons ADD COLUMN IF NOT EXISTS embedding vector({s.embed_dim})"
                )
            )
    ensure_config_version(s)


def ensure_config_version(settings: Settings | None = None) -> int:
    s = settings or get_settings()
    rh = s.risk_hash()
    with session_scope(s) as db:
        cv = db.query(ConfigVersion).filter_by(risk_hash=rh).one_or_none()
        if not cv:
            cv = ConfigVersion(
                risk_hash=rh,
                strategy_version=s.strategy_version,
                params=s.risk_fingerprint(),
                approved_by="config",
            )
            db.add(cv)
            db.flush()
        return cv.id


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    eng = get_engine(settings)
    factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_healthy(settings: Settings | None = None) -> bool:
    try:
        eng = get_engine(settings)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
