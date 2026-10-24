import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxset_covers


class Response:
    def __init__(self, payload=None, content=b""):
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload


class FakeEmby:
    """只实现 boxset_covers 用到的那几个接口。"""

    def __init__(self, boxsets, children=None, fixable=None, fail_refresh=()):
        self.boxsets = boxsets
        self.children = children or {}
        self.fixable = set(fixable or [])
        self.fail_refresh = set(fail_refresh)
        self.refreshed = []
        self.uploads = []

    def __call__(self, method, path, **kwargs):
        params = kwargs.get("params") or {}
        if method == "GET" and path == "/Items":
            if params.get("IncludeItemTypes") == "BoxSet":
                return Response({"Items": [dict(item) for item in self.boxsets]})
            parent = params.get("ParentId")
            return Response({"Items": self.children.get(parent, [])})
        if method == "POST" and path.endswith("/Refresh"):
            item_id = path.split("/")[2]
            if item_id in self.fail_refresh:
                raise RuntimeError("refresh boom")
            self.refreshed.append(item_id)
            for item in self.boxsets:
                if item["Id"] == item_id and item_id in self.fixable:
                    item.setdefault("ImageTags", {})["Primary"] = "tag-" + item_id
            return Response()
        if method == "GET" and path.endswith("/Images/Primary"):
            return Response(content=b"member-poster")
        if method == "POST" and path.endswith("/Images/Primary"):
            item_id = path.split("/")[2]
            self.uploads.append(item_id)
            for item in self.boxsets:
                if item["Id"] == item_id:
                    item.setdefault("ImageTags", {})["Primary"] = "uploaded"
            return Response()
        raise AssertionError(f"unexpected call {method} {path}")


def boxset(item_id, name, has_image=False):
    item = {"Id": item_id, "Name": name}
    if has_image:
        item["ImageTags"] = {"Primary": "tag"}
    return item


class BoxsetCoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "boxset-covers.json"
        patcher = patch.multiple(boxset_covers, SETTLE_SECONDS=0, SETTLE_POLL_SECONDS=0, SETTLE_MAX_SECONDS=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_backfill(self, emby, **kwargs):
        kwargs.setdefault("log", None)
        return boxset_covers.backfill(emby, self.state, **kwargs)

    def test_boxset_without_cover_is_refreshed_and_reported_fixed(self):
        emby = FakeEmby([boxset("1", "有图", True), boxset("2", "缺图")], fixable={"2"})
        result = self.run_backfill(emby)
        self.assertEqual(emby.refreshed, ["2"])
        self.assertEqual(result["missing_before"], 1)
        self.assertEqual(result["fixed"], 1)
        self.assertEqual(result["still_missing"], 0)
        self.assertEqual(result["missing_after"], 0)

    def test_all_covered_boxsets_trigger_no_request(self):
        emby = FakeEmby([boxset("1", "有图", True), boxset("2", "也有图", True)])
        result = self.run_backfill(emby)
        self.assertEqual(emby.refreshed, [])
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(result["missing_before"], 0)

    def test_dry_run_touches_nothing(self):
        emby = FakeEmby([boxset("2", "缺图")], fixable={"2"})
        result = self.run_backfill(emby, dry_run=True)
        self.assertEqual(emby.refreshed, [])
        self.assertEqual(result["items"][0]["status"], "would_refresh")
        self.assertFalse(self.state.exists())

    def test_member_poster_is_reused_when_tmdb_has_no_collection_poster(self):
        emby = FakeEmby(
            [boxset("2", "缺图")],
            children={"2": [boxset("child", "成员影片", True)]},
            fixable=set(),
        )
        result = self.run_backfill(emby)
        self.assertEqual(emby.uploads, ["2"])
        self.assertEqual(result["fallback"], 1)
        self.assertEqual(result["still_missing"], 0)
        self.assertEqual(result["missing_after"], 0)

    def test_member_fallback_can_be_disabled(self):
        emby = FakeEmby([boxset("2", "缺图")], children={"2": [boxset("child", "成员", True)]})
        result = self.run_backfill(emby, member_fallback=False)
        self.assertEqual(emby.uploads, [])
        self.assertEqual(result["still_missing"], 1)

    def test_recent_failed_attempt_is_skipped_until_retry_window(self):
        emby = FakeEmby([boxset("2", "缺图")])
        self.run_backfill(emby)
        self.assertEqual(emby.refreshed, ["2"])
        emby.refreshed.clear()
        again = self.run_backfill(emby)
        self.assertEqual(emby.refreshed, [])
        self.assertEqual(again["skipped_recent"], 1)

    def test_force_ignores_retry_window(self):
        emby = FakeEmby([boxset("2", "缺图")])
        self.run_backfill(emby)
        emby.refreshed.clear()
        self.run_backfill(emby, force=True)
        self.assertEqual(emby.refreshed, ["2"])

    def test_retry_window_expires(self):
        emby = FakeEmby([boxset("2", "缺图")])
        self.run_backfill(emby)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["items"]["2"]["last_attempt_ts"] = time.time() - 8 * 86400
        self.state.write_text(json.dumps(state), encoding="utf-8")
        emby.refreshed.clear()
        self.run_backfill(emby)
        self.assertEqual(emby.refreshed, ["2"])

    def test_single_refresh_error_does_not_abort_the_batch(self):
        emby = FakeEmby([boxset("2", "缺图"), boxset("3", "也缺图")], fixable={"3"}, fail_refresh={"2"})
        result = self.run_backfill(emby)
        self.assertEqual(emby.refreshed, ["3"])
        self.assertEqual(result["fixed"], 1)
        statuses = {entry["id"]: entry["status"] for entry in result["items"]}
        self.assertEqual(statuses["2"], "error")


if __name__ == "__main__":
    unittest.main()
