"""
PREDICT — optional single-admin authentication.

When ADMIN_PASSWORD is set, the dashboard + REST API + WebSocket require a
valid session cookie. When it's empty, auth is disabled (local dev / no auth).

Sessions are HMAC-signed tokens (stdlib only — no new dependencies).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from server.config import settings

COOKIE_NAME = "pdm_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

# Stable secret for signing session cookies. If AUTH_SECRET is unset, a secret
# is generated once and persisted to a file so sessions survive restarts.
_secret: Optional[bytes] = None


def _get_secret() -> bytes:
    global _secret
    if _secret is not None:
        return _secret
    if settings.AUTH_SECRET:
        _secret = settings.AUTH_SECRET.encode()
        return _secret
    path = Path(os.environ.get("AUTH_SECRET_FILE", "auth_secret"))
    try:
        if path.exists():
            raw = path.read_text().strip()
            if raw:
                _secret = raw.encode()
                return _secret
        raw = secrets.token_hex(32)
        path.write_text(raw)
        _secret = raw.encode()
        return _secret
    except Exception:
        _secret = secrets.token_bytes(32)
        return _secret


def auth_enabled() -> bool:
    """Auth is active only when an admin password is configured."""
    return bool(settings.ADMIN_PASSWORD)


def _sign(payload: str) -> str:
    mac = hmac.new(_get_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def _verify(payload: str, sig: str) -> bool:
    expected = hmac.new(_get_secret(), payload.encode(), hashlib.sha256).digest()
    try:
        actual = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    except Exception:
        return False
    return hmac.compare_digest(expected, actual)


def create_session_token() -> str:
    """Return a signed token: <expiry>.<nonce>.<signature>"""
    exp = int(time.time()) + SESSION_TTL_SECONDS
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def validate_token(token: str) -> bool:
    try:
        payload, sig = token.rsplit(".", 1)
        exp_str, _ = payload.split(".", 1)
        exp = int(exp_str)
    except Exception:
        return False
    if exp < time.time():
        return False
    return _verify(payload, sig)


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    token = request.cookies.get(COOKIE_NAME)
    return bool(token and validate_token(token))


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,  # plain HTTP for now (IP access); set True when HTTPS lands
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def require_auth(request: Request) -> None:
    """FastAPI dependency: 401 when auth is enabled and not authenticated."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ── API router (open — login/logout/me must not require auth) ───────────────
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class AuthState(BaseModel):
    authenticated: bool


@router.post("/login", response_model=AuthState)
async def login(body: LoginRequest, response: Response):
    if not auth_enabled():
        return AuthState(authenticated=True)
    if not hmac.compare_digest(body.password, settings.ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Invalid password")
    set_session_cookie(response, create_session_token())
    return AuthState(authenticated=True)


@router.post("/logout", response_model=AuthState)
async def logout(response: Response):
    clear_session_cookie(response)
    return AuthState(authenticated=False)


@router.get("/me", response_model=AuthState)
async def me(request: Request):
    return AuthState(authenticated=is_authenticated(request))