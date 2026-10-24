"""Regression coverage for durable work, direct IDs and type corrections."""
import copy,json,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,render_queue,search_sources,media_match,issue_policy

class DurableWorkTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'jobs.json';self.queues=[]
 def tearDown(self):
  for queue in self.queues:queue.close()
  self.temp.cleanup()
 def queue(self,execute=lambda job:'updated',lock=None):
  q=render_queue.RenderQueue(self.path,lock or threading.Lock(),execute);self.queues.append(q);return q
 def await_idle(self,q):
  deadline=time.monotonic()+3
  while time.monotonic()<deadline:
   if not q.snapshot()['active']:return
   threading.Event().wait(.01)
  self.fail('queue did not finish')
 def test_busy_scan_accepts_and_preserves_order(self):
  lock=threading.Lock();lock.acquire();seen=[];q=self.queue(lambda j:seen.append(j['item_id']) or 'updated',lock)
  q.start();q.enqueue('a');q.enqueue('b');self.assertEqual(q.snapshot()['counts']['queued'],2);self.assertEqual(seen,[])
  lock.release();self.await_idle(q);self.assertEqual(seen,['a','b'])
 def test_pending_edits_merge_latest_action_and_refresh(self):
  q=self.queue();first=q.enqueue('a',action='render',refresh=True);second=q.enqueue('a',name='latest')
  self.assertEqual(first['id'],second['id']);self.assertTrue(second['refresh']);self.assertEqual(len(q.snapshot()['active']),1)
  restore=q.enqueue('a',action='restore');self.assertFalse(restore['refresh']);self.assertEqual(restore['action'],'restore')
 def test_running_edit_gets_one_followup(self):
  entered=threading.Event();release=threading.Event();seen=[]
  def execute(job):
   seen.append(job);entered.set();release.wait(2);return 'updated'
  q=self.queue(execute);q.enqueue('a');q.start();self.assertTrue(entered.wait(1))
  q.enqueue('a');q.enqueue('a',refresh=True);self.assertEqual(q.snapshot()['counts']['queued'],1)
  release.set();self.await_idle(q);self.assertEqual(len(seen),2);self.assertTrue(seen[1]['refresh'])
 def test_restart_recovers_interrupted_and_waiting_jobs(self):
  q=self.queue();q.enqueue('a');q.enqueue('b');payload=json.loads(self.path.read_text());payload['jobs'][0]['status']='running';self.path.write_text(json.dumps(payload))
  seen=[];recovered=self.queue(lambda j:seen.append(j['item_id']) or 'updated');recovered.start();self.await_idle(recovered);self.assertEqual(seen,['a','b'])
 def test_restart_merges_interrupted_with_newer_pending(self):
  q=self.queue();q.enqueue('a',refresh=True);payload=json.loads(self.path.read_text());old=payload['jobs'][0];old['status']='running';new=copy.deepcopy(old);new.update(id='new',status='queued',refresh=False,created_at=old['created_at']+1,updated_at=old['updated_at']+1);payload['jobs'].append(new);self.path.write_text(json.dumps(payload))
  recovered=self.queue();snapshot=recovered.snapshot();self.assertEqual(len(snapshot['active']),1);self.assertTrue(snapshot['active'][0]['refresh'])
 def test_failure_does_not_stop_next_job_and_retry_is_durable(self):
  def execute(job):
   if job['item_id']=='a':raise ValueError('no poster')
   return 'updated'
  q=self.queue(execute);failed=q.enqueue('a');q.enqueue('b');q.start();self.await_idle(q)
  self.assertEqual(q.snapshot()['counts']['failed'],1);self.assertEqual(q.snapshot()['counts']['succeeded'],1)
  q.execute=lambda j:'updated';retried=q.retry(failed['id']);self.assertNotEqual(failed['id'],retried['id']);self.await_idle(q)
  self.assertEqual(q.snapshot()['counts']['succeeded'],2)
 def test_concurrent_submissions_do_not_lose_items(self):
  q=self.queue()
  with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda n:q.enqueue(str(n)),range(40)))
  self.assertEqual(len(q.snapshot()['active']),40);self.assertEqual(len(json.loads(self.path.read_text())['jobs']),40)

class MatchingRegressionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.stack=ExitStack()
  for name,value in [('CACHE_DIR',self.root),('DOUBAN_CACHE_DIR',self.root/'douban'),('RATING_OVERRIDES_FILE',self.root/'overrides.json'),('_RATING_OVERRIDES_MTIME',-1),('_RATING_OVERRIDES_CACHE',{}),('OMDB_API_KEY','')]:self.stack.enter_context(patch.object(app,name,value))
  self.stack.enter_context(patch.object(app,'_start_item_render',return_value=True));self.stack.enter_context(patch.object(app,'_omdb_cached_request',return_value={'Response':'False'}));app.MATCH_CANDIDATES.clear()
  self.item={'Id':'1','Name':'二十二','ProductionYear':2017,'Type':'Series','ProviderIds':{}}
 def tearDown(self):self.stack.close();self.temp.cleanup();app.MATCH_CANDIDATES.clear()
 def cache(self):
  path=app.DOUBAN_CACHE_DIR/'douban-movies'/'26430107_tt3843282';path.mkdir(parents=True);(path/'all.json').write_text(json.dumps({'id':'26430107','title':'二十二','year':'2015','is_tv':False,'rating':{'value':'8.7'}}))
 def test_movie_can_be_searched_for_misclassified_series(self):
  self.cache()
  with patch.object(search_sources,'gather',return_value=({'status':'ok','items':[]},{'status':'ok','items':[]})):
   row=app._match_search(self.item,{'query':'二十二','kind':'Movie','year':'2017'})['items'][0]
  self.assertFalse(row['blocked']);self.assertIn('type_correction',row['warnings']);self.assertIn('year_difference',row['warnings'])
 def test_official_series_all_json_is_searchable_by_payload_type(self):
  path=app.DOUBAN_CACHE_DIR/'cache'/'123_tt123';path.mkdir(parents=True)
  (path/'all.json').write_text(json.dumps({'id':'123','title':'Show','year':'2020','is_tv':True,'rating':{'value':'8.1'}}))
  rows=media_match.search_local(app.DOUBAN_CACHE_DIR,'Show','Series','2020')
  self.assertEqual(rows[0]['douban_id'],'123');self.assertEqual(media_match.search_local(app.DOUBAN_CACHE_DIR,'Show','Movie','2020'),[])
 def test_direct_cache_id_requires_explicit_type_then_reads_movie_cache(self):
  self.cache();row=app._match_search(self.item,{'douban_id':'26430107','kind':'Movie','year':'2017'})['items'][0]
  preview=app._match_preview(self.item,{'token':row['token']});self.assertEqual(preview['automatic_ratings']['豆瓣'],'8.7')
  with self.assertRaises(ValueError):app._match_confirm(self.item,{'token':row['token'],'confirmed':True,'sync_emby':False})
  with patch.object(app,'_scan_is_busy',return_value=True):result=app._match_confirm(self.item,{'token':row['token'],'confirmed':True,'sync_emby':False,'type_confirmed':True})
  self.assertTrue(result['queued']);self.assertEqual(app._effective_item_type(self.item)['Type'],'Movie');self.assertEqual(app._douban_cache_result(self.item)['value'],'8.7')
 def test_unreadable_direct_id_requires_manual_confirmation_and_never_guesses_imdb(self):
  item={**self.item,'Type':'Movie'}
  with patch.object(search_sources,'douban_subject',return_value={'status':'verification_required','items':[]}):row=app._match_search(item,{'douban_id':'https://movie.douban.com/subject/26430107/','kind':'Movie','year':'2017'})['items'][0]
  self.assertTrue(row['requires_manual_verification']);self.assertIsNone(row['imdb_id']);app._match_preview(item,{'token':row['token']})
  with self.assertRaises(ValueError):app._match_confirm(item,{'token':row['token'],'confirmed':True,'sync_emby':False})
  result=app._match_confirm(item,{'token':row['token'],'confirmed':True,'sync_emby':False,'manual_verified':True});self.assertIsNone(result['effective_ids']['imdb'])
 def test_direct_lookup_never_appends_title_candidates(self):
  self.cache()
  with patch.object(app,'_omdb_cached_request',return_value={'Response':'True','Search':[{'Title':'Other','Type':'movie','Year':'2017','imdbID':'tt999'}]}):rows=app._match_search({**self.item,'Type':'Movie'},{'douban_id':'26430107','kind':'Movie'})['items']
  self.assertEqual(len(rows),1);self.assertEqual(rows[0]['douban_id'],'26430107')
 def test_known_douban_waits_for_cache_without_imdb_becoming_required_action(self):
  item={**self.item,'ProviderIds':{'Douban':'26430107'}}
  r=issue_policy.classify(item,{}, {'status':'missing_cache_file'},['missing_imdb_id'],[],time.time())
  self.assertEqual(r['status'],'waiting');self.assertFalse(any(i['action']=='match' for i in r['issues']))
 def test_cache_without_rating_is_reference(self):
  item={**self.item,'ProviderIds':{'Douban':'26430107'}}
  r=issue_policy.classify(item,{}, {'status':'json_without_rating'},['missing_imdb_id'],[],time.time());self.assertEqual(r['status'],'info')
 def test_ranking_prioritizes_near_year_over_alphabetical_title(self):
  rows=[{'title':'Show 第二季','year':'2020','imdb_id':None,'douban_id':'2'},{'title':'Show 第一季','year':'2004','imdb_id':None,'douban_id':'1'},{'title':'Show 美版第一季','year':'2011','imdb_id':None,'douban_id':'3'}]
  self.assertEqual(media_match.sort_candidates(rows,'Show',2011)[0]['douban_id'],'3')

class SearchParsingTests(unittest.TestCase):
 def test_safe_id_parser_rejects_other_hosts_and_malformed_ids(self):
  self.assertEqual(search_sources.parse_subject_id('https://movie.douban.com/subject/26430107/?from=search'),'26430107')
  for value in ['0','abc','http://localhost/subject/123/','https://movie.douban.com.evil/subject/123/','123/../../x']:
   with self.assertRaises(ValueError):search_sources.parse_subject_id(value)
 def test_full_search_reads_explicit_tv_category_and_identity(self):
  text='''<div class="result"><div class="pic"><a href="https://www.douban.com/link2/?url=https%3A%2F%2Fmovie.douban.com%2Fsubject%2F123%2F"><img src="https://img.example/a.jpg"></a></div><div><h3><span>[电视剧]</span><a>无耻之徒(美版) 第一季</a></h3><span class="subject-cast">原名:Shameless / 2011</span><p>summary</p></div></div>'''
  rows=search_sources.parse_douban_search(text,'Series');self.assertEqual(rows[0]['douban_id'],'123');self.assertEqual(rows[0]['year'],'2011');self.assertEqual(rows[0]['title'],'无耻之徒(美版) 第一季');self.assertEqual(search_sources.parse_douban_search(text,'Movie'),[])
 def test_page_challenge_is_not_a_subject(self):
  search_sources._CACHE.clear();session=Mock();session.get.return_value=Mock(status_code=200,text='<title>豆瓣</title><script>challenge</script>')
  result=search_sources.douban_subject(session,'123');self.assertEqual(result['status'],'verification_required');self.assertEqual(result['items'],[])

if __name__=='__main__':unittest.main()
