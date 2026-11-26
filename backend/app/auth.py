"""Optional token gate for the API and the event streams.

Off by default: the service is meant for one operator on ``127.0.0.1``, and a
demo should not need a login. Set ``TRANSCRIBE_TOKEN`` and every API call plus
every WebSocket handshake needs a cookie that only that token can mint.

Why a cookie and not a header
-----------------------------
A WebSocket cannot send an ``Authorization`` header from the browser, and a token
in the query string ends up in access logs and browser history. Exchanging the
token once for an HttpOnly, SameSite=Strict cookie keeps it out of both, and the
event stream authenticates itself with the same cookie.

The cookie value is an HMAC of the token, not the token itself: rotating
``TRANSCRIBE_TOKEN`` invalidates every existing session without a store.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Final

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE_NAME: Final = "transcribe_session"
#: Endpoints that must answer without a token: the login itself, a liveness
#: probe, the interactive docs and the local test double.
PUBLIC_PATHS: Final = ("/api/auth", "/api/health", "/docs", "/redoc", "/openapi.json")
PUBLIC_PREFIXES: Final = ("/assets/", "/fake-meeting", "/fixtures")
#: How long a browser stays logged in after presenting the token.
SESSION_MAX_AGE_S: Final = 12 * 3600


class AuthRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)


def configured_token() -> str | None:
    """The token from the environment, or ``None`` when the gate is disabled."""
    value = os.environ.get("TRANSCRIBE_TOKEN", "").strip()
    return value or None


def auth_enabled() -> bool:
    return configured_token() is not None


def cookie_for(token: str) -> str:
    """The value a browser must present; changing the token changes it."""
    return hmac.new(token.encode("utf-8"), b"authorised", hashlib.sha256).hexdigest()


def is_authorised(request: Request) -> bool:
    """True when the gate is off, or the request carries a valid cookie."""
    token = configured_token()
    if token is None:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), cookie_for(token))


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def requires_auth(path: str) -> bool:
    """Only our own API and streams are gated; static files must load to log in."""
    if is_public(path):
        return False
    return path.startswith("/api/") or path.startswith("/events/")


class TokenMiddleware(BaseHTTPMiddleware):
    """Rejects unauthorised API calls when a token is configured."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        if requires_auth(request.url.path) and not is_authorised(request):
            return JSONResponse({"detail": "нужен токен доступа"}, status_code=401)
        return await call_next(request)


def login(payload: AuthRequest, response: Response) -> dict:
    """Exchange the token for a cookie; wrong token is a plain 401."""
    token = configured_token()
    if token is None:
        return {"required": False, "authorised": True}
    if not hmac.compare_digest(payload.token.strip(), token):
        raise HTTPException(status_code=401, detail="неверный токен")
    response.set_cookie(
        COOKIE_NAME,
        cookie_for(token),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"required": True, "authorised": True}


def status(request: Request) -> dict:
    """What the interface needs to decide whether to ask for a token."""
    return {"required": auth_enabled(), "authorised": is_authorised(request)}
