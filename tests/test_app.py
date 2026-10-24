from pathlib import Path
import sys
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


class RatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.community_switch = app.COMMUNITY_RATING_IS_DOUBAN
        self.strict_cache = app.DOUBAN_REQUIRE_VERIFIED_CACHE
        self.require_id = app.COMMUNITY_RATING_REQUIRES_DOUBAN_ID
        self.critic_rating_is_rt = app.CRITIC_RATING_IS_RT
        self.omdb_key = app.OMDB_API_KEY
        self.override_file = app.RATING_OVERRIDES_FILE
        self.override_mtime = app._RATING_OVERRIDES_MTIME
        app.COMMUNITY_RATING_IS_DOUBAN = True
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = False
        app.COMMUNITY_RATING_REQUIRES_DOUBAN_ID = False
        app.OMDB_API_KEY = ""

    def tearDown(self) -> None:
        app.COMMUNITY_RATING_IS_DOUBAN = self.community_switch
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = self.strict_cache
        app.COMMUNITY_RATING_REQUIRES_DOUBAN_ID = self.require_id
        app.CRITIC_RATING_IS_RT = self.critic_rating_is_rt
        app.OMDB_API_KEY = self.omdb_key
        app.RATING_OVERRIDES_FILE = self.override_file
        app._RATING_OVERRIDES_MTIME = self.override_mtime
        app._RATING_OVERRIDES_CACHE = {}

    def test_official_shenyi_cache_layout_is_resolved_by_imdb_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_dir = app.DOUBAN_CACHE_DIR
            app.DOUBAN_CACHE_DIR = Path(directory)
            try:
                path = Path(directory) / "cache" / "1296217_tt0117381" / "all.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"id": "1296217", "rating": {"value": 9.1}}), encoding="utf-8")
                item = {"Type": "Movie", "ProviderIds": {"Imdb": "tt0117381"}}
                result = app._douban_cache_result(item)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["value"], "9.1")
                self.assertEqual(result["match"], "imdb_id")
            finally:
                app.DOUBAN_CACHE_DIR = old_dir

    def test_douban_cache_identity_mismatch_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_dir = app.DOUBAN_CACHE_DIR
            app.DOUBAN_CACHE_DIR = Path(directory)
            try:
                path = Path(directory) / "cache" / "1296217_tt0117381" / "all.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"id": "9999999", "rating": {"value": 9.1}}), encoding="utf-8")
                item = {"Type": "Movie", "ProviderIds": {"Imdb": "tt0117381"}}
                self.assertEqual(app._douban_cache_result(item)["status"], "douban_id_mismatch")
                self.assertIsNone(app._douban_cache_rating(item))
            finally:
                app.DOUBAN_CACHE_DIR = old_dir

    def test_negative_shenyi_placeholder_is_not_a_rating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_dir = app.DOUBAN_CACHE_DIR
            app.DOUBAN_CACHE_DIR = Path(directory)
            try:
                (Path(directory) / "cache" / "0_tt0117381").mkdir(parents=True)
                item = {"Type": "Movie", "ProviderIds": {"Imdb": "tt0117381"}}
                self.assertEqual(app._douban_cache_result(item)["status"], "negative_placeholder")
                self.assertIsNone(app._douban_cache_rating(item))
            finally:
                app.DOUBAN_CACHE_DIR = old_dir

    def test_local_douban_cache_takes_precedence_over_community_rating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_dir = app.DOUBAN_CACHE_DIR
            app.DOUBAN_CACHE_DIR = Path(directory)
            try:
                path = Path(directory) / "douban-movies" / "1298624_tt0105323" / "all.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"id": "1298624", "rating": {"value": 9.1}}), encoding="utf-8")
                item = {
                    "Type": "Movie",
                    "CommunityRating": 7.8,
                    "ProviderIds": {"Douban": "1298624", "Imdb": "tt0105323"},
                }
                self.assertEqual(app._douban_rating(item), ("9.1", "douban.local_cache"))
                result = app.rating_details(item, {"豆瓣"}, {}, {})
                self.assertEqual(result["values"]["豆瓣"], "9.1")
                self.assertEqual(result["details"]["豆瓣"]["source"], "douban.local_cache")
            finally:
                app.DOUBAN_CACHE_DIR = old_dir

    def test_local_douban_cache_works_without_imdb_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_dir = app.DOUBAN_CACHE_DIR
            app.DOUBAN_CACHE_DIR = Path(directory)
            try:
                path = Path(directory) / "douban-movies" / "1298624_tt0105323" / "all.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"id": "1298624", "rating": {"value": 9.2}}), encoding="utf-8")
                item = {"Type": "Movie", "ProviderIds": {"Douban": "1298624"}}
                app.DOUBAN_REQUIRE_VERIFIED_CACHE = True
                self.assertEqual(app._douban_rating(item), ("9.2", "douban.local_cache"))
            finally:
                app.DOUBAN_CACHE_DIR = old_dir

    def test_temporary_douban_missing_keeps_previous_score(self) -> None:
        old_confirmations = app.DOUBAN_MISSING_CONFIRMATIONS
        app.DOUBAN_MISSING_CONFIRMATIONS = 3
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = True
        item = {"Type": "Movie", "ProviderIds": {"Douban": "1298624"}}
        cached = {"豆瓣": "8.6"}
        details = {"豆瓣": {"status": "ok", "source": "douban.local_cache", "douban_id": "1298624", "emby_douban_id": "1298624", "imdb_id": None, "missing_attempts": 0}}
        try:
            first = app.rating_details(item, {"豆瓣"}, cached, details)
            self.assertEqual(first["values"]["豆瓣"], "8.6")
            self.assertEqual(first["details"]["豆瓣"]["status"], "temporarily_missing")
            second = app.rating_details(item, {"豆瓣"}, first["values"], first["details"])
            self.assertEqual(second["values"]["豆瓣"], "8.6")
            third = app.rating_details(item, {"豆瓣"}, second["values"], second["details"])
            self.assertNotIn("豆瓣", third["values"])
        finally:
            app.DOUBAN_MISSING_CONFIRMATIONS = old_confirmations

    def test_community_rating_does_not_require_douban_provider_id(self) -> None:
        item = {"CommunityRating": 7.3, "ProviderIds": {"Imdb": "tt1234567"}}
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = False
        self.assertEqual(app.ratings(item).get("豆瓣"), "7.3")

    def test_invalid_or_missing_community_rating_stays_missing(self) -> None:
        self.assertNotIn("豆瓣", app.ratings({"ProviderIds": {"Douban": "123"}}))
        self.assertNotIn("豆瓣", app.ratings({"CommunityRating": 11, "ProviderIds": {}}))

    def test_verified_cache_mode_hides_unverified_emby_score(self) -> None:
        original_dir = app.DOUBAN_CACHE_DIR
        original_strict = app.DOUBAN_REQUIRE_VERIFIED_CACHE
        app.DOUBAN_CACHE_DIR = None
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = True
        try:
            value, source = app._douban_rating({"CommunityRating": 7.8, "ProviderIds": {}})
            self.assertIsNone(value)
            self.assertEqual(source, "douban.verified_cache_required")
        finally:
            app.DOUBAN_CACHE_DIR = original_dir
            app.DOUBAN_REQUIRE_VERIFIED_CACHE = original_strict

    def test_new_cache_is_detected_after_strict_mode_missing_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_dir, original_strict = app.DOUBAN_CACHE_DIR, app.DOUBAN_REQUIRE_VERIFIED_CACHE
            app.DOUBAN_CACHE_DIR = Path(directory)
            app.DOUBAN_REQUIRE_VERIFIED_CACHE = True
            item = {"Id": "163251", "Type": "Movie", "CommunityRating": 7.8, "ProviderIds": {"Douban": "1298624", "Imdb": "tt0105323"}}
            details = {"豆瓣": {"status": "no_rating", "source": "douban.verified_cache_required"}}
            try:
                self.assertFalse(app.community_rating_changed(item, {}, details))
                path = Path(directory) / "douban-movies" / "1298624_tt0105323" / "all.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"id": "1298624", "rating": {"value": 9.1}}), encoding="utf-8")
                self.assertTrue(app.community_rating_changed(item, {}, details))
            finally:
                app.DOUBAN_CACHE_DIR, app.DOUBAN_REQUIRE_VERIFIED_CACHE = original_dir, original_strict

    def test_audit_identifies_conflicts_and_missing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_dir, old_report = app.DOUBAN_CACHE_DIR, app.DOUBAN_AUDIT_FILE
            app.DOUBAN_CACHE_DIR = Path(directory)
            app.DOUBAN_AUDIT_FILE = Path(directory) / "report.json"
            try:
                path = Path(directory) / "douban-movies" / "1298624_tt0105323" / "all.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"id": "1298624", "rating": {"value": 9.1}}), encoding="utf-8")
                items = [
                    {"Id": "1", "Name": "闻香识女人", "Type": "Movie", "CommunityRating": 7.8,
                     "ProviderIds": {"Douban": "1298624", "Imdb": "tt0105323"}},
                    {"Id": "2", "Name": "未核对", "Type": "Movie", "CommunityRating": 7.0, "ProviderIds": {}},
                ]
                report = app.audit_douban_ratings(items)
                self.assertEqual(report["counts"]["score_conflict"], 1)
                self.assertEqual(report["counts"]["missing_ids"], 1)
                self.assertEqual(report["counts"]["unverified_emby_score"], 1)
                self.assertTrue(app.DOUBAN_AUDIT_FILE.exists())
            finally:
                app.DOUBAN_CACHE_DIR, app.DOUBAN_AUDIT_FILE = old_dir, old_report

    def test_community_rating_change_is_detected_before_refresh_interval(self) -> None:
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = False
        details = {"豆瓣": {"status": "ok", "source": "emby.community_rating"}}
        self.assertTrue(app.community_rating_changed({"CommunityRating": 7.3}, {"豆瓣": "6.6"}, details))
        self.assertFalse(app.community_rating_changed({"CommunityRating": 7.3}, {"豆瓣": "7.3"}, details))
        self.assertFalse(app.community_rating_changed({"CommunityRating": None}, {"豆瓣": "6.6"}, details))

    def test_community_rating_change_does_not_override_other_sources(self) -> None:
        self.assertFalse(app.community_rating_changed(
            {"CommunityRating": 7.3}, {"豆瓣": "6.6"},
            {"豆瓣": {"status": "override", "source": "rating_overrides"}},
        ))
        app.COMMUNITY_RATING_IS_DOUBAN = False
        self.assertFalse(app.community_rating_changed(
            {"CommunityRating": 7.3}, {"豆瓣": "6.6"},
            {"豆瓣": {"status": "ok", "source": "emby.community_rating"}},
        ))

    def test_rating_diagnostics_flag_stale_douban_without_substituting_sources(self) -> None:
        codes = app._rating_diagnostic_codes(
            {"CommunityRating": 7.3},
            {"豆瓣": "6.6", "IMDb": "7.0"},
            {"豆瓣": {"status": "ok", "source": "emby.community_rating"}, "IMDb": {"status": "ok", "source": "omdb.provider_id"}, "RT": {"status": "no_rating", "source": "omdb.provider_id"}},
            True,
            [],
        )
        self.assertIn("douban_stale", codes)
        self.assertIn("rt_missing", codes)
        self.assertNotIn("omdb_api_invalid", codes)

    def test_manual_douban_override_is_not_marked_stale(self) -> None:
        codes = app._rating_diagnostic_codes(
            {"CommunityRating": 6.6},
            {"豆瓣": "7.1"},
            {"豆瓣": {"status": "override", "source": "rating_overrides"}},
            True,
            [],
        )
        self.assertNotIn("douban_stale", codes)

    def test_rating_diagnostics_flag_invalid_omdb_key(self) -> None:
        codes = app._rating_diagnostic_codes(
            {"CommunityRating": 7.3},
            {"豆瓣": "7.3"},
            {"豆瓣": {"status": "ok", "source": "emby.community_rating"}, "IMDb": {"status": "invalid_api_key", "source": "omdb.provider_id"}, "RT": {"status": "invalid_api_key", "source": "omdb.provider_id"}},
            True,
            [],
        )
        self.assertIn("omdb_api_invalid", codes)

    def test_provider_id_lookup_is_case_insensitive(self) -> None:
        item = {"ProviderIds": {"IMDb": "tt1234567"}}
        self.assertEqual(app.provider_id(item["ProviderIds"], "Imdb"), "tt1234567")

    def test_get_items_reads_all_emby_pages(self) -> None:
        first = {"Items": [{"Id": "1"}, {"Id": "2"}], "TotalRecordCount": 3}
        second = {"Items": [{"Id": "3"}], "TotalRecordCount": 3}
        fake_emby = Mock(side_effect=[Mock(json=lambda: first), Mock(json=lambda: second)])
        with patch.object(app, "emby", fake_emby), patch.object(app, "EMBY_PAGE_SIZE", 2):
            items = app.get_items(timeout=7)
        self.assertEqual([item["Id"] for item in items], ["1", "2", "3"])
        self.assertEqual([call.kwargs["params"]["StartIndex"] for call in fake_emby.call_args_list], ["0", "2"])
        self.assertEqual(fake_emby.call_args_list[0].kwargs["timeout"], 7)

    def test_emby_allows_callers_to_override_timeout(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        with patch.object(app, "EMBY_URL", "http://emby.test"), patch.object(app.SESSION, "request", return_value=response) as request:
            app.emby("GET", "/System/Info", timeout=7)
        self.assertEqual(request.call_args.kwargs["timeout"], 7)

    def test_series_media_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_cache = app.CACHE_DIR
            old_days = app.SERIES_MEDIA_CACHE_DAYS
            try:
                app.CACHE_DIR = Path(directory)
                app.SERIES_MEDIA_CACHE_DAYS = 7
                item = {"Id": "series-1", "DateModified": "2026-09-25T00:00:00Z"}
                sources = [{"Path": "episode.mkv"}]
                streams = [{"Type": "Video", "Width": 3840, "Height": 2160}]
                app._write_series_media_cache(item, sources, streams, 12)
                cached = app._read_series_media_cache(item)
                self.assertEqual(cached["episode_count"], 12)
                self.assertEqual(cached["media_sources"], sources)
                self.assertEqual(cached["media_streams"], streams)
            finally:
                app.CACHE_DIR = old_cache
                app.SERIES_MEDIA_CACHE_DAYS = old_days

    def test_quality_uses_stream_display_title_when_dimensions_are_missing(self) -> None:
        item = {"MediaStreams": [{"Type": "Video", "DisplayTitle": "4K HEVC HDR10"}]}
        self.assertEqual(app.quality_labels(item)[:2], ["4K", "HDR10"])

    def test_quality_marks_disc_source_when_resolution_is_unknown(self) -> None:
        item = {"Path": r"\\nas\movie\title.UltraHD.BluRay.iso"}
        self.assertEqual(app.quality_labels(item)[0], "4K")
        item = {"Path": r"\\nas\movie\title.BluRay.iso"}
        self.assertEqual(app.quality_labels(item)[0], "BLURAY")

    def test_omdb_title_policy_skips_unmatched_concert_search(self) -> None:
        item = {"Genres": ["Music", "Concert"], "ProviderIds": {}}
        self.assertTrue(app._should_skip_omdb_title_search(item))
        item["ProviderIds"] = {"Imdb": "tt1234567"}
        self.assertFalse(app._should_skip_omdb_title_search(item))

    def test_omdb_circuit_status_is_safe_when_closed(self) -> None:
        self.assertIsNone(app._omdb_circuit_status())

    def test_omdb_status_classifies_external_failures(self) -> None:
        self.assertEqual(app._omdb_status({"Response": "False", "Error": "Invalid API key!"}), "invalid_api_key")
        self.assertEqual(app._omdb_status({"Response": "False", "_worker_status": "rate_limited"}), "rate_limited")
        self.assertEqual(app._omdb_status({"Response": "False", "Error": "Movie not found!"}), "no_result")

    def test_rating_details_reports_missing_source_reason(self) -> None:
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = False
        result = app.rating_details({"CommunityRating": 8.8, "CriticRating": 82})
        self.assertEqual(result["values"], {"豆瓣": "8.8", "RT": "82%"})
        self.assertEqual(result["details"]["IMDb"]["status"], "no_api_key")
        self.assertEqual(result["details"]["RT"]["source"], "emby.critic_rating")

    def test_original_title_can_find_omdb_match(self) -> None:
        app.OMDB_API_KEY = "test-key"
        item = {
            "Name": "中文片名",
            "OriginalTitle": "English Title",
            "ProductionYear": 2020,
            "Type": "Movie",
        }

        def fake_request(cache_key, params):
            if params.get("s") == "中文片名":
                return {"Response": "False", "Error": "Movie not found!"}
            if params.get("s") == "English Title":
                return {"Response": "True", "Search": [{"Title": "English Title", "Year": "2020", "Type": "movie", "imdbID": "tt7654321"}]}
            return {
                "Response": "True",
                "Ratings": [
                    {"Source": "Internet Movie Database", "Value": "7.6/10"},
                    {"Source": "Rotten Tomatoes", "Value": "84%"},
                ],
            }

        with patch.object(app, "_omdb_cached_request", side_effect=fake_request):
            result = app.rating_details(item)
        self.assertEqual(result["values"]["IMDb"], "7.6")
        self.assertEqual(result["values"]["RT"], "84%")
        self.assertEqual(result["details"]["IMDb"]["source"], "omdb.title_search")

    def test_rating_override_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            override_file = Path(directory) / "rating-overrides.json"
            override_file.write_text(
                '{"items":{"83737":{"douban":"8.9","imdb_id":"tt20119656","rt":"91%","reason":"verified"}}}',
                encoding="utf-8",
            )
            app.RATING_OVERRIDES_FILE = override_file
            app._RATING_OVERRIDES_MTIME = -1
            result = app.rating_details({"Id": "83737", "Name": "毛骗", "CommunityRating": 8.8})
        self.assertEqual(result["values"], {"豆瓣": "8.9", "RT": "91%"})
        self.assertEqual(result["details"]["豆瓣"]["status"], "override")
        self.assertEqual(app.rating_override_signature({"Id": "83737", "Name": "毛骗"}), "")

    def test_normalize_poster_canvas_preserves_tall_poster_content(self) -> None:
        image = Image.new("RGB", (754, 1280), "white")
        normalized = app.normalize_poster_canvas(image)
        self.assertEqual(normalized.size, (1000, 1500))
        self.assertEqual(normalized.getpixel((0, 0)), (11, 12, 13))

    def test_normalize_poster_canvas_keeps_standard_posters_unchanged(self) -> None:
        image = Image.new("RGB", (1000, 1500), "white")
        normalized = app.normalize_poster_canvas(image)
        self.assertEqual(normalized.size, (1000, 1500))

    def test_atomic_write_replaces_target_without_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            app.atomic_write_text(target, '{"ok": true}')
            self.assertEqual(target.read_text(encoding="utf-8"), '{"ok": true}')
            self.assertFalse(any(target.parent.glob("*.tmp")))

    def test_log_formatter_uses_beijing_time(self) -> None:
        import logging

        formatter = app.BeijingFormatter("%(asctime)s %(message)s", "Asia/Shanghai")
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
        record.created = 0
        self.assertTrue(formatter.formatTime(record).startswith("1970-01-01 08:00:00"))

    def test_compact_layout_with_no_ratings_is_empty_and_safe(self) -> None:
        self.assertEqual(app._rating_sources({}), [])

    def test_quality_only_layout_omits_rating_footer(self) -> None:
        self.assertEqual(app._rating_sources({"豆瓣": "8.2"}, "quality_only"), [])
        self.assertEqual(app._rating_sources({"豆瓣": "8.2"}, "none"), [])

    def test_media_health_detects_filename_quality_mismatch(self) -> None:
        item = {
            "Id": "health-1", "Name": "sample", "Type": "Series", "ImageTags": {"Primary": "tag"},
            "MediaSources": [{"Name": "sample.4K.HDR10.mkv", "Path": "sample.4K.HDR10.mkv", "MediaStreams": [
                {"Type": "Video", "Width": 1920, "Height": 1080, "DisplayTitle": "1080p AVC"},
                {"Type": "Audio", "Codec": "ac3", "DisplayTitle": "Japanese 5.1"},
                {"Type": "Subtitle", "Language": "eng", "DisplayTitle": "English"},
            ]}],
        }
        result = app.media_health(item)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("resolution_mismatch", codes)
        self.assertIn("hdr_mismatch", codes)
        self.assertIn("no_chinese_subtitle", codes)

    def test_media_health_does_not_report_clean_item(self) -> None:
        item = {
            "Id": "health-2", "Name": "clean", "Type": "Movie", "ImageTags": {"Primary": "tag"},
            "MediaStreams": [
                {"Type": "Video", "Width": 3840, "Height": 2160, "DisplayTitle": "4K HDR10"},
                {"Type": "Audio", "Codec": "eac3", "DisplayTitle": "Chinese Atmos"},
            ],
        }
        self.assertEqual(app.media_health(item)["issues"], [])

    def test_media_health_does_not_flag_remote_source_without_decoded_streams(self) -> None:
        item = {
            "Id": "health-3", "Name": "remote iso", "Type": "Movie",
            "ImageTags": {"Primary": "tag"},
            "Path": "/CloudNAS/movie.iso",
            "MediaSources": [{"Path": "/CloudNAS/movie.iso", "Container": "iso", "MediaStreams": []}],
        }
        codes = {issue["code"] for issue in app.media_health(item)["issues"]}
        self.assertNotIn("missing_video_stream", codes)
        self.assertNotIn("missing_audio_stream", codes)

    def test_library_is_resolved_by_longest_path_prefix(self) -> None:
        item = {"Path": "/media/movies/chinese/title/title.strm"}
        libraries = [
            {"id": "root", "name": "全部", "locations": ["/media"]},
            {"id": "movies", "name": "华语电影", "locations": ["/media/movies/chinese"]},
        ]
        self.assertEqual(app._library_for_item(item, libraries)["id"], "movies")

    def test_reference_score_layout_omits_missing_sources(self) -> None:
        self.assertEqual(
            [source[0] for source in app._rating_sources({"豆瓣": "8.2"})],
            ["豆瓣"],
        )
        self.assertEqual(
            [source[0] for source in app._rating_sources({"豆瓣": "8.2", "RT": "91%"})],
            ["豆瓣", "RT"],
        )
        self.assertEqual(
            [source[0] for source in app._rating_sources({"豆瓣": "8.2", "IMDb": "--", "RT": ""})],
            ["豆瓣"],
        )

    def test_current_render_preserves_overlay_style(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "rendered.jpg"
            Image.new("RGB", (1000, 1500), (120, 130, 140)).save(source, "JPEG")
            item = {
                "Id": "sample",
                "Name": "sample",
                "MediaStreams": [{"Type": "Video", "Width": 3840, "Height": 2160, "DisplayTitle": "4K HDR10"}],
                "MediaSources": [{"Name": "sample-4K-HDR10.mkv", "Path": "sample-4K-HDR10.mkv"}],
            }
            app.render(item, source, output, {"豆瓣": "8.2", "IMDb": "7.9"})
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (1000, 1500))
                self.assertLess(sum(rendered.getpixel((0, 0))), 80)
                self.assertNotEqual(rendered.getpixel((500, 1490)), (120, 130, 140))

    def test_worker_render_marker_is_detected(self) -> None:
        from io import BytesIO

        buffer = BytesIO()
        Image.new("RGB", (100, 150), "black").save(
            buffer,
            format="JPEG",
            comment=f"{app.WORKER_IMAGE_MARKER}1.6".encode("ascii"),
        )
        self.assertTrue(app.is_worker_rendered_image(buffer.getvalue()))

    def test_rating_details_can_refresh_only_one_source(self) -> None:
        app.DOUBAN_REQUIRE_VERIFIED_CACHE = False
        result = app.rating_details(
            {"CommunityRating": 8.8},
            refresh_sources={"豆瓣"},
            cached_values={"IMDb": "7.0", "RT": "80%"},
            cached_details={
                "IMDb": {"status": "ok", "source": "omdb.provider_id"},
                "RT": {"status": "ok", "source": "omdb.provider_id"},
            },
        )
        self.assertEqual(result["values"], {"IMDb": "7.0", "RT": "80%", "豆瓣": "8.8"})
        self.assertEqual(result["details"]["豆瓣"]["status"], "unverified")

    def test_rating_input_summary_counts_raw_fields(self) -> None:
        items = [
            {"CommunityRating": 8.8, "ProviderIds": {"Douban": "1", "Imdb": "tt1"}},
            {"CommunityRating": 7.0, "CriticRating": 82, "ProviderIds": {"Imdb": "tt2"}},
            {},
        ]
        self.assertEqual(
            app.rating_input_summary(items),
            {
                "items": 3,
                "community_rating": 2,
                "douban_provider_id": 1,
                "critic_rating": 1,
                "imdb_provider_id": 2,
            },
        )

    def test_update_rating_stats_counts_statuses(self) -> None:
        stats = app.Counter()
        app.update_rating_stats(stats, {
            "豆瓣": {"status": "ok"},
            "IMDb": {"status": "no_match"},
            "RT": {"status": "no_rating"},
        })
        self.assertEqual(stats["豆瓣:ok"], 1)
        self.assertEqual(stats["IMDb:no_match"], 1)
        self.assertEqual(stats["RT:no_rating"], 1)



    def test_library_page_filters_by_resolved_library_path(self) -> None:
        items = [
            {"Id": "a", "Name": "A", "Path": "/media/jp/a/a.strm", "Type": "Movie"},
            {"Id": "b", "Name": "B", "Path": "/media/cn/b/b.strm", "Type": "Movie"},
        ]
        libraries = [
            {"id": "jp", "name": "日韩电影", "locations": ["/media/jp"]},
            {"id": "cn", "name": "华语电影", "locations": ["/media/cn"]},
        ]
        with patch.object(app, "_library_definitions", return_value=libraries), patch.object(app, "get_items_cached", return_value=items):
            page, total = app.get_item_page(0, 24, "jp")
        self.assertEqual(total, 1)
        self.assertEqual([item["Id"] for item in page], ["a"])

    def test_mdblist_batch_normalizes_imdb_and_tomatoes_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_cache = app.CACHE_DIR
            app.CACHE_DIR = Path(directory)
            try:
                with patch.object(app, "_mdblist_request", return_value={
                    "ratings": [
                        {"provider_id": "tt0000001", "provider_rating": 7.4},
                        {"provider_id": "tt0000002", "provider_rating": 8.6},
                    ]
                }):
                    result = app._mdblist_fetch_batch("movie", "imdb", ["tt0000001", "tt0000002"])
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["ids"]["tt0000001"], "7.4")
                self.assertEqual(result["ids"]["tt0000002"], "8.6")
            finally:
                app.CACHE_DIR = old_cache

    def test_tomatoes_normalizer_requires_percentage_scale(self) -> None:
        self.assertIsNone(app._format_mdblist_rating("tomatoes", 8.6))
        self.assertEqual(app._format_mdblist_rating("tomatoes", 86), "86%")

    def test_mdblist_provider_label_is_not_saved_as_an_imdb_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_cache = app.CACHE_DIR
            app.CACHE_DIR = Path(directory)
            try:
                app._mdblist_write_values("movie", "tt0000001", "imdb", {"IMDb": "imdb"}, "ok")
                with patch.object(app, "_mdblist_request", return_value={
                    "provider_id": "imdb", "provider_rating": "imdb", "ratings": []
                }):
                    result = app._mdblist_fetch_batch("movie", "imdb", ["tt0000001"])
                cached = app._load_json_file(app._mdblist_cache_file("movie", "tt0000001"), {})
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["count"], 0)
                self.assertNotIn("IMDb", cached["values"])
                self.assertEqual(cached["statuses"]["imdb"], "no_rating")
                self.assertIsNone(app._format_imdb("imdb"))
            finally:
                app.CACHE_DIR = old_cache

    def test_external_rating_provider_uses_mdblist_before_omdb(self) -> None:
        item = {"Type": "Movie"}
        with patch.object(app, "_mdblist_cached_values", return_value=({"IMDb": "7.5"}, {"imdb": "ok"})), \
             patch.object(app, "_omdb_rating_values") as omdb:
            values, details = app._external_rating_values(item, "tt1234567", "provider_id", {"IMDb"})
        self.assertEqual(values, {"IMDb": "7.5"})
        self.assertEqual(details["IMDb"]["source"], "mdblist.provider_id")
        omdb.assert_not_called()

    def test_mdblist_request_uses_direct_session_without_proxy_environment(self) -> None:
        original_keys = app.MDBLIST_API_KEYS
        app.MDBLIST_API_KEYS = ["unit-test-key"]
        response = Mock(status_code=200, headers={}, json=lambda: {"ratings": []})
        try:
            with patch.object(app, "_mdblist_wait_for_slot"), \
                 patch.object(app.MDBLIST_SESSION, "post", return_value=response) as direct_post, \
                 patch.object(app.SESSION, "post") as proxy_session_post:
                payload = app._mdblist_request("/rating/movie/imdb", {"ids": ["tt0000001"], "provider": "imdb"})
            self.assertEqual(payload["ratings"], [])
            direct_post.assert_called_once()
            proxy_session_post.assert_not_called()
            self.assertFalse(app.MDBLIST_SESSION.trust_env)
        finally:
            app.MDBLIST_API_KEYS = original_keys

    def test_mdblist_request_falls_through_invalid_key_to_next_key(self) -> None:
        original_keys = app.MDBLIST_API_KEYS
        original_index = app.MDBLIST_KEY_INDEX
        app.MDBLIST_API_KEYS = ["invalid-key", "working-key"]
        app.MDBLIST_KEY_INDEX = 0
        invalid = Mock(status_code=401, headers={}, json=lambda: {"error": "Invalid API key"})
        working = Mock(status_code=200, headers={}, json=lambda: {"ratings": []})
        try:
            with patch.object(app, "_mdblist_wait_for_slot"), patch.object(app.MDBLIST_SESSION, "post", side_effect=[invalid, working]) as post:
                payload = app._mdblist_request("/rating/movie/imdb", {"ids": ["tt0000001"], "provider": "imdb"})
            self.assertEqual(payload["ratings"], [])
            self.assertEqual(post.call_count, 2)
        finally:
            app.MDBLIST_API_KEYS = original_keys
            app.MDBLIST_KEY_INDEX = original_index

    def test_mdblist_rating_cache_reads_values_for_requested_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_cache = app.CACHE_DIR
            app.CACHE_DIR = Path(directory)
            item = {"Type": "Movie"}
            try:
                app._mdblist_write_values("movie", "tt0000001", "imdb", {"IMDb": "7.4"}, "ok")
                app._mdblist_write_values("movie", "tt0000001", "tomatoes", {"RT": "82%"}, "ok")
                with patch.object(app, "_mdblist_fetch_batch") as lookup:
                    values, statuses = app._mdblist_cached_values(item, "tt0000001", {"IMDb", "RT"})
                self.assertEqual(values, {"IMDb": "7.4", "RT": "82%"})
                self.assertEqual(statuses, {"imdb": "ok", "tomatoes": "ok"})
                lookup.assert_not_called()
            finally:
                app.CACHE_DIR = old_cache

    def test_rating_provider_signature_change_forces_external_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_cache = app.CACHE_DIR
            old_keys = app.MDBLIST_API_KEYS
            app.CACHE_DIR = Path(directory)
            app.MDBLIST_API_KEYS = ["new-unit-key"]
            try:
                state_dir = app.CACHE_DIR / "state"
                state_dir.mkdir(parents=True)
                (state_dir / "item-1.json").write_text(json.dumps({
                    "ratings": {"IMDb": "7.0", "RT": "80%"},
                    "rating_details": {
                        "IMDb": {"status": "ok", "source": "omdb.provider_id"},
                        "RT": {"status": "ok", "source": "omdb.provider_id"},
                    },
                    "rating_checked_at": {"IMDb": time.time(), "RT": time.time()},
                    "rating_provider_signature": "old-provider-signature",
                }), encoding="utf-8")
                item = {"Id": "item-1", "Type": "Movie"}
                self.assertTrue(app._rating_prefetch_due(item, "IMDb", time.time()))
                self.assertTrue(app._rating_prefetch_due(item, "RT", time.time()))
            finally:
                app.CACHE_DIR = old_cache
                app.MDBLIST_API_KEYS = old_keys

    def test_api_key_pool_splits_and_deduplicates_values(self) -> None:
        self.assertEqual(app.split_api_keys("one, two\none;one"), ["one", "two"])

if __name__ == "__main__":
    unittest.main()

