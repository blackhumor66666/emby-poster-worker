import sys,time,threading,unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,issue_policy

class QueueCacheTests(unittest.TestCase):
    def setUp(self):app._invalidate_matching_queue()
    def tearDown(self):app._invalidate_matching_queue()
    def test_concurrent_read_builds_one_snapshot(self):
        def build():time.sleep(.03);return {"items":[],"counts":{}}
        with patch.object(app,"_build_matching_queue_payload",side_effect=build) as mock:
            with ThreadPoolExecutor(max_workers=6) as pool:
                rows=list(pool.map(lambda _:app._matching_queue_payload(),range(6)))
            self.assertEqual(mock.call_count,1)
            self.assertTrue(all(row is rows[0] for row in rows))
    def test_expiry_refresh_and_invalidation(self):
        with patch.object(app,"_build_matching_queue_payload",return_value={"items":[]}) as mock:
            app._matching_queue_payload();app._matching_queue_payload()
            self.assertEqual(mock.call_count,1)
            app.MATCHING_QUEUE_CACHE_AT-=app.MATCHING_QUEUE_TTL+1;app._matching_queue_payload()
            app._matching_queue_payload(force=True)
            app._invalidate_matching_queue();app._matching_queue_payload()
            self.assertEqual(mock.call_count,4)
    def test_request_memo_does_not_survive_next_snapshot(self):
        app.DOUBAN_RESOLVE_CONTEXT.memo={}
        item={"Id":"1","ProviderIds":{"Douban":"123"},"Type":"Series"}
        try:
            with patch.object(app,"_resolve_douban_cache_result",return_value={"status":"ok","value":"8.1"}) as mock:
                app._douban_cache_result(item);app._douban_cache_result(item)
                self.assertEqual(mock.call_count,1)
                app._douban_cache_result({**item,"ProviderIds":{"Douban":"124"}})
                self.assertEqual(mock.call_count,2)
        finally:del app.DOUBAN_RESOLVE_CONTEXT.memo
        with patch.object(app,"_resolve_douban_cache_result",return_value={"status":"ok","value":"8.2"}) as mock:
            self.assertEqual(app._douban_cache_result(item)["value"],"8.2")
    def test_season_missing_id_is_reference_not_series_match(self):
        result=issue_policy.classify({"Id":"s","Type":"Season","ProviderIds":{}},{"ratings":{"豆瓣":"8.1"}}, {"status":"ok"},["missing_douban_id"],[],time.time())
        self.assertFalse(any(r["action"]=="match" for r in result["issues"]))
        result=issue_policy.classify({"Id":"s","Type":"Season","ProviderIds":{}},{"ratings":{}},{"status":"missing_season_mapping"},["missing_douban_id"],[],time.time())
        self.assertEqual(result["status"],"info")
