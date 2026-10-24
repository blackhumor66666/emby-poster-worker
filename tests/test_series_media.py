"""Series media cache compaction keeps badges identical while shrinking JSON.

Badges read the best video stream and the first Atmos/DTS:X audio, so a
15,000-episode series only needs a handful of representative sources.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="poster-series-cache-")
os.environ["ADMIN_DATA_DIR"] = tempfile.mkdtemp(prefix="poster-series-admin-")
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def source(width, height, codec="hevc", audio=None, label="ep"):
    streams = [{"Type": "Video", "Width": width, "Height": height, "Codec": codec}]
    if audio:
        streams.append({"Type": "Audio", "Codec": audio, "DisplayTitle": f"English {audio}"})
    return {"Id": label, "MediaStreams": streams}


class CompactionTests(unittest.TestCase):
    def test_deduplicates_identical_layouts(self):
        sources = [source(1920, 1080, audio="aac", label=f"ep{i}") for i in range(200)]
        kept, _ = app._compact_series_media(sources, [])
        self.assertEqual(len(kept), 1)

    def test_keeps_best_video_even_beyond_limit(self):
        sources = [source(1280 + index, 720, audio="aac", label=f"ep{index}") for index in range(40)]
        sources.append(source(3840, 2160, audio="aac", label="uhd"))
        kept, _ = app._compact_series_media(sources, [], limit=5)
        self.assertLessEqual(len(kept), 5)
        self.assertTrue(any(item["Id"] == "uhd" for item in kept))

    def test_keeps_immersive_audio_source(self):
        sources = [source(1920, 1080, audio="aac", label=f"ep{index}") for index in range(30)]
        sources.append(source(1920, 1080, audio="eac3", label="atmos-ep"))
        sources[-1]["MediaStreams"][1]["DisplayTitle"] = "English EAC3 Atmos 7.1"
        kept, _ = app._compact_series_media(sources, [], limit=4)
        self.assertTrue(any(item["Id"] == "atmos-ep" for item in kept))

    def test_respects_limit_and_dedupes_streams(self):
        sources = [source(1920, 1080, audio="aac", label=f"a{index}") for index in range(10)]
        sources += [source(3840, 2160, audio="eac3", label=f"b{index}") for index in range(10)]
        streams = [{"Type": "Video", "Width": 1920, "Height": 1080}] * 50
        kept, kept_streams = app._compact_series_media(sources, streams, limit=4)
        self.assertLessEqual(len(kept), 4)
        self.assertEqual(len(kept_streams), 1)

    def test_write_then_read_migrates_oversized_cache(self):
        cache_dir = Path(tempfile.mkdtemp())
        saved = app.CACHE_DIR
        app.CACHE_DIR = cache_dir
        try:
            item = {"Id": "42", "Type": "Series", "DateModified": "2026-01-01T00:00:00Z"}
            target = cache_dir / "series-media" / "42.json"
            target.parent.mkdir(parents=True)
            # A legacy cache with one source per episode, as the worker used to write.
            legacy = [source(1920, 1080, audio="aac", label=f"ep{i}") for i in range(500)]
            legacy_fetched_at = time.time() - 86400
            target.write_text(json.dumps({
                "signature": app._series_media_signature(item),
                "fetched_at": time.time() - 86400,
                "episode_count": 500,
                "media_sources": legacy,
                "media_streams": [],
            }), encoding="utf-8")
            before = target.stat().st_size

            payload = app._read_series_media_cache(item)
            self.assertIsNotNone(payload)
            self.assertLessEqual(len(payload["media_sources"]), app.SERIES_MEDIA_SOURCE_LIMIT)
            self.assertEqual(payload["episode_count"], 500)
            self.assertLess(target.stat().st_size, before)
            # fetched_at is preserved so compaction does not silently extend the TTL
            self.assertAlmostEqual(payload["fetched_at"], legacy_fetched_at, delta=1)
        finally:
            app.CACHE_DIR = saved


if __name__ == "__main__":
    unittest.main()
