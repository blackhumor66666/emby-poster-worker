import time,json,tempfile,sys
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch,Mock
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,issue_policy,search_sources,media_match

class TriageTests(unittest.TestCase):
 def classification(self,days=10,score=None,status='missing_ids',gaps=None,codes=None,state=None,decisions=None):
  now=1800000000
  item={'Id':'1','Type':'Series','Name':'Show','DateCreated':datetime.fromtimestamp(now-days*86400,timezone.utc).isoformat(),'ProviderIds':{}}
  state=state or {'ratings':score or {},'rating_details':{}}
  return issue_policy.classify(item,state,{'status':status},gaps if gaps is not None else ['missing_both_ids'],codes or [],now,48,decisions)
 def test_new_arrival_waits(self):
  r=self.classification(days=.2);self.assertEqual(r['status'],'waiting');self.assertEqual(r['counts']['action'],0)
 def test_old_missing_ids_action(self):
  self.assertEqual(self.classification()['status'],'action')
 def test_bad_identity_immediate_even_new(self):
  r=self.classification(days=.1,status='identity_mismatch');self.assertEqual(r['status'],'action')
 def test_existing_scores_optional_id_is_reference(self):
  r=self.classification(score={'豆瓣':'8.1'},gaps=['missing_imdb_id'],status='ok');self.assertEqual(r['status'],'info')
 def test_ids_present_cache_wait_not_match(self):
  r=self.classification(gaps=[],status='missing_cache_file');self.assertEqual(r['status'],'waiting');self.assertEqual(r['issues'][0]['action'],'refresh')
 def test_no_rating_is_not_failure(self):
  r=self.classification(gaps=[],status='json_without_rating');self.assertEqual(r['status'],'info')
 def test_dismiss_restorable(self):
  r=self.classification();issue=r['issues'][0]
  decision={issue['code']:{'mode':'dismiss','fingerprint':issue['fingerprint']}}
  self.assertEqual(self.classification(decisions=decision)['status'],'ignored')
  self.assertEqual(self.classification()['status'],'action')
 def test_changed_condition_reappears(self):
  r=self.classification();i=r['issues'][0]
  decision={i['code']:{'mode':'dismiss','fingerprint':i['fingerprint']}}
  self.assertEqual(self.classification(status='missing_cache_file',decisions=decision)['status'],'action')
 def test_redraw_not_new_arrival(self):
  r=self.classification(state={'rendered_at':datetime.now(timezone.utc).isoformat(),'ratings':{}});self.assertFalse(r['new_arrival'])
 def test_unknown_creation_does_not_hide_old(self):
  r=issue_policy.classify({'Id':'1','DateModified':datetime.now(timezone.utc).isoformat()},{},{'status':'missing_ids'},['missing_both_ids'],[],time.time());self.assertEqual(r['status'],'action')
 def test_network_failure_wait(self):
  r=self.classification(gaps=[],status='json_without_rating',state={'ratings':{'豆瓣':'8.1'},'rating_details':{'IMDb':{'status':'rate_limited'}}});self.assertEqual(r['status'],'waiting')

