import os
import sys
import unittest
from datetime import time
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("ADMIN_TOKEN_SECRET", "test-secret-for-scanner-schedule")

from app import main  # noqa: E402


class ScannerPasswordFreeScheduleTests(unittest.TestCase):
    def test_default_window_is_start_inclusive_and_end_exclusive(self):
        with patch.object(main, "SCANNER_PASSWORD_FREE_WINDOWS", "07:00-09:00"):
            self.assertFalse(main.scanner_password_free_now(time(6, 59, 59)))
            self.assertTrue(main.scanner_password_free_now(time(7, 0)))
            self.assertTrue(main.scanner_password_free_now(time(8, 59, 59)))
            self.assertFalse(main.scanner_password_free_now(time(9, 0)))

    def test_empty_window_disables_password_free_scanning(self):
        with patch.object(main, "SCANNER_PASSWORD_FREE_WINDOWS", ""):
            self.assertFalse(main.scanner_password_free_now(time(8, 0)))

    def test_multiple_and_overnight_windows_are_supported(self):
        with patch.object(main, "SCANNER_PASSWORD_FREE_WINDOWS", "07:00-09:00,23:00-01:00"):
            self.assertTrue(main.scanner_password_free_now(time(23, 30)))
            self.assertTrue(main.scanner_password_free_now(time(0, 30)))
            self.assertFalse(main.scanner_password_free_now(time(1, 0)))


if __name__ == "__main__":
    unittest.main()
