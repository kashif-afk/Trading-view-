"""Webhook security helpers: constant-time secret compare, freshness, token-bucket rate limit,
and log redaction so secrets never reach logs."""
from __future__ import annotations

import hmac
import logging
import re
import threading
import time
from datetime import UTC, datetime


def secret_ok(provided: str | None, expected: str) -> bool:
    if not expected or provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def parse_ts(value: str) -> datetime:
    v = value.strip()
    if v.isdigit():  # epoch ms or s
        n = int(v)
        return datetime.fromtimestamp(n / 1000 if n > 10**11 else n, tz=UTC)
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def freshness_error(ts: str, max_age_sec: int, future_skew_sec: int, now: datetime | None = None) -> str | None:
    try:
        sent = parse_ts(ts)
    except (ValueError, OverflowError):
        return "invalid time"
    now = now or datetime.now(UTC)
    age = (now - sent).total_seconds()
    if age > max_age_sec:
        return f"stale: {age:.0f}s old > {max_age_sec}s"
    if age < -future_skew_sec:
        return f"time in future by {-age:.0f}s"
    return None


class RateLimiter:
    """Token bucket per key (client IP) plus a global bucket."""

    def __init__(self, per_min: int, burst: int):
        self.rate = per_min / 60.0
        self.burst = burst
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            ok_key = self._take(key, now)
            return ok_key and self._take("__global__", now, burst=self.burst * 2)

    def _take(self, key: str, now: float, burst: int | None = None) -> bool:
        cap = burst or self.burst
        tokens, last = self._b.get(key, (float(cap), now))
        tokens = min(cap, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._b[key] = (tokens, now)
            return False
        self._b[key] = (tokens - 1, now)
        return True


_SECRET_RE = re.compile(r'("?(secret|api_key|token|password)"?\s*[:=]\s*"?)([^",\s}]+)', re.IGNORECASE)


def redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def setup_logging(level: str = "INFO") -> None:
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler("logs/app.log", maxBytes=10_000_000, backupCount=10)
        sh = logging.StreamHandler()
        for h in (fh, sh):
            h.setFormatter(fmt)
            h.addFilter(RedactingFilter())
            root.addHandler(h)
