import json,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,admin_snapshot,issue_policy

class SnapshotTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'view.json'
 def tearDown(self):self.temp.cleanup()
 def create(self,build):return admin_snapshot.Snapshot(self.path,build,app.atomic_write_text,'test',ttl=60)
 def ready(self,s):
  deadline=time.monotonic()+2
  while time.monotonic()<deadline:
   data,meta=s.read()
   if not meta['refreshing']:return data
   threading.Event().wait(.01)
  self.fail('build stuck')
 def test_cold_read_never_blocks_on_slow_build(self):
  gate=threading.Event();entered=threading.Event()
  def build():entered.set();gate.wait(1);return {'value':1}
  s=self.create(build);t=time.monotonic();data,meta=s.read();elapsed=time.monotonic()-t
  self.assertIsNone(data);self.assertTrue(meta['refreshing']);self.assertLess(elapsed,.2);self.assertTrue(entered.wait(1));gate.set();self.assertEqual(self.ready(s),{'value':1})
 def test_concurrent_reads_share_one_build(self):
  gate=threading.Event();calls=[]
  def build():calls.append(1);gate.wait(1);return {'value':1}
  s=self.create(build)
  for _ in range(20):s.read()
  gate.set();self.ready(s);self.assertEqual(len(calls),1)
 def test_stale_data_survives_refresh_and_restart(self):
  s=self.create(lambda:{'value':1});s.read();self.ready(s)
  loaded=self.create(lambda:{'value':2});self.assertEqual(loaded.read()[0],{'value':1})
  loaded.invalidate();data,meta=loaded.read();self.assertEqual(data,{'value':1});self.assertTrue(meta['refreshing']);self.assertEqual(self.ready(loaded),{'value':2})
 def test_failure_keeps_last_good_snapshot(self):
  s=self.create(lambda:{'value':1});s.read();self.ready(s)
  def fail():raise ValueError('upstream unavailable')
  s.build=fail;s.invalidate();s.read()
  while s.running:threading.Event().wait(.01)
  with s.lock:self.assertEqual(s.data,{'value':1});self.assertEqual(s.error,'upstream unavailable')
 def test_version_mismatch_does_not_reuse_old_classification(self):
  self.path.write_text(json.dumps({'version':'old','updated':time.time(),'data':{'value':1}}))
  s=self.create(lambda:{'value':2});self.assertIsNone(s.data)

class WaitStateTests(unittest.TestCase):
 def classify(self,item=None,work=None,result=None):
  return issue_policy.classify(item or {'Id':'1','Type':'Movie','ProviderIds':{'Douban':'123'},'_binding_at':time.time()-7*3600},{'ratings':{'IMDb':'8.1'}},result or {'status':'missing_cache_file'},['missing_imdb_id'],['poster_missing'],time.time(),work=work or {'status':'succeeded','action':'render'},handled=True)
 def test_six_hour_cache_wait_becomes_explicit_blocked_reference(self):
  r=self.classify();self.assertEqual(r['status'],'info');self.assertEqual(r['issues'][0]['code'],'cache_blocked')
 def test_no_douban_binding_is_not_a_permanent_running_job(self):
  r=self.classify(item={'Id':'1','Type':'Movie','ProviderIds':{'Imdb':'tt123'}});self.assertEqual(r['status'],'info');self.assertEqual(r['issues'][0]['code'],'optional_douban')
 def test_recent_cache_wait_is_not_render_running(self):
  r=self.classify(item={'Id':'1','Type':'Movie','ProviderIds':{'Douban':'123'},'_binding_at':time.time()});self.assertEqual(r['status'],'waiting');self.assertEqual(r['issues'][0]['code'],'cache_wait')
 def test_active_job_keeps_actual_running_state(self):
  r=self.classify(work={'status':'running','action':'render'});self.assertEqual(r['status'],'waiting');self.assertEqual(r['issues'][0]['code'],'background_pending')
 def test_no_season_artwork_does_not_wait_for_impossible_render(self):
  r=issue_policy.classify({'Id':'s','Type':'Season','ImageTags':{}},{'ratings':{}},{'status':'ok','value':'8.1'},[],['douban_stale','poster_missing'],time.time());self.assertEqual(r['status'],'info');self.assertTrue(all(i['bucket']=='info'for i in r['issues']))
 def test_brief_card_never_calls_rich_diagnostics_or_external_queries(self):
  with patch.object(app,'_safe_item_state',return_value={'ratings':{'IMDb':'8.1'}}),patch.object(app,'_library_for_item',return_value={'name':'Movies'}),patch.object(app,'_item_triage',side_effect=AssertionError('no fresh audit')),patch.object(app,'_douban_cache_result',side_effect=AssertionError('no cache audit')):
   card=app._library_card({'Id':'1','Name':'Movie','Type':'Movie'});self.assertEqual(card['ratings']['IMDb'],'8.1')

if __name__=='__main__':unittest.main()