class OnlineSearchTests(unittest.TestCase):
 def setUp(self):search_sources._CACHE.clear()
 def test_douban_series_type_uses_episode_count(self):
  session=Mock();session.get.return_value=Mock(status_code=200,json=lambda:[{'id':'123','title':'九门','year':'2026','type':'movie','episode':'30','img':'https://img.example/a.jpg'},{'id':'456','title':'九门','year':'2021','type':'movie','episode':''}])
  r=search_sources.douban_suggest(session,'九门','Series');self.assertEqual(len(r['items']),2);self.assertEqual(r['items'][0]['douban_id'],'123');self.assertTrue(r['items'][1]['type_unverified'])
 def test_douban_limited_no_repeated_requests(self):
  session=Mock();session.get.return_value=Mock(status_code=403)
  a=search_sources.douban_suggest(session,'x','Series');b=search_sources.douban_suggest(session,'x','Series');self.assertEqual(a['status'],'rate_limited');self.assertEqual(session.get.call_count,1)
 def test_tmdb_only_candidate_not_bindable(self):
  r=search_sources.combine([],[],[{'tmdb_id':'2','title':'Show','year':'2020','imdb_id':None,'douban_id':None}],{'ProviderIds':{}})
  self.assertFalse(r[0]['bindable'])
 def test_chinese_same_title_not_automatic_pair(self):
  rows=search_sources.combine([],[{'douban_id':'123','imdb_id':None,'title':'Show','year':'2020'}],[{'tmdb_id':'1','imdb_id':'tt1','douban_id':None,'title':'Show','year':'2020'}],{'ProviderIds':{}})
  self.assertEqual(len(rows),2);self.assertIsNone(rows[0]['imdb_id'])
 def test_imdb_key_failover(self):
  with tempfile.TemporaryDirectory() as root,patch.object(app,'CACHE_DIR',Path(root)),patch.object(app,'OMDB_API_KEY','bad'),patch.object(app,'OMDB_API_KEYS',['bad','good']),patch.object(app,'_omdb_wait_for_slot'),patch.object(app.SESSION,'get',side_effect=[Mock(status_code=401,json=lambda:{'Response':'False'}),Mock(status_code=200,json=lambda:{'Response':'True','Search':[]})]) as call:
   r=app._omdb_cached_request('key-rotation',{'s':'Show'},interactive=True)
   self.assertEqual(r['Response'],'True');self.assertEqual(call.call_count,2)
 def test_interactive_old_failure_cache_not_stuck(self):
  with tempfile.TemporaryDirectory() as root,patch.object(app,'CACHE_DIR',Path(root)),patch.object(app,'OMDB_API_KEY','good'),patch.object(app,'OMDB_API_KEYS',[]),patch.object(app,'_omdb_wait_for_slot'),patch.object(app.SESSION,'get',return_value=Mock(status_code=200,json=lambda:{'Response':'True','Search':[]})) as call:
   p=Path(root)/'omdb'/'f.json';p.parent.mkdir();p.write_text(json.dumps({'Response':'False','_worker_status':'lookup_error'}));import os;os.utime(p,(1,1))
   app._omdb_cached_request('f',{'s':'Show'},interactive=True);call.assert_called_once()

class IssueActionTests(unittest.TestCase):
 def test_issue_dismiss_and_restore_persist_without_scores_changes(self):
  with tempfile.TemporaryDirectory() as root,patch.object(app,'CACHE_DIR',Path(root)):
   item={'Id':'1','Name':'Old','Type':'Series','ProviderIds':{}}
   state={'ratings':{},'rating_details':{}}
   file=Path(root)/'state'/'1.json';file.parent.mkdir();file.write_text(json.dumps(state))
   before=file.read_bytes()
   with patch.object(app,'_douban_cache_result',return_value={'status':'missing_ids'}),patch.object(app,'media_health',return_value={'issues':[]}):
    triage=app._item_triage(item)
    issue=next(i for i in triage['issues'] if i['code']=='missing_identity')
    app._issue_action(item,{'action':'dismiss','code':'missing_identity'})
    self.assertEqual(next(i for i in app._item_triage(item)['issues'] if i['code']=='missing_identity')['bucket'],'ignored')
    app._issue_action(item,{'action':'restore','code':'missing_identity'})
    self.assertEqual(next(i for i in app._item_triage(item)['issues'] if i['code']=='missing_identity')['bucket'],'action')
   self.assertEqual(before,file.read_bytes())
 def test_refresh_runs_due_marking_inside_queue_without_clearing_scores(self):
  with tempfile.TemporaryDirectory() as root,patch.object(app,'CACHE_DIR',Path(root)),patch.object(app,'_enqueue_item',return_value={'id':'job'}) as enqueue:
   file=Path(root)/'state'/'1.json';file.parent.mkdir();file.write_text(json.dumps({'ratings':{'豆瓣':'7.0'},'rating_checked_at':{'豆瓣':100}}))
   result=app._issue_action({'Id':'1'},{'action':'refresh'})
   enqueue.assert_called_once_with({'Id':'1'},refresh=True)
   self.assertTrue(result['queued'])
   self.assertEqual(json.loads(file.read_text())['rating_checked_at']['豆瓣'],100)
   with patch.object(app,'_get_item_by_id',return_value={'Id':'1'}),patch.object(app,'process',return_value='updated'):
    app._execute_item_job({'item_id':'1','action':'render','refresh':True})
   state=json.loads(file.read_text());self.assertEqual(state['rating_checked_at']['豆瓣'],0);self.assertEqual(state['ratings']['豆瓣'],'7.0')

if __name__=='__main__':unittest.main()
