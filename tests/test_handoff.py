import sys,json,time,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from contextlib import ExitStack
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,issue_policy,render_queue

class HandoffPolicyTests(unittest.TestCase):
 def classify(self,work=None,handled=False,codes=None,result=None,item=None):
  return issue_policy.classify(item or {'Id':'1','Type':'Series','ProviderIds':{'Douban':'123'}},{'ratings':{}},result or {'status':'ok','value':'8.1'},['missing_imdb_id'],codes or [],time.time(),work=work,handled=handled)
 def test_confirmed_work_leaves_action_before_cache_is_ready(self):
  r=self.classify(handled=True,codes=['poster_missing'],result={'status':'missing_cache_file'})
  self.assertEqual(r['status'],'waiting');self.assertEqual([i['code']for i in r['issues']],['cache_wait'])
 def test_cache_ready_after_first_task_does_not_request_second_human_click(self):
  r=self.classify(work={'status':'succeeded','action':'render'},handled=True,codes=['douban_stale'])
  self.assertEqual(r['status'],'waiting');self.assertNotIn('poster_score_stale',[i['code']for i in r['issues']])
 def test_active_task_moves_render_issues_out_of_action(self):
  r=self.classify(work={'status':'queued','action':'render'},codes=['poster_missing','douban_stale'])
  self.assertEqual(r['status'],'waiting');self.assertEqual(len(r['issues']),1)
 def test_failed_work_returns_with_explicit_reason_and_preserves_binding(self):
  r=self.classify(work={'status':'failed','action':'render','error':'没有原海报'},handled=True,codes=['poster_missing'])
  self.assertEqual(r['status'],'action');self.assertEqual(r['issues'][0]['code'],'background_failed');self.assertIn('没有原海报',r['issues'][0]['description'])
 def test_new_identity_conflict_is_never_hidden_by_old_success(self):
  r=self.classify(work={'status':'succeeded','action':'render'},handled=True,result={'status':'identity_mismatch'},codes=['poster_missing'])
  self.assertEqual(r['status'],'action');self.assertEqual(r['issues'][0]['code'],'identity_conflict')
 def test_artless_season_is_reference_with_specific_reason(self):
  r=self.classify(item={'Id':'s','Type':'Season','IndexNumber':0,'ProviderIds':{}},codes=['poster_missing'])
  self.assertEqual(r['status'],'info');self.assertIn('season_artwork',[i['code']for i in r['issues']])
 def test_real_season_poster_stale_is_waiting_not_another_match_request(self):
  r=self.classify(item={'Id':'s','Type':'Season','ImageTags':{'Primary':'tag'},'ProviderIds':{}},codes=['douban_stale'])
  self.assertEqual(r['status'],'waiting')
 def test_render_without_match_does_not_hide_missing_identity(self):
  r=self.classify(work={'status':'queued','action':'render'},item={'Id':'1','Type':'Movie','ProviderIds':{}},result={'status':'missing_ids'})
  self.assertEqual(r['status'],'action');self.assertIn('missing_identity',[i['code']for i in r['issues']])
 def test_success_without_remaining_issue_is_healthy(self):
  r=issue_policy.classify({'Id':'1','Type':'Movie','ProviderIds':{'Douban':'123','Imdb':'tt123'}},{'ratings':{'豆瓣':'8.1'}},{'status':'ok'},[],[],time.time(),work={'status':'succeeded','action':'render'},handled=True)
  self.assertEqual(r['status'],'healthy')

class CacheFollowupTests(unittest.TestCase):
 def setUp(self):
  self.stack=ExitStack();self.item={'Id':'1','Name':'Show','Type':'Series','ProviderIds':{'Douban':'123'}}
  self.queue=Mock();self.queue.latest_items.return_value={'1':{'status':'succeeded','action':'render'}}
  self.stack.enter_context(patch.object(app,'_work_queue',return_value=self.queue))
  self.stack.enter_context(patch.object(app,'_load_rating_overrides',return_value={'1':{'match':{'confirmed_at':'2026-10-14'}}}))
  self.stack.enter_context(patch.object(app,'get_items_cached',return_value=[self.item]))
  self.stack.enter_context(patch.object(app,'_rating_override',return_value={}))
  self.stack.enter_context(patch.object(app,'_douban_cache_result',return_value={'status':'ok','value':'8.1','cache_updated_at':123}))
  self.stack.enter_context(patch.object(app,'_safe_item_state',return_value={'ratings':{},'rating_details':{'豆瓣':{'source':'douban.cache'}}}))
  self.enqueue=self.stack.enter_context(patch.object(app,'_enqueue_item'))
 def tearDown(self):self.stack.close()
 def test_late_cache_automatically_creates_followup(self):
  self.assertEqual(app._reconcile_cache_jobs(),1);self.enqueue.assert_called_once_with(self.item)
 def test_active_or_failed_jobs_are_not_duplicated_or_automatically_retried(self):
  for status in ['queued','running','failed']:
   self.queue.latest_items.return_value={'1':{'status':status,'action':'render'}}
   self.assertEqual(app._reconcile_cache_jobs(),0)
  self.enqueue.assert_not_called()
 def test_applied_cache_does_not_create_infinite_tasks(self):
  with patch.object(app,'_safe_item_state',return_value={'ratings':{'豆瓣':'8.1'},'rating_details':{'豆瓣':{'source':'douban.local_cache','cache_updated_at':123}}}):self.assertEqual(app._reconcile_cache_jobs(),0)
  self.enqueue.assert_not_called()
 def test_missing_cache_does_not_create_repeated_render_jobs(self):
  with patch.object(app,'_douban_cache_result',return_value={'status':'missing_cache_file'}):self.assertEqual(app._reconcile_cache_jobs(),0)
  self.enqueue.assert_not_called()
 def test_manual_score_is_not_overridden_by_followup(self):
  with patch.object(app,'_rating_override',return_value={'douban':'7.1'}):self.assertEqual(app._reconcile_cache_jobs(),0)
  self.enqueue.assert_not_called()

class ReceiptTests(unittest.TestCase):
 def test_old_receipt_survives_history_pruning_and_restart(self):
  with tempfile.TemporaryDirectory()as root:
   path=Path(root)/'jobs.json';q=render_queue.RenderQueue(path,threading.Lock(),lambda job:'updated')
   first=q.enqueue('first',context={'identity':'abc'})
   # Complete jobs without invoking a live media server.
   with q.condition:
    q.jobs[0]['status']='succeeded';q._save()
    for n in range(105):
     job=q.enqueue(str(n));next(j for j in q.jobs if j['id']==job['id'])['status']='succeeded'
    q._save()
   loaded=render_queue.RenderQueue(path,threading.Lock(),lambda job:'updated')
   self.assertEqual(loaded.latest_for('first')['id'],first['id']);self.assertEqual(loaded.latest_for('first')['context']['identity'],'abc');self.assertEqual(loaded.latest_for('first')['status'],'succeeded')
 def test_season_names_include_parent_and_special_label(self):
  item={'Id':'s','Name':'第 0 季','Type':'Season','ParentId':'p','IndexNumber':0}
  self.assertEqual(app._queue_item_name(item,{'p':{'Name':'全职高手'}}),'全职高手 · 特别篇')
 def test_season_name_falls_back_to_its_path_without_network(self):
  self.assertEqual(app._queue_item_name({'Type':'Season','Name':'第 1 季','Path':'/media/Show (2020)/Season 1'}),'Show · 第 1 季')

if __name__=='__main__':unittest.main()
