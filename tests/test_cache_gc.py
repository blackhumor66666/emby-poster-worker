"""Cache hygiene must never treat a partial Emby response as mass deletion."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="poster-gc-cache-")
os.environ["ADMIN_DATA_DIR"] = tempfile.mkdtemp(prefix="poster-gc-admin-")
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class CacheGcTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="poster-gc-"))
        for name in ("original", "rendered", "state", "series-media"):
            (self.root / name).mkdir()
        self._saved = (app.CACHE_DIR, app.CACHE_GC_ENABLED, app.CACHE_GC_DAYS, app.ADMIN_SETTINGS)
        app.CACHE_DIR = self.root
        app.CACHE_GC_ENABLED = True
        app.CACHE_GC_DAYS = 7
        # Ops settings take precedence over the module constants.
        app.ADMIN_SETTINGS = {**self._saved[3], "maintenance": {**self._saved[3].get("maintenance", {}),
                                                               "cache_gc_enabled": True, "cache_gc_days": 7}}

    def tearDown(self):
        app.CACHE_DIR, app.CACHE_GC_ENABLED, app.CACHE_GC_DAYS, app.ADMIN_SETTINGS = self._saved

    def make_orphan(self, name="original", item_id="999"):
        path = self.root / name / f"{item_id}.jpg"
        path.write_bytes(b"x")
        old = time.time() - 30 * 86400
        os.utime(path, (old, old))
        return path

    def test_incomplete_scan_keeps_everything(self):
        orphan = self.make_orphan()
        result = app.cache_gc({"1", "2"}, expected_total=100)
        self.assertTrue(orphan.exists())
        self.assertEqual(result["removed"], 0)
        self.assertGreater(result["skipped_incomplete"], 0)

    def test_complete_scan_removes_old_orphans(self):
        orphan = self.make_orphan()
        keep = self.root / "original" / "1.jpg"
        keep.write_bytes(b"y")
        result = app.cache_gc({"1"}, expected_total=1)
        self.assertFalse(orphan.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(result["removed"], 1)

    def test_recent_orphans_survive_grace_period(self):
        path = self.root / "original" / "777.jpg"
        path.write_bytes(b"z")  # fresh mtime
        result = app.cache_gc({"1"}, expected_total=1)
        self.assertTrue(path.exists())
        self.assertEqual(result["removed"], 0)

    def test_disabled_never_deletes(self):
        app.CACHE_GC_ENABLED = False
        app.ADMIN_SETTINGS = {**app.ADMIN_SETTINGS, "maintenance": {**app.ADMIN_SETTINGS.get("maintenance", {}),
                                                                   "cache_gc_enabled": False}}
        orphan = self.make_orphan()
        result = app.cache_gc({"1"}, expected_total=1)
        self.assertTrue(orphan.exists())
        self.assertEqual(result["removed"], 0)


if __name__ == "__main__":
    unittest.main()
