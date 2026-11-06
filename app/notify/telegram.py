"""Telegram Bot notification sender with in-memory + log audit so alerts are observable in tests
and never crash trading when Telegram is unconfigured or unreachable."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.sent_messages: list[dict[str, Any]] = []

    def send(self, event_type: str, message: str) -> bool:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "message": message,
        }
        self.sent_messages.append(entry)
        logger.info("ALERT [%s]: %s", event_type, message)

        token = self.s.telegram_bot_token.get_secret_value()
        chat_id = self.s.telegram_chat_id
        if not token or not chat_id:
            return False
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            with httpx.Client(timeout=5.0) as client:
                r = client.post(url, json={"chat_id": chat_id, "text": f"[{event_type.upper()}] {message}"})
                return r.status_code == 200
        except Exception as e:
            logger.warning("Telegram delivery failed: %s", e)
            return False


_NOTIFIER: Notifier | None = None


def get_notifier(settings: Settings | None = None) -> Notifier:
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = Notifier(settings)
    return _NOTIFIER
