"""Bulk queueing: caps, dedupe and validation without touching Emby in parallel."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="poster-bulk-cache-")
os.environ["ADMIN_DATA_DIR"] = tempfile.mkdtemp(prefix="poster-bulk-admin-")
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import render_queue  # noqa: E402


class BulkQueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = render_queue.RenderQueue(
            Path(tempfile.mkdtemp()) / "queue.json", app.SCAN_RUN_LOCK, lambda job: None)
        self._saved_queue = app.WORK_QUEUE
        self._saved_get = app._get_item_by_id
        app.WORK_QUEUE = self.queue
        # Only these ids exist in the fake Emby.
        app._get_item_by_id = lambda item_id: ({"Id": item_id, "Name": f"Item {item_id}"}
                                               if item_id in {"1", "2", "3"} else None)

    def tearDown(self):
        app.WORK_QUEUE = self._saved_queue
        app._get_item_by_id = self._saved_get

    def test_queues_existing_items_and_skips_missing(self):
        result = app._bulk_enqueue(["1", "2", "999"], "render")
        self.assertEqual(result["queued"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(sorted(self.queue.latest_items()), ["1", "2"])

    def test_deduplicates_repeated_ids(self):
        result = app._bulk_enqueue(["1", "1", "1"], "render")
        self.assertEqual(result["queued"], 1)
        self.assertEqual(list(self.queue.latest_items()), ["1"])

    def test_refresh_sets_refresh_flag(self):
        app._bulk_enqueue(["1"], "refresh")
        job = self.queue.latest_for("1")
        self.assertTrue(job["refresh"])
        self.assertEqual(job["action"], "render")

    def test_caps_request_size(self):
        many = [str(index) for index in range(500)]
        result = app._bulk_enqueue(many, "refresh")
        self.assertEqual(result["requested"], 500)
        self.assertLessEqual(result["queued"] + result["skipped"], 100)

    def test_rejects_unknown_action_and_empty_selection(self):
        with self.assertRaises(ValueError):
            app._bulk_enqueue(["1"], "delete")
        with self.assertRaises(ValueError):
            app._bulk_enqueue([], "render")

    def test_ignores_path_traversal_ids(self):
        result = app._bulk_enqueue(["../etc/passwd"], "render")
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["skipped"], 0)


if __name__ == "__main__":
    unittest.main()
