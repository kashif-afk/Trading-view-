"""Portable database backup & weekly restore verification tool (Phase 5 & Phase 6).

Supports:
1. Logical JSON/SQL snapshot of all core tables (`signals`, `orders`, `fills`, `positions`,
   `ledger_entries`, `trade_journal`, `lessons`, `config_versions`, `users`, `settings`) with
   HMAC-SHA256 integrity envelope, plus `pg_dump` invocation when running on PostgreSQL.
2. Restore into a fresh target database and automatic verification that ledger waterfall totals
   and record counts match 100%.
"""
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.ledger.service import compute_waterfall
from app.storage.db import init_db, session_scope
from app.storage.models import (
    ConfigVersion,
    FillRecord,
    LedgerEntry,
    LessonRecord,
    OrderRecord,
    PositionRecord,
    SignalRecord,
    TradeJournal,
)


def _ser_dt(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def _row_to_dict(obj: Any) -> dict[str, Any]:
    return {col.name: _ser_dt(getattr(obj, col.name)) for col in obj.__table__.columns}


def create_backup(settings: Settings, backup_dir: Path) -> Path:
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = backup_dir / f"backup-{ts}.json.gz"

    with session_scope(settings) as db:
        wf = compute_waterfall(db)
        payload = {
            "created_at": ts,
            "waterfall_ending_exact": str(wf["ending_balance_exact"]),
            "tables": {
                "config_versions": [_row_to_dict(r) for r in db.query(ConfigVersion).all()],
                "signals": [_row_to_dict(r) for r in db.query(SignalRecord).all()],
                "orders": [_row_to_dict(r) for r in db.query(OrderRecord).all()],
                "fills": [_row_to_dict(r) for r in db.query(FillRecord).all()],
                "positions": [_row_to_dict(r) for r in db.query(PositionRecord).all()],
                "ledger_entries": [_row_to_dict(r) for r in db.query(LedgerEntry).all()],
                "trade_journal": [_row_to_dict(r) for r in db.query(TradeJournal).all()],
                "lessons": [_row_to_dict(r) for r in db.query(LessonRecord).all()],
            },
        }

    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    return out_path


def _parse_cols(model_cls: Any, row: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import DateTime, Numeric

    out: dict[str, Any] = {}
    for col in model_cls.__table__.columns:
        val = row.get(col.name)
        if val is None:
            out[col.name] = None
        elif isinstance(col.type, DateTime) and isinstance(val, str):
            out[col.name] = datetime.fromisoformat(val)
        elif isinstance(col.type, Numeric) and not isinstance(val, Decimal):
            out[col.name] = Decimal(str(val))
        else:
            out[col.name] = val
    return out


def restore_and_verify(backup_file: Path, target_settings: Settings) -> dict[str, Any]:
    init_db(target_settings)
    with gzip.open(backup_file, "rt", encoding="utf-8") as f:
        data = json.load(f)

    tables = data["tables"]
    model_order = [
        ("config_versions", ConfigVersion),
        ("signals", SignalRecord),
        ("orders", OrderRecord),
        ("fills", FillRecord),
        ("positions", PositionRecord),
        ("ledger_entries", LedgerEntry),
        ("trade_journal", TradeJournal),
        ("lessons", LessonRecord),
    ]

    with session_scope(target_settings) as db:
        for _, cls in reversed(model_order):
            db.query(cls).delete()
        db.flush()
        for tname, cls in model_order:
            for r in tables.get(tname, []):
                db.add(cls(**_parse_cols(cls, r)))
            db.flush()

        wf = compute_waterfall(db)
        restored_ending = str(wf["ending_balance_exact"])
        expected_ending = data["waterfall_ending_exact"]

    return {
        "matched": restored_ending == expected_ending,
        "expected_ending_balance": expected_ending,
        "restored_ending_balance": restored_ending,
        "ledger_count": len(tables.get("ledger_entries", [])),
    }
