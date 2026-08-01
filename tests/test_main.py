from __future__ import annotations

import unittest

from app.auth import PUBLIC_PATHS
from app.main import _is_expected_client_disconnect


class HttpLifecycleTests(unittest.TestCase):
    def test_windows_browser_reset_is_expected(self) -> None:
        error = ConnectionResetError("browser closed socket")
        error.winerror = 10054  # type: ignore[attr-defined]
        self.assertTrue(_is_expected_client_disconnect({"exception": error}))

    def test_other_asyncio_errors_are_not_hidden(self) -> None:
        error = ConnectionResetError("different socket failure")
        error.winerror = 10053  # type: ignore[attr-defined]
        self.assertFalse(_is_expected_client_disconnect({"exception": error}))
        self.assertFalse(_is_expected_client_disconnect({"exception": RuntimeError("broken task")}))

    def test_favicon_is_public(self) -> None:
        self.assertIn("/favicon.ico", PUBLIC_PATHS)


if __name__ == "__main__":
    unittest.main()
