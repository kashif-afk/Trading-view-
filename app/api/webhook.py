"""POST /webhook endpoint (Phase 3).

Security & Reliability:
1. Appends raw request bytes to the daily rotated JSONL log BEFORE parsing.
2. Enforces token-bucket rate limit (per IP + global).
3. Parses & validates strict Pydantic schema (`WebhookPayload`).
4. Verifies shared secret with constant-time `hmac.compare_digest`.
5. Rejects stale payloads (> 60s old) or future-skewed timestamps.
6. Processes idempotently on `signal_id` through the risk gate, sizer, and broker adapter.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import freshness_error, secret_ok
from app.engine.pipeline import WebhookPayload, process_webhook_signal
from app.storage.raw_log import RawWebhookLogger

router = APIRouter()


@router.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, Any]:
    s: Settings = request.app.state.settings
    limiter = request.app.state.rate_limiter
    raw_logger: RawWebhookLogger = request.app.state.raw_logger

    client_ip = request.client.host if request.client else "unknown"
    raw_bytes = await request.body()
    content_type = request.headers.get("content-type", "")

    # 1. Always write append-only raw payload BEFORE processing
    raw_logger.append(raw_bytes, client_ip=client_ip, content_type=content_type)

    # 2. Optional TradingView IP allowlist
    if s.tv_ip_allowlist_enabled:
        allowed_ips = {ip.strip() for ip in s.tv_ips.split(",") if ip.strip()}
        if client_ip not in allowed_ips:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not in TradingView allowlist")

    # 3. Rate limit check
    if not limiter.allow(client_ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    # 4. Parse JSON & validate schema
    try:
        raw_obj = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(raw_obj, dict):
            raise ValueError("Payload must be a JSON object")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Malformed JSON: {e}") from e

    # 5. Constant-time shared secret compare BEFORE detailed schema feedback
    expected_secret = s.webhook_secret.get_secret_value()
    provided_secret = str(raw_obj.get("secret", "")) if isinstance(raw_obj.get("secret"), str) else None
    if not secret_ok(provided_secret, expected_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")

    try:
        payload = WebhookPayload.model_validate(raw_obj)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors()) from e

    # 6. Freshness check (reject stale > 60s)
    fresh_err = freshness_error(payload.time, s.freshness_sec, s.future_skew_sec)
    if fresh_err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=fresh_err)

    # 7. Process idempotently through pipeline
    result = process_webhook_signal(
        payload,
        settings=s,
        broker=getattr(request.app.state, "broker", None),
        embedder=getattr(request.app.state, "embedder", None),
        force_db_down=getattr(request.app.state, "force_db_down", False),
    )
    return result
