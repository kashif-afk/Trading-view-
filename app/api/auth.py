"""Single-user authentication with Argon2id password hashing, optional TOTP (RFC 6238),
and signed session cookies. The dashboard is NEVER exposed without authentication."""
from __future__ import annotations

import os
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.storage.models import User

_ph = PasswordHasher()


def ensure_default_admin(db: Session) -> User:
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "changeme123!")
    totp_secret = os.environ.get("ADMIN_TOTP_SECRET") or None
    u = db.query(User).filter_by(username=username).one_or_none()
    if u is None:
        u = User(
            username=username,
            password_hash=_ph.hash(password),
            totp_secret=totp_secret,
            totp_enabled=bool(totp_secret),
        )
        db.add(u)
        db.flush()
    return u


def verify_credentials(db: Session, username: str, password: str, totp_code: str | None = None) -> User | None:
    ensure_default_admin(db)
    u = db.query(User).filter_by(username=username).one_or_none()
    if not u:
        return None
    try:
        _ph.verify(u.password_hash, password)
    except VerifyMismatchError:
        return None
    if u.totp_enabled and u.totp_secret:
        if not totp_code or not pyotp.TOTP(u.totp_secret).verify(totp_code.strip(), valid_window=1):
            return None
    return u


def _serializer(settings: Settings | None = None) -> URLSafeTimedSerializer:
    s = settings or get_settings()
    return URLSafeTimedSerializer(s.session_secret.get_secret_value(), salt="dashboard-session")


def create_session_token(username: str, settings: Settings | None = None) -> str:
    return _serializer(settings).dumps({"u": username})


def read_session_token(token: str | None, settings: Settings | None = None, max_age: int = 86400) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        return _serializer(settings).loads(token, max_age=max_age)
    except BadSignature:
        return None


def require_user(request: Request) -> str:
    s: Settings = request.app.state.settings
    token = request.cookies.get("session_token")
    data = read_session_token(token, s)
    if not data or "u" not in data:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return str(data["u"])
