from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .config import Settings


COOKIE_NAME = "sigit_cam_session"
CSRF_HEADER = "X-CSRF-Token"
PUBLIC_PATHS = {"/login", "/favicon.ico", "/static/login.css"}


def session_token(settings: Settings) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        b"sigit-cam-authenticated-v1",
        hashlib.sha256,
    ).hexdigest()


def csrf_token(settings: Settings) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        b"sigit-csrf-v1",
        hashlib.sha256,
    ).hexdigest()


def is_authenticated(request: Request, settings: Settings) -> bool:
    supplied = request.cookies.get(COOKIE_NAME, "")
    expected = session_token(settings)
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def safe_next_path(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


class PasswordAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        is_public = path in PUBLIC_PATHS
        if not is_public and not is_authenticated(request, self.settings):
            accepts_html = "text/html" in request.headers.get("accept", "")
            if request.method == "GET" and (path in {"/", "/highlights"} or accepts_html):
                query = urlencode({"next": request.url.path})
                return RedirectResponse(f"/login?{query}", status_code=303)
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        if not is_public and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get(CSRF_HEADER, "")
            if not supplied or not hmac.compare_digest(supplied, csrf_token(self.settings)):
                return JSONResponse({"detail": "Invalid CSRF token"}, status_code=403)

        response = await call_next(request)
        if not is_public:
            response.headers["Cache-Control"] = "no-store"
        return response


class LoginAttemptLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def _recent(self, client: str) -> list[float]:
        cutoff = time.monotonic() - self.window_seconds
        recent = [stamp for stamp in self._attempts.get(client, []) if stamp >= cutoff]
        self._attempts[client] = recent
        return recent

    def is_limited(self, client: str) -> bool:
        return len(self._recent(client)) >= self.max_attempts

    def record_failure(self, client: str) -> None:
        self._recent(client).append(time.monotonic())

    def clear(self, client: str) -> None:
        self._attempts.pop(client, None)
