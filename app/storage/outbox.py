"""Local SQLite outbox queue used when the primary PostgreSQL database is unreachable.

Guarantees:
- Idempotent enqueue on `signal_id` (duplicate signals while DB is down are ignored).
- Preserves strict arrival order (`id ASC`) on replay when primary DB recovers.
- Also supports queuing pending embedding jobs when Ollama / embedding service is down.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class OutboxStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    replayed_at TEXT,
                    last_error TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    journal_id TEXT NOT NULL UNIQUE,
                    text_to_embed TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )

    def enqueue_signal(self, signal_id: str, payload: dict[str, Any]) -> bool:
        """Returns True if inserted, False if duplicate signal_id already in outbox."""
        now = datetime.now(UTC).isoformat()
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO signal_outbox (signal_id, payload_json, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (signal_id, json.dumps(payload), now),
            )
            return cur.rowcount == 1

    def pending_signals(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, signal_id, payload_json, created_at FROM signal_outbox WHERE status='pending' ORDER BY id ASC"
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "signal_id": r["signal_id"],
                    "payload": json.loads(r["payload_json"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def mark_signal_replayed(self, row_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE signal_outbox SET status='replayed', replayed_at=? WHERE id=?",
                (now, row_id),
            )

    def pending_count(self) -> int:
        with self._conn() as c:
            r = c.execute("SELECT COUNT(*) AS n FROM signal_outbox WHERE status='pending'").fetchone()
            return int(r["n"]) if r else 0

    # --- Embedding fallback queue ---
    def enqueue_embedding(self, journal_id: str, text_to_embed: str, metadata: dict[str, Any]) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT OR REPLACE INTO embedding_outbox (journal_id, text_to_embed, metadata_json, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (journal_id, text_to_embed, json.dumps(metadata), now),
            )
            return cur.rowcount >= 1

    def pending_embeddings(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, journal_id, text_to_embed, metadata_json FROM embedding_outbox WHERE status='pending' ORDER BY id ASC"
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "journal_id": r["journal_id"],
                    "text": r["text_to_embed"],
                    "metadata": json.loads(r["metadata_json"]),
                }
                for r in rows
            ]

    def mark_embedding_done(self, row_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE embedding_outbox SET status='done', completed_at=? WHERE id=?",
                (now, row_id),
            )
