from __future__ import annotations

import unittest
from dataclasses import replace

from app.auth import LoginAttemptLimiter, safe_next_path, session_token
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


if __name__ == "__main__":
    unittest.main()
