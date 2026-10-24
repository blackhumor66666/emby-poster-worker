"""Storage helpers are used by every report reader, so cover them directly.

A rename during the module split left `read_report` calling a stale function
name; nothing exercised it, so the regression shipped. These tests exist to
make that impossible again.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="poster-storage-cache-")
os.environ["ADMIN_DATA_DIR"] = tempfile.mkdtemp(prefix="poster-storage-admin-")
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import storage  # noqa: E402


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="poster-storage-"))

    def test_load_json_file_missing_returns_fallback(self):
        self.assertEqual(storage.load_json_file(self.dir / "nope.json", {"a": 1}), {"a": 1})

    def test_load_json_file_invalid_returns_fallback(self):
        broken = self.dir / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(storage.load_json_file(broken, []), [])

    def test_read_report_roundtrip(self):
        target = self.dir / "report.json"
        storage.atomic_write_text(target, json.dumps({"status": "ok"}))
        self.assertEqual(storage.read_report(target, {}), {"status": "ok"})
        self.assertEqual(storage.read_report(self.dir / "missing.json", {"empty": True}), {"empty": True})

    def test_app_read_report_wrapper_works(self):
        """app._read_report is the alias used across the app; it must resolve."""
        target = self.dir / "scan.json"
        storage.atomic_write_text(target, json.dumps({"total": 3}))
        self.assertEqual(app._read_report(target, {}), {"total": 3})

    def test_atomic_write_creates_parent_and_leaves_no_temp(self):
        target = self.dir / "nested" / "deep" / "file.json"
        storage.atomic_write_text(target, "{}")
        self.assertTrue(target.is_file())
        leftovers = [p.name for p in target.parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
