"""Guards that keep the rating printed on a poster tied to live identity.

A cached score must never outlive the identity it came from, and a local
Douban cache value must win over a weeks-old cached number.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(tempfile.mkdtemp(prefix="poster-stale-cache-"))
DOUBAN = CACHE / "douban"
os.environ["CACHE_DIR"] = str(CACHE)
os.environ["ADMIN_DATA_DIR"] = str(tempfile.mkdtemp(prefix="poster-stale-admin-"))
os.environ["DOUBAN_CACHE_DIR"] = str(DOUBAN)
os.environ["PUBLIC_BASE_URL"] = "http://localhost"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

# Other test modules may import app first with their own temp cache dir, so
# point the resolver at this module's fixture directory explicitly.
app.DOUBAN_CACHE_DIR = DOUBAN


def make_item(ids, name="Test", year="2000", item_type="Movie"):
    return {
        "Id": "1",
        "Name": name,
        "Type": item_type,
        "ProductionYear": int(year),
        "ProviderIds": ids,
        "MediaSources": [{"MediaStreams": []}],
    }


class RatingStaleGuardTests(unittest.TestCase):
    def test_cached_douban_score_is_dropped_when_ids_are_gone(self):
        item = make_item({})
        result = app.rating_details(
            item, set(), {"豆瓣": "7.0", "IMDb": "7.1"},
            {"豆瓣": {"status": "ok", "source": "douban.local_cache", "douban_id": "1"}},
        )
        self.assertNotIn("豆瓣", result["values"])

    def test_cached_douban_score_refreshes_from_local_cache(self):
        target = DOUBAN / "cache" / "douban-movies" / "1292352_tt0181875"
        target.mkdir(parents=True, exist_ok=True)
        (target / "all.json").write_text(json.dumps({
            "id": "1292352", "title": "Almost Famous", "year": "2000",
            "rating": {"value": 8.4},
        }), encoding="utf-8")
        item = make_item({"Douban": "1292352", "Imdb": "tt0181875"}, name="Almost Famous")
        result = app.rating_details(
            item, set(), {"豆瓣": "7.0"},
            {"豆瓣": {"status": "ok", "source": "douban.local_cache", "douban_id": "1292352"}},
        )
        self.assertEqual(result["values"]["豆瓣"], "8.4")

    def test_douban_score_kept_when_cache_is_transiently_unavailable(self):
        item = make_item({"Douban": "999999"})
        previous = {"status": "ok", "source": "douban.local_cache", "douban_id": "999999",
                    "emby_douban_id": "999999"}
        result = app.rating_details(item, set(), {"豆瓣": "7.2"}, {"豆瓣": previous})
        # A cache miss that is not definitive must not erase a verified score.
        self.assertIn("豆瓣", result["values"])


if __name__ == "__main__":
    unittest.main()
