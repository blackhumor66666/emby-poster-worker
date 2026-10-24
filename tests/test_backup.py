"""Originals backup: creates the archive, prunes old copies, never blocks scans."""
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="poster-backup-cache-")
os.environ["ADMIN_DATA_DIR"] = tempfile.mkdtemp(prefix="poster-backup-admin-")
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="poster-backup-"))
        (self.root / "original").mkdir()
        (self.root / "original" / "1.jpg").write_bytes(b"x" * 2048)
        (self.root / "original" / "2.jpg").write_bytes(b"y" * 1024)
        self.backup = self.root / "backup"
        self._saved = (app.CACHE_DIR, app.BACKUP_DIR, app.BACKUP_KEEP, app.BACKUP_STATUS_FILE,
                       app.ADMIN_DATA_DIR, app.ADMIN_SETTINGS)
        app.CACHE_DIR = self.root
        # Isolate config files: another test module may have written auth.json.
        app.ADMIN_DATA_DIR = self.root / "config"
        app.BACKUP_DIR = self.backup
        app.BACKUP_KEEP = 1
        app.BACKUP_STATUS_FILE = self.root / "reports" / "backup.json"
        # Ops settings now take precedence over the module constants.
        app.ADMIN_SETTINGS = {**self._saved[5], "maintenance": {**self._saved[5].get("maintenance", {}),
                                                               "backup_keep": 1, "backup_enabled": True}}

    def tearDown(self):
        (app.CACHE_DIR, app.BACKUP_DIR, app.BACKUP_KEEP, app.BACKUP_STATUS_FILE,
         app.ADMIN_DATA_DIR, app.ADMIN_SETTINGS) = self._saved

    def test_disabled_when_no_backup_dir(self):
        app.BACKUP_DIR = None
        self.assertEqual(app.run_originals_backup()["status"], "disabled")

    def test_creates_archive_with_all_originals(self):
        result = app.run_originals_backup()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files"], 2)
        archive = self.backup / result["file"]
        self.assertTrue(archive.is_file())
        with tarfile.open(archive, "r:gz") as tar:
            names = sorted(tar.getnames())
        self.assertEqual(names, ["original/1.jpg", "original/2.jpg"])
        status = json.loads(app.BACKUP_STATUS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "ok")

    def test_prunes_to_keep_count(self):
        self.backup.mkdir(parents=True)
        old = self.backup / "originals-20200101.tar.gz"
        old.write_bytes(b"old")
        older = self.backup / "originals-20190101.tar.gz"
        older.write_bytes(b"older")
        past = time.time() - 86400 * 30
        os.utime(old, (past, past))
        os.utime(older, (past - 86400, past - 86400))
        result = app.run_originals_backup()
        self.assertEqual(result["status"], "ok")
        remaining = sorted(path.name for path in self.backup.glob("*.tar.gz"))
        self.assertEqual(remaining, [result["file"]])


if __name__ == "__main__":
    unittest.main()
