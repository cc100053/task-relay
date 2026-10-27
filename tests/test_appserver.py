import json
import os
import sys
import time
import unittest
from pathlib import Path

from codex_quota_watchdog.appserver import AppServerClient, ProbeError

from .helpers import rate_limits

FAKE = str(Path(__file__).with_name("fake_app_server.py"))


class AppServerTests(unittest.TestCase):
    def client(self, mode="ok", timeout=10):
        os.environ["FAKE_MODE"] = mode
        os.environ["FAKE_RATE_LIMITS"] = json.dumps(rate_limits(time.time()))
        return AppServerClient(sys.executable, [FAKE], timeout=timeout)

    def tearDown(self):
        os.environ.pop("FAKE_MODE", None)
        os.environ.pop("FAKE_RATE_LIMITS", None)

    def test_reads_rate_limits(self):
        result = self.client().read_rate_limits()
        self.assertEqual(result["rateLimits"]["primary"]["windowDurationMins"], 300)

    def test_legacy_cli_retries_without_params(self):
        result = self.client("legacy").read_rate_limits()
        self.assertEqual(result["rateLimits"]["primary"]["windowDurationMins"], 300)

    def test_error_reply(self):
        with self.assertRaisesRegex(ProbeError, "boom"):
            self.client("error").read_rate_limits()

    def test_early_exit(self):
        with self.assertRaisesRegex(ProbeError, "exited"):
            self.client("exit").read_rate_limits()

    def test_timeout(self):
        start = time.monotonic()
        with self.assertRaisesRegex(ProbeError, "in time"):
            self.client("hang", timeout=1.5).read_rate_limits()
        self.assertLess(time.monotonic() - start, 10)

    def test_missing_binary(self):
        with self.assertRaises(ProbeError):
            AppServerClient("/nonexistent/codex", ["app-server"]).read_rate_limits()


if __name__ == "__main__":
    unittest.main()
