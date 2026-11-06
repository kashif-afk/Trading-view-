"""File-backed safety state: kill switch + circuit breaker.

Deliberately NOT in the database so they keep working when Postgres, the LLM, or the dashboard is down.
Logic mirrors the `drawdown-circuit-breaker` skill (daily-loss trip, manual reset) and the kill-switch
requirement (cancel all, flatten, block until manual reset).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KillSwitch:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "KILL_SWITCH.json"

    def active(self) -> bool:
        return self.path.exists()

    def info(self) -> dict[str, Any] | None:
        return _read(self.path)

    def engage(self, reason: str, by: str) -> None:
        if not self.active():
            _atomic_write(self.path, {"reason": reason, "by": by, "at": utcnow().isoformat()})

    def reset(self, by: str) -> None:
        if self.path.exists():
            hist = self.path.with_name("KILL_SWITCH.history.jsonl")
            with hist.open("a") as f:
                f.write(json.dumps({**(self.info() or {}), "reset_by": by, "reset_at": utcnow().isoformat()}) + "\n")
            self.path.unlink()


class CircuitBreaker:
    """Tracks day-start equity (UTC day) and trips when daily drawdown >= max_daily_loss_pct.
    Once tripped it stays tripped for the rest of the UTC day (exits still allowed)."""

    def __init__(self, state_dir: Path, max_daily_loss_pct: Decimal):
        self.path = Path(state_dir) / "breaker.json"
        self.max_loss = Decimal(str(max_daily_loss_pct))

    def _state(self, equity: Decimal | None = None) -> dict[str, Any]:
        st = _read(self.path) or {}
        today = utcnow().date().isoformat()
        if st.get("day") != today:
            if equity is None:
                return {"day": today, "start_equity": None, "tripped": False, "reason": None}
            st = {"day": today, "start_equity": str(equity), "tripped": False, "reason": None}
            _atomic_write(self.path, st)
        return st

    def daily_pnl_pct(self, equity: Decimal) -> Decimal:
        st = self._state(equity)
        start = Decimal(st["start_equity"])
        if start <= 0:
            return Decimal(0)
        return (equity - start) / start * 100

    def update(self, equity: Decimal) -> tuple[bool, str | None]:
        """Returns (tripped, reason). Trips (persistently for the day) at the loss threshold."""
        st = self._state(equity)
        if st.get("tripped"):
            return True, st.get("reason")
        pnl = self.daily_pnl_pct(equity)
        if pnl <= -self.max_loss:
            st["tripped"] = True
            st["reason"] = f"daily loss {abs(pnl):.2f}% >= {self.max_loss}%"
            st["tripped_at"] = utcnow().isoformat()
            _atomic_write(self.path, st)
            return True, st["reason"]
        return False, None

    def status(self) -> dict[str, Any]:
        return self._state()

    def reset(self) -> None:
        st = self._state()
        st.update({"tripped": False, "reason": None, "reset_at": utcnow().isoformat()})
        _atomic_write(self.path, st)
