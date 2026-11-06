"""Append-only raw webhook JSONL log (written BEFORE any parsing/validation) with daily rotation
and 90-day retention."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.security import redact


class RawWebhookLogger:
    def __init__(self, raw_dir: Path, retention_days: int = 90):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days

    def _today_file(self, now: datetime | None = None) -> Path:
        dt = now or datetime.now(timezone.utc)
        return self.raw_dir / f"webhooks-{dt.strftime('%Y-%m-%d')}.jsonl"

    def append(self, raw_bytes: bytes, client_ip: str, content_type: str, now: datetime | None = None) -> Path:
        dt = now or datetime.now(timezone.utc)
        path = self._today_file(dt)
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        record: dict[str, Any] = {
            "received_at": dt.isoformat(),
            "client_ip": client_ip,
            "content_type": content_type,
            "raw_redacted": redact(raw_text),
            "sha256": __import__("hashlib").sha256(raw_bytes).hexdigest(),
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return path

    def prune_old(self, now: datetime | None = None) -> int:
        dt = now or datetime.now(timezone.utc)
        cutoff = (dt - timedelta(days=self.retention_days)).date()
        removed = 0
        for p in self.raw_dir.glob("webhooks-*.jsonl"):
            try:
                day_str = p.stem.replace("webhooks-", "")
                file_date = datetime.strptime(day_str, "%Y-%m-%d").date()
                if file_date < cutoff:
                    p.unlink()
                    removed += 1
            except ValueError:
                continue
        return removed
