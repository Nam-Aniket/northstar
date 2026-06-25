"""Unit tests for app/freshness.py - pure, deterministic (now is injected)."""
import unittest
from datetime import datetime, timezone

from app import freshness as F

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


class PostedAtOf(unittest.TestCase):
    def test_prefers_jd_posted_date_timestamp(self):
        job = {"jd_posted_date": "2026-06-25T11:50:00", "first_seen_date": "2026-06-20"}
        self.assertEqual(F.posted_at_of(job), "2026-06-25T11:50:00")

    def test_date_only_jd_posted_date(self):
        self.assertEqual(F.posted_at_of({"jd_posted_date": "2026-06-24"}), "2026-06-24")

    def test_falls_back_to_first_seen(self):
        self.assertEqual(F.posted_at_of({"jd_posted_date": "", "first_seen_date": "2026-06-20"}),
                         "2026-06-20")

    def test_empty_when_nothing(self):
        self.assertEqual(F.posted_at_of({}), "")


class HumanizeAgo(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(F.humanize_ago("2026-06-25T11:48:00", NOW), "12m ago")

    def test_hours(self):
        self.assertEqual(F.humanize_ago("2026-06-25T09:00:00", NOW), "3h ago")

    def test_just_now(self):
        self.assertEqual(F.humanize_ago("2026-06-25T11:59:50", NOW), "just now")

    def test_date_only_today(self):
        self.assertEqual(F.humanize_ago("2026-06-25", NOW), "today")

    def test_date_only_days(self):
        self.assertEqual(F.humanize_ago("2026-06-22", NOW), "3d ago")

    def test_empty(self):
        self.assertEqual(F.humanize_ago("", NOW), "")


class JustPosted(unittest.TestCase):
    def test_within_30_min(self):
        self.assertTrue(F.is_just_posted("2026-06-25T11:45:00", NOW))

    def test_over_30_min(self):
        self.assertFalse(F.is_just_posted("2026-06-25T11:00:00", NOW))

    def test_date_only_never_just_posted(self):
        self.assertFalse(F.is_just_posted("2026-06-25", NOW))


class FreshOk(unittest.TestCase):
    def test_within_1h(self):
        self.assertTrue(F.fresh_ok("2026-06-25T11:30:00", NOW, "1h"))

    def test_outside_1h(self):
        self.assertFalse(F.fresh_ok("2026-06-25T10:00:00", NOW, "1h"))

    def test_unknown_bucket_passes(self):
        self.assertTrue(F.fresh_ok("2026-06-20", NOW, ""))

    def test_date_only_passes_only_24h(self):
        self.assertTrue(F.fresh_ok("2026-06-25", NOW, "24h"))
        self.assertFalse(F.fresh_ok("2026-06-25", NOW, "1h"))


if __name__ == "__main__":
    unittest.main()
