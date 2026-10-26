import unittest

from codex_quota_watchdog import quota
from codex_quota_watchdog.config import Config

from .helpers import rate_limits

NOW = 1_800_000_000.0


class ParseTests(unittest.TestCase):
    def test_parses_windows_and_kinds(self):
        snap = quota.parse_snapshot(rate_limits(NOW, five_used=40), NOW)
        self.assertEqual([w.kind for w in snap.windows], ["5h", "weekly"])
        self.assertEqual(snap.windows[0].remaining, 60)
        self.assertTrue(snap.ordinary_field_present)
        self.assertEqual(snap.reset_credits_available, 1)

    def test_prefers_codex_bucket(self):
        raw = rate_limits(NOW)
        raw["rateLimitsByLimitId"] = {"codex": {"primary": {
            "usedPercent": 99, "windowDurationMins": 300, "resetsAt": int(NOW + 60)}}}
        snap = quota.parse_snapshot(raw, NOW)
        self.assertEqual(len(snap.windows), 1)
        self.assertEqual(snap.windows[0].used_percent, 99)

    def test_rejects_garbage(self):
        bad = [
            {},
            {"rateLimits": {}},
            {"rateLimits": {"primary": {"usedPercent": "50"}}},
            {"rateLimits": {"primary": {"usedPercent": -1}}},
            {"rateLimits": {"primary": {"usedPercent": 5, "resetsAt": int(NOW * 1000)}}},
            {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 0}}},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(quota.QuotaParseError):
                quota.parse_snapshot(raw, NOW)
        raw = rate_limits(NOW)
        raw["ordinaryUsageAllowed"] = "yes"
        with self.assertRaises(quota.QuotaParseError):
            quota.parse_snapshot(raw, NOW)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def snap(self, **kw):
        return quota.parse_snapshot(rate_limits(NOW, **kw), NOW)

    def test_ok_when_plenty_left(self):
        self.assertEqual(quota.stop_reasons(self.snap(), self.cfg), [])

    def test_stop_on_five_hour_threshold(self):
        s = self.snap(five_used=97.5)
        self.assertTrue(quota.stop_reasons(s, self.cfg))
        self.assertEqual(quota.resume_after(s, self.cfg), int(NOW + 3600) + self.cfg.grace_seconds)

    def test_weekly_block_waits_for_weekly_reset(self):
        s = self.snap(five_used=100, week_used=100)
        self.assertEqual(quota.resume_after(s, self.cfg), int(NOW + 3 * 86400) + self.cfg.grace_seconds)

    def test_stop_when_backend_disallows(self):
        s = self.snap(allowed=False)
        self.assertIn("backend reports ordinary usage is not allowed", quota.stop_reasons(s, self.cfg))
        # no blocking window -> wait for the furthest known reset
        self.assertEqual(quota.resume_after(s, self.cfg), int(NOW + 3 * 86400) + self.cfg.grace_seconds)

    def test_restart_requires_ordinary_usage_true(self):
        self.assertEqual(quota.restart_blockers(self.snap(), self.cfg, NOW), [])
        self.assertTrue(quota.restart_blockers(self.snap(allowed=None), self.cfg, NOW))
        self.assertTrue(quota.restart_blockers(self.snap(allowed=False), self.cfg, NOW))

    def test_older_codex_without_field_uses_windows(self):
        self.assertEqual(quota.restart_blockers(self.snap(include_allowed=False), self.cfg, NOW), [])
        self.assertTrue(quota.restart_blockers(self.snap(include_allowed=False, five_used=100), self.cfg, NOW))

    def test_stale_window_blocks_restart(self):
        s = self.snap(five_reset=-10)
        self.assertTrue(any("stale" in b for b in quota.restart_blockers(s, self.cfg, NOW)))

    def test_reached_type_blocks(self):
        s = self.snap(reached="rate_limit_reached")
        self.assertTrue(quota.stop_reasons(s, self.cfg))
        self.assertTrue(quota.restart_blockers(s, self.cfg, NOW))


if __name__ == "__main__":
    unittest.main()
