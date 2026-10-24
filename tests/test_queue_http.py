import json,sys,tempfile,threading,time,unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from contextlib import ExitStack
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,requests,render_queue

class BusyQueueHttpTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.stack=ExitStack();self.lock=threading.Lock();self.lock.acquire();self.completed=[]
  self.queue=render_queue.RenderQueue(self.root/'jobs.json',self.lock,lambda job:self.completed.append(job['item_id'])or'updated')
  for name,value in [('WORK_QUEUE',self.queue),('CACHE_DIR',self.root),('RATING_OVERRIDES_FILE',self.root/'overrides.json'),('_RATING_OVERRIDES_MTIME',-1),('_RATING_OVERRIDES_CACHE',{})]:self.stack.enter_context(patch.object(app,name,value))
  self.stack.enter_context(patch.object(app,'_require_admin',return_value=True));self.stack.enter_context(patch.object(app,'_scan_is_busy',return_value=True))
  self.stack.enter_context(patch.object(app,'_get_item_by_id',side_effect=lambda id:{'Id':id,'Name':'Item '+id,'Type':'Movie','ProviderIds':{}}))
  self.stack.enter_context(patch.object(app,'_item_summary',side_effect=lambda item:{'id':item['Id']}))
  self.server=ThreadingHTTPServer(('127.0.0.1',0),app.PosterHandler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url='http://127.0.0.1:'+str(self.server.server_port)
 def tearDown(self):
  self.server.shutdown();self.server.server_close();self.lock.release();self.queue.close();self.stack.close();self.temp.cleanup()
 def test_busy_override_is_saved_and_multiple_http_submissions_queue(self):
  for id in ['1','2']:
   response=requests.post(self.url+'/api/items/'+id+'/override',json={'douban':'8.1','score_modes':{'douban':'manual'},'render':True},timeout=2)
   self.assertEqual(response.status_code,202,response.text);self.assertTrue(response.json()['queued'])
  self.assertEqual(self.completed,[]);self.assertEqual(len(json.loads((self.root/'overrides.json').read_text())['items']),2)
  response=requests.get(self.url+'/api/tasks',timeout=2);self.assertEqual(response.status_code,200);self.assertEqual(response.json()['counts']['queued'],2)
  self.lock.release()
  try:
   deadline=time.monotonic()+2
   while time.monotonic()<deadline and self.queue.snapshot()['active']:threading.Event().wait(.01)
   self.assertEqual(self.completed,['1','2'])
  finally:self.lock.acquire()
 def test_busy_restore_is_queued_instead_of_rejected(self):
  original=self.root/'original'/'1.jpg';original.parent.mkdir();original.write_bytes(b'fixture')
  response=requests.post(self.url+'/api/items/1/restore',json={},timeout=2)
  self.assertEqual(response.status_code,202,response.text);self.assertEqual(response.json()['task']['action'],'restore');self.assertEqual(self.completed,[])

if __name__=='__main__':unittest.main()
