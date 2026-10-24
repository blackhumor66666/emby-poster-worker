import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class RatingsDueRegressionTests(unittest.TestCase):
    """process() 曾经在任何条目上都抛 UnboundLocalError: ratings_due。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.item = {"Id": "42", "Type": "Movie", "Name": "测试片", "ImageTags": {}}
        state_dir = self.cache / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "42.json").write_text(
            json.dumps({"signature": "SIG", "image_tag": ""}), encoding="utf-8"
        )
        self.patches = [
            patch.object(app, "CACHE_DIR", self.cache),
            patch.object(app, "SEASON_POSTERS_ENABLED", False),
            patch.object(app, "signature", return_value="SIG"),
            patch.object(app, "media_signature", return_value="MEDIA"),
            patch.object(app, "rating_identity", return_value={"id": "x"}),
            patch.object(app, "rating_identity_matches", return_value=True),
            patch.object(app, "rating_override_signature", return_value="OVR"),
            patch.object(app, "rating_provider_signature", return_value="PROV"),
            patch.object(app, "community_rating_changed", return_value=False),
            patch.object(app, "_douban_cache_result", return_value={"status": "ok"}),
            patch.object(app, "_rating_override", return_value={}),
            patch.object(
                app,
                "rating_details",
                return_value={
                    "values": {"豆瓣": "8.1", "IMDb": "7.5", "RT": "90%"},
                    "details": {
                        "豆瓣": {"status": "ok", "source": "douban.cache"},
                        "IMDb": {"status": "ok", "source": "omdb"},
                        "RT": {"status": "ok", "source": "omdb"},
                    },
                },
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_due_ratings_do_not_crash_before_the_unchanged_shortcut(self):
        # 旧代码在这里抛 UnboundLocalError，导致每小时扫描每个条目都失败。
        self.assertEqual(app.process(self.item), "unchanged")
        state = json.loads((self.cache / "state" / "42.json").read_text(encoding="utf-8"))
        self.assertEqual(state["ratings"]["豆瓣"], "8.1")


if __name__ == "__main__":
    unittest.main()
