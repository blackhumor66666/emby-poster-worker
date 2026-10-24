"""Ops settings validation, refresh windows, sorting and task cleanup."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CACHE_DIR", tempfile.mkdtemp(prefix="poster-ops-cache-"))
os.environ.setdefault("ADMIN_DATA_DIR", tempfile.mkdtemp(prefix="poster-ops-admin-"))
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost")
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import render_queue  # noqa: E402


class MaintenanceSettingsTests(unittest.TestCase):
    def setUp(self):
        self._saved = app.ADMIN_SETTINGS

    def tearDown(self):
        app.ADMIN_SETTINGS = self._saved

    def test_defaults_come_from_env(self):
        settings = app._normalise_maintenance({}, {})
        self.assertIn("backup_enabled", settings)
        self.assertIn("rating_refresh_days", settings)
        self.assertGreaterEqual(settings["backup_keep"], 1)

    def test_numbers_are_clamped(self):
        settings = app._normalise_maintenance(
            {"backup_hour": 99, "backup_keep": 0, "cache_gc_days": 100000}, {})
        self.assertEqual(settings["backup_hour"], 23)
        self.assertEqual(settings["backup_keep"], 1)
        self.assertEqual(settings["cache_gc_days"], 365)

    def test_rejects_non_http_alert_url(self):
        with self.assertRaises(ValueError):
            app._normalise_maintenance({"alert_webhook_url": "ftp://nope"}, {})

    def test_accepts_https_alert_url(self):
        settings = app._normalise_maintenance({"alert_webhook_url": "https://example.com/hook"}, {})
        self.assertEqual(settings["alert_webhook_url"], "https://example.com/hook")

    def test_rating_refresh_days_override_env(self):
        app.ADMIN_SETTINGS = dict(self._saved)
        app.ADMIN_SETTINGS["maintenance"] = {"rating_refresh_days": {"豆瓣": 1, "IMDb": 2, "RT": 3}}
        seconds = app.rating_refresh_seconds()
        self.assertEqual(seconds["豆瓣"], 86400)
        self.assertEqual(seconds["IMDb"], 172800)
        self.assertEqual(seconds["RT"], 259200)


class SortKeyTests(unittest.TestCase):
    ITEMS = [
        {"Name": "B", "SortName": "b", "ProductionYear": 2001, "CommunityRating": 7.0, "DateCreated": "2026-01-02"},
        {"Name": "A", "SortName": "a", "ProductionYear": 2020, "CommunityRating": 9.0, "DateCreated": "2026-03-02"},
        {"Name": "C", "SortName": "c", "ProductionYear": 1990, "CommunityRating": None, "DateCreated": "2025-12-02"},
    ]

    def names(self, sort):
        return [item["Name"] for item in app._sort_items(self.ITEMS, sort)]

    def test_name_sort(self):
        self.assertEqual(self.names("name"), ["A", "B", "C"])

    def test_year_sort_is_newest_first(self):
        self.assertEqual(self.names("year"), ["A", "B", "C"])

    def test_rating_sort_puts_unrated_last(self):
        self.assertEqual(self.names("rating"), ["A", "B", "C"])

    def test_added_sort_is_newest_first(self):
        self.assertEqual(self.names("added"), ["A", "B", "C"])


class ClearFinishedTests(unittest.TestCase):
    def test_only_finished_jobs_are_removed(self):
        queue = render_queue.RenderQueue(Path(tempfile.mkdtemp()) / "q.json",
                                         app.SCAN_RUN_LOCK, lambda job: None)
        queue.enqueue("1")
        queue.enqueue("2")
        with queue.condition:
            queue.jobs[0]["status"] = "succeeded"
            queue.jobs[1]["status"] = "queued"
            queue._save()
        removed = queue.clear_finished()
        self.assertEqual(removed, 1)
        self.assertEqual([job["item_id"] for job in queue.jobs], ["2"])


if __name__ == "__main__":
    unittest.main()
