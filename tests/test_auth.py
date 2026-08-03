from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from starlette.requests import Request
from starlette.responses import Response

from app.auth import (
    COOKIE_NAME,
    LoginAttemptLimiter,
    PasswordAuthMiddleware,
    csrf_token,
    safe_next_path,
    session_token,
)
from app.config import Settings


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = replace(
            Settings(),
            app_password="correct horse battery staple",
            session_secret="a" * 32,
        )

    def test_session_token_is_stable_and_changes_with_secret(self) -> None:
        token = session_token(self.settings)
        self.assertEqual(token, session_token(self.settings))
        changed = replace(self.settings, session_secret="b" * 32)
        self.assertNotEqual(token, session_token(changed))

    def test_next_path_rejects_external_redirects(self) -> None:
        self.assertEqual(safe_next_path("/highlights"), "/highlights")
        self.assertEqual(safe_next_path("//example.com"), "/")
        self.assertEqual(safe_next_path("https://example.com"), "/")

    def test_login_attempt_limiter_resets_after_success(self) -> None:
        limiter = LoginAttemptLimiter(max_attempts=2)
        limiter.record_failure("client")
        limiter.record_failure("client")
        self.assertTrue(limiter.is_limited("client"))
        limiter.clear("client")
        self.assertFalse(limiter.is_limited("client"))

    def test_authenticated_mutations_require_csrf_header(self) -> None:
        middleware = PasswordAuthMiddleware(lambda *_: None, settings=self.settings)

        async def request_with(headers: list[tuple[bytes, bytes]]) -> Response:
            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/api/replays",
                    "raw_path": b"/api/replays",
                    "query_string": b"",
                    "headers": headers,
                    "client": ("127.0.0.1", 1),
                    "server": ("127.0.0.1", 8000),
                }
            )

            async def call_next(_: Request) -> Response:
                return Response(status_code=204)

            return await middleware.dispatch(request, call_next)

        cookie = f"{COOKIE_NAME}={session_token(self.settings)}".encode()
        denied = asyncio.run(request_with([(b"cookie", cookie)]))
        allowed = asyncio.run(
            request_with([(b"cookie", cookie), (b"x-csrf-token", csrf_token(self.settings).encode())])
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 204)


if __name__ == "__main__":
    unittest.main()
