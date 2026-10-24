"""Emby answers a Season with no Primary image with HTTP 500, not 404."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


class OriginalFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        patcher = patch.object(app, "CACHE_DIR", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _response(status: int, content: bytes = b"") -> Mock:
        response = Mock()
        response.status_code = status
        response.content = content
        if status >= 400:
            response.raise_for_status.side_effect = app.requests.HTTPError(f"HTTP {status}")
        else:
            response.raise_for_status.return_value = None
        return response

    def test_season_without_image_reuses_cached_parent_original(self) -> None:
        parent = self.cache / "original" / "series1.jpg"
        parent.parent.mkdir(parents=True)
        parent.write_bytes(b"parent-bytes")
        with patch.object(app.SESSION, "get", return_value=self._response(500)) as request:
            path = app.get_original("season1", fallback_item_id="series1")
        self.assertEqual(path, self.cache / "original" / "season1.jpg")
        self.assertEqual(path.read_bytes(), b"parent-bytes")
        self.assertEqual(request.call_count, 1)

    def test_season_without_image_downloads_parent_image(self) -> None:
        with patch.object(
            app.SESSION, "get",
            side_effect=[self._response(500), self._response(200, b"parent-image")],
        ) as request:
            path = app.get_original("season1", fallback_item_id="series1")
        self.assertEqual(path.read_bytes(), b"parent-image")
        self.assertEqual(request.call_count, 2)

    def test_season_404_also_falls_back_to_the_parent(self) -> None:
        with patch.object(
            app.SESSION, "get",
            side_effect=[self._response(404), self._response(200, b"parent-image")],
        ) as request:
            path = app.get_original("season1", fallback_item_id="series1")
        self.assertEqual(path.read_bytes(), b"parent-image")
        self.assertEqual(request.call_count, 2)

    def test_season_fallback_refuses_a_worker_render(self) -> None:
        with patch.object(
            app.SESSION, "get",
            side_effect=[self._response(500), self._response(200, b"rendered")],
        ), patch.object(app, "is_worker_rendered_image", return_value=True):
            with self.assertRaises(FileNotFoundError):
                app.get_original("season1", fallback_item_id="series1")

    def test_server_error_without_fallback_still_fails(self) -> None:
        with patch.object(app.SESSION, "get", return_value=self._response(500)):
            with self.assertRaises(app.requests.HTTPError):
                app.get_original("movie1")

    def test_missing_image_without_fallback_is_skipped(self) -> None:
        with patch.object(app.SESSION, "get", return_value=self._response(404)):
            with self.assertRaises(FileNotFoundError):
                app.get_original("movie1")


if __name__ == "__main__":
    unittest.main()
