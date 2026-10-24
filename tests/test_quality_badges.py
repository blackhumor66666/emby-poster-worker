"""Regression tests for the metadata-driven quality badges.

Run with: python -m unittest discover -s tests -v
No Emby connection is required; every case is a synthetic MediaSource.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="poster-worker-test-")
os.environ["ADMIN_DATA_DIR"] = tempfile.mkdtemp(prefix="poster-worker-admin-")
os.environ["DOUBAN_CACHE_DIR"] = os.path.join(os.environ["CACHE_DIR"], "douban")
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def make_item(streams, path="", name="Test"):
    return {
        "Id": "1",
        "Name": name,
        "Path": path,
        "MediaSources": [{"Path": path, "MediaStreams": streams}],
    }


class QualityBadgeTests(unittest.TestCase):
    def test_4k_dv_webdl_atmos(self):
        item = make_item([
            {"Type": "Video", "Width": 3840, "Height": 2076, "VideoRange": "DolbyVision", "ColorTransfer": "smpte2084"},
            {"Type": "Audio", "Codec": "eac3", "DisplayTitle": "English EAC3 5.1"},
        ], path="/media/Film (2025) - 2160p.WEB-DL.DV.HDR.5.1.Atmos.mkv")
        self.assertEqual(
            [badge["label"] for badge in app.quality_badges(item)],
            ["4K", "WEB-DL", "DOLBY VISION", "ATMOS"],
        )

    def test_1080p_bluray_sdr_has_no_hdr_or_atmos(self):
        item = make_item([
            {"Type": "Video", "Width": 1920, "Height": 1080, "VideoRange": "SDR", "ColorTransfer": "bt709"},
            {"Type": "Audio", "Codec": "dts", "Profile": "DTS-HD MA", "DisplayTitle": "English DTS-HD MA 5.1"},
        ], path="/media/Film (1995) - 1080p.BluRay.H.264.DTS-HD MA 5.1.mkv")
        # badge_minimal (default) keeps premium audio but drops the SDR badge.
        self.assertEqual(
            [badge["label"] for badge in app.quality_badges(item)],
            ["1080P", "BLURAY", "DTS-HD"],
        )
        # The full set is still available when minimal mode is turned off.
        app.RENDER_SETTINGS["badge_minimal"] = False
        try:
            self.assertEqual(
                [badge["label"] for badge in app.quality_badges(item)],
                ["1080P", "BLURAY", "SDR", "DTS-HD"],
            )
        finally:
            app.RENDER_SETTINGS["badge_minimal"] = True

    def test_576p_hdtv_is_not_labelled_hd(self):
        item = make_item([
            {"Type": "Video", "Width": 1024, "Height": 576, "VideoRange": "SDR"},
        ], path="/media/Film (2002) - 576p.HDTV.H.264.mkv")
        self.assertEqual(
            [badge["label"] for badge in app.quality_badges(item)],
            ["576P", "HDTV"],
        )

    def test_iso_without_streams_only_reports_container(self):
        item = make_item([], path="/media/Film (2005)/Film (2005).iso")
        self.assertEqual([badge["label"] for badge in app.quality_badges(item)], ["ISO"])

    def test_4k_webdl_sdr_has_no_hdr(self):
        item = make_item([
            {"Type": "Video", "Width": 3840, "Height": 1616, "VideoRange": "SDR", "ColorTransfer": "bt709"},
        ], path="/media/Film (2026) - 2160p.WEB-DL.H.265.DTS 5.1.mkv")
        # Minimal mode drops the SDR badge; DTS here is a plain DTS track
        # (not DTS-HD), so the audio badge is dropped too.
        self.assertEqual(
            [badge["label"] for badge in app.quality_badges(item)],
            ["4K", "WEB-DL"],
        )

    def test_atmos_is_not_added_to_an_aac_track(self):
        item = make_item([
            {"Type": "Video", "Width": 3840, "Height": 2160, "VideoRange": "SDR"},
            {"Type": "Audio", "Codec": "aac", "DisplayTitle": "English AAC stereo"},
        ], path="/media/Film (2025) - 2160p.WEB-DL.AAC.Atmos.mkv")
        # Minimal mode: AAC is lossy filler, so only resolution and source stay.
        self.assertEqual(
            [badge["label"] for badge in app.quality_badges(item)],
            ["4K", "WEB-DL"],
        )

    def test_hdr10_plus(self):
        item = make_item([
            {"Type": "Video", "Width": 3840, "Height": 2160, "VideoRangeType": "HDR10Plus"},
        ], path="/media/Film (2024) - 2160p.WEB-DL.HDR10+.mkv")
        self.assertEqual(
            [badge["label"] for badge in app.quality_badges(item)],
            ["4K", "WEB-DL", "HDR10+"],
        )

    def test_dolby_vision_does_not_duplicate_hdr10(self):
        item = make_item([
            {"Type": "Video", "Width": 3840, "Height": 2160, "VideoRange": "DolbyVision", "ColorTransfer": "smpte2084"},
        ], path="/media/Film (2006) - 2160p.Ultra HD BluRay.DV.HDR.mkv")
        self.assertEqual(
            [badge["label"] for badge in app.quality_badges(item)],
            ["4K", "BLURAY", "DOLBY VISION"],
        )

    def test_missing_metadata_produces_no_guessed_badge(self):
        item = make_item([], path="/media/Some Title (2020)")
        self.assertEqual(app.quality_badges(item), [])


if __name__ == "__main__":
    unittest.main()
