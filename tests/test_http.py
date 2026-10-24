"""HTTP-layer integration tests against the real handler on a random port.

Unit tests never exercised the routing, gzip/ETag plumbing or auth gate, and
two production incidents (a broken HEAD path, a stale storage alias) lived
exactly there. This module boots PosterHandler for real and speaks HTTP.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CACHE_DIR", tempfile.mkdtemp(prefix="poster-http-cache-"))
os.environ.setdefault("ADMIN_DATA_DIR", tempfile.mkdtemp(prefix="poster-http-admin-"))
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost")
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import app  # noqa: E402
import render_queue  # noqa: E402

PASSWORD = "test-password"


class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = Path(tempfile.mkdtemp(prefix="poster-http-"))
        cls.saved = (app.CACHE_DIR, app.WEBHOOK_TOKEN, app.WORK_QUEUE, app._get_item_by_id)
        app.CACHE_DIR = cls.cache
        app.WEBHOOK_TOKEN = "test-token"
        app.WORK_QUEUE = render_queue.RenderQueue(
            cls.cache / "queue.json", app.SCAN_RUN_LOCK, lambda job: None)
        app._get_item_by_id = lambda item_id: ({"Id": item_id, "Name": "Test", "Type": "Movie"}
                                               if item_id in {"123", "124", "125", "126", "127"} else None)

        (cls.cache / "rendered").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1000, 1500), (30, 40, 50)).save(cls.cache / "rendered" / "123.jpg", "JPEG")

        # Known credentials so the login flow is testable.
        salt, digest = app._hash_password(PASSWORD)
        app.atomic_write_text(app.ADMIN_AUTH_FILE, json.dumps({
            "username": "admin", "salt": salt, "password_hash": digest, "force_change": False}))

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(app.PosterHandler, directory=str(app.WEB_DIR)))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        app.CACHE_DIR, app.WEBHOOK_TOKEN, app.WORK_QUEUE, app._get_item_by_id = cls.saved

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            payload = json.dumps(body).encode() if isinstance(body, dict) else body
            merged = {"Content-Type": "application/json"}
            merged.update(headers or {})
            conn.request(method, path, body=payload, headers=merged)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def login(self):
        status, headers, _ = self.request("POST", "/api/auth/login",
                                          {"username": "admin", "password": PASSWORD})
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("pw_session=", cookie)
        return cookie.split(";", 1)[0]

    # ---------------------------------------------------------------- basics
    def test_health_is_public_json(self):
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertIn("status", json.loads(body))

    def test_index_is_html(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"Poster Worker", body)

    def test_static_asset_gzip_and_304(self):
        status, headers, body = self.request("GET", "/styles.css", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        etag = headers.get("ETag")
        self.assertTrue(etag)
        self.assertLess(len(body), 40_000)

        status, headers, _ = self.request("GET", "/styles.css",
                                          headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
        self.assertEqual(status, 304)

    def test_head_returns_headers_without_body(self):
        status, headers, body = self.request("HEAD", "/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(body, b"")

    def test_unknown_api_gated_then_404(self):
        status, _, _ = self.request("GET", "/api/does-not-exist")
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/api/does-not-exist", headers={"Cookie": self.login()})
        self.assertEqual(status, 404)

    # ------------------------------------------------------------ poster path
    def test_poster_and_thumbnail(self):
        status, headers, body = self.request("GET", "/123.jpg?v=9")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        self.assertGreater(len(body), 1000)

        status, headers, thumb = self.request("GET", "/123.jpg?v=9&w=400")
        self.assertEqual(status, 200)
        self.assertLess(len(thumb), len(body))
        self.assertTrue((self.cache / "thumbnails" / "400" / "123.jpg").is_file())

    def test_missing_poster_is_404(self):
        status, _, _ = self.request("GET", "/999.jpg?v=9")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------ auth
    def test_protected_endpoints_require_session(self):
        for method, path in (("GET", "/api/overview"), ("GET", "/api/config"),
                             ("POST", "/api/items/bulk")):
            with self.subTest(path=path):
                status, _, _ = self.request(method, path, {"ids": ["123"], "action": "render"}
                                            if method == "POST" else None)
                self.assertEqual(status, 401)

    def test_login_rejects_wrong_password(self):
        status, _, _ = self.request("POST", "/api/auth/login",
                                    {"username": "admin", "password": "wrong"})
        self.assertEqual(status, 401)

    def test_login_then_config_includes_maintenance(self):
        cookie = self.login()
        status, _, body = self.request("GET", "/api/config", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("maintenance", payload)
        self.assertIn("backup", payload["maintenance"])
        self.assertIn("cache_gc", payload["maintenance"])

    def test_keepalive_survives_rejected_post(self):
        """A rejected POST must drain its body, or the next request gets a 400."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            body = json.dumps({"ids": ["123"], "action": "render"})
            for _ in range(3):
                conn.request("POST", "/api/items/bulk", body=body,
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                response.read()
                # 400 here means the previous body leaked into the request line
                self.assertEqual(response.status, 401)
            # The connection must still be usable for a different POST.
            conn.request("POST", "/emby/webhook", body=body,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
        finally:
            conn.close()

    def test_ops_settings_round_trip(self):
        """Saving maintenance settings must be reflected by the next read."""
        cookie = self.login()
        status, _, body = self.request("POST", "/api/config", {
            "maintenance": {
                "cache_gc_days": 9,
                "backup_keep": 3,
                "alert_webhook_url": "https://example.com/hook",
                "rating_refresh_days": {"豆瓣": 2, "IMDb": 5, "RT": 6},
            }
        }, headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)

        status, _, body = self.request("GET", "/api/config", headers={"Cookie": cookie})
        payload = json.loads(body)
        maintenance = payload["maintenance"]
        self.assertEqual(maintenance["cache_gc"]["days"], 9)
        self.assertEqual(maintenance["backup"]["keep"], 3)
        self.assertTrue(maintenance["alert"]["enabled"])
        self.assertEqual(maintenance["rating_refresh_days"]["IMDb"], 5)

    def test_ops_settings_reject_bad_alert_url(self):
        cookie = self.login()
        status, _, _ = self.request("POST", "/api/config",
                                    {"maintenance": {"alert_webhook_url": "ftp://nope"}},
                                    headers={"Cookie": cookie})
        self.assertEqual(status, 400)

    def test_tasks_clear_requires_session(self):
        status, _, _ = self.request("POST", "/api/tasks/clear", {})
        self.assertEqual(status, 401)
        status, _, body = self.request("POST", "/api/tasks/clear", {}, headers={"Cookie": self.login()})
        self.assertEqual(status, 200)
        self.assertIn("removed", json.loads(body))

    # --------------------------------------------------------------- webhook
    def test_webhook_requires_token(self):
        status, _, _ = self.request("POST", "/emby/webhook",
                                    {"Event": "library.new", "Item": {"Id": "123", "Type": "Movie"}})
        self.assertEqual(status, 403)

    def test_webhook_accepts_multipart_payload_field(self):
        """Emby's plugin posts multipart with the JSON document in a field."""
        boundary = "----embyboundary"
        document = json.dumps({"Event": "library.new",
                               "Item": {"Id": "124", "Type": "Movie", "Name": "Test"}})
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="payload"\r\n\r\n'
                + document + f"\r\n--{boundary}--\r\n").encode()
        status, _, resp = self.request("POST", "/emby/webhook?token=test-token", body=body,
                                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(resp)["queued"])

    def test_webhook_accepts_multipart_loose_fields(self):
        boundary = "----embyboundary"
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="Event"\r\n\r\nlibrary.new\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="Item"\r\n\r\n'
                + json.dumps({"Id": "125", "Type": "Movie"}) + f"\r\n--{boundary}--\r\n").encode()
        status, _, resp = self.request("POST", "/emby/webhook?token=test-token", body=body,
                                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(resp)["queued"])

    def test_webhook_accepts_urlencoded(self):
        body = "Event=library.new&Item=%7B%22Id%22%3A%22126%22%2C%22Type%22%3A%22Movie%22%7D"
        status, _, resp = self.request("POST", "/emby/webhook?token=test-token", body=body.encode(),
                                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(resp)["queued"])

    def test_webhook_unparsable_body_is_not_an_error(self):
        boundary = "----embyboundary"
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="Something"\r\n\r\nvalue\r\n'
                f"--{boundary}--\r\n").encode()
        status, _, resp = self.request("POST", "/emby/webhook?token=test-token", body=body,
                                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(resp).get("ignored"), "event")

    def test_webhook_queues_with_token(self):
        status, _, body = self.request(
            "POST", "/emby/webhook?token=test-token",
            {"Event": "library.new", "Item": {"Id": "127", "Type": "Movie", "Name": "Test"}})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(body)["queued"])

    # ------------------------------------------------------------------ bulk
    def test_bulk_render_accepts_session(self):
        cookie = self.login()
        status, _, body = self.request("POST", "/api/items/bulk",
                                       {"ids": ["123"], "action": "render"},
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 202)
        result = json.loads(body)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["action"], "render")


if __name__ == "__main__":
    unittest.main()
