"""Emby webhook guardrails: only new movies/series/seasons, one job at a time."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="poster-hook-cache-")
os.environ["ADMIN_DATA_DIR"] = tempfile.mkdtemp(prefix="poster-hook-admin-")
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import render_queue  # noqa: E402


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.queue = render_queue.RenderQueue(
            Path(tempfile.mkdtemp()) / "queue.json", app.SCAN_RUN_LOCK, lambda job: None)
        self._saved_queue = app.WORK_QUEUE
        app.WORK_QUEUE = self.queue
        self._saved = (app.WEBHOOK_MAX_QUEUE, app.WEBHOOK_MIN_INTERVAL)
        app.WEBHOOK_MAX_QUEUE = 2
        app.WEBHOOK_MIN_INTERVAL = 30.0

    def tearDown(self):
        app.WORK_QUEUE = self._saved_queue
        app.WEBHOOK_MAX_QUEUE, app.WEBHOOK_MIN_INTERVAL = self._saved

    def payload(self, item_id="123", item_type="Movie", event="library.new"):
        return {"Event": event, "Item": {"Id": item_id, "Type": item_type, "Name": "Test"}}

    def test_ignores_non_add_events(self):
        result = app.handle_emby_webhook(self.payload(event="library.removed"))
        self.assertEqual(result.get("ignored"), "event")
        self.assertEqual(self.queue.snapshot()["counts"]["queued"], 0)

    def test_ignores_episodes(self):
        result = app.handle_emby_webhook(self.payload(item_type="Episode"))
        self.assertEqual(result.get("ignored"), "type")
        self.assertEqual(self.queue.snapshot()["counts"]["queued"], 0)

    def test_queues_movie(self):
        result = app.handle_emby_webhook(self.payload())
        self.assertTrue(result.get("queued"))
        self.assertEqual(self.queue.snapshot()["counts"]["queued"], 1)

    def test_dedupes_while_queued(self):
        app.handle_emby_webhook(self.payload())
        second = app.handle_emby_webhook(self.payload())
        self.assertTrue(second.get("deduped"))
        self.assertEqual(self.queue.snapshot()["counts"]["queued"], 1)

    def test_rejects_when_queue_is_full(self):
        app.handle_emby_webhook(self.payload("1"))
        app.handle_emby_webhook(self.payload("2"))
        result = app.handle_emby_webhook(self.payload("3"))
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "queue_full")
        self.assertEqual(self.queue.snapshot()["counts"]["queued"], 2)

    def test_invalid_item_id_is_ignored(self):
        result = app.handle_emby_webhook(self.payload("../../etc/passwd"))
        self.assertEqual(result.get("ignored"), "id")


if __name__ == "__main__":
    unittest.main()
