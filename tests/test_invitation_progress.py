import contextlib,datetime as dt,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import erp_invitations as bridge
import erp_invitation_library as library

class Progress(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.worker=bridge.Engine(self.root,'https://erp.example',None,None,async_jobs=False)
  self.key='progress-job-0001';self.public='a'*32
  self.input={'requestId':self.key,'invitationId':'invitation-progress-01','version':1,'kind':'wedding','action':'parse','automatic':True,'orderMode':'invitation-checkout','source':'https://example.test/card','values':{}}
  with self.worker.db() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(self.key,self.input['invitationId'],1,'parse','x',json.dumps(self.input),'queued',None,self.public,'2099-01-01T01:00:00+09:00'))
 def tearDown(self):self.worker.executor.shutdown(wait=True);self.tmp.cleanup()
 def test_get_returns_persisted_real_photo_counts_and_terminal_cannot_be_overwritten(self):
  self.worker.progress(self.key,1,'원본 읽기');self.worker.progress(self.key,3,'사진 선별',completedPhotos=2)
  value=self.worker.get(self.key);self.assertEqual(value['status'],'processing');self.assertEqual(value['progress']['step'],3);self.assertEqual(value['progress']['completedPhotos'],2);self.assertEqual(value['progress']['totalPhotos'],4)
  self.assertEqual(value['progress']['startedAt'],'2099-01-01T01:00:00+09:00')
  result={'status':'done','values':{'name':'verified'},'progress':value['progress']}
  with self.worker.db() as c:c.execute("UPDATE jobs SET state='done',result=? WHERE id=?",(json.dumps(result),self.key))
  self.assertIsNone(self.worker.progress(self.key,7,'must not overwrite',completedPhotos=4));self.assertEqual(self.worker.get(self.key),result)
 def test_corrupt_old_payload_does_not_break_processing_and_invalid_step_is_ignored(self):
  with self.worker.db() as c:c.execute('UPDATE jobs SET result=? WHERE id=?',('not-json',self.key))
  self.assertNotIn('progress',self.worker.get(self.key));self.assertIsNone(self.worker.progress(self.key,8,'invalid'))
  self.worker.progress(self.key,4,'제작');self.assertEqual(self.worker.get(self.key)['progress']['step'],4)
 def test_parse_stages_and_photo_callback_reflect_actual_completed_candidates(self):
  seen=[]
  when=dt.datetime(2099,1,28,11)
  def parse(source,extract):
   seen.append(self.worker.get(self.key)['progress']['step'])
   return {'name':'가상예식','when':when,'families':[('가상부모','장남','가상신랑'),('가상혼주','장녀','가상신부')],'photos':['https://photo.test/a'],'source':source}
  self.worker.legacy=SimpleNamespace(parse_source=parse)
  def select(worker,values,progress):
   seen.append(worker.get(self.key)['progress']['step'])
   for number in range(1,5):
    progress(number,4);seen.append(worker.get(self.key)['progress']['completedPhotos'])
   return values,[]
  with patch('erp_invitation_auto.select_photos',side_effect=select):self.worker.run(self.key)
  result=self.worker.get(self.key);self.assertEqual(result['status'],'done');self.assertEqual(seen,[1,3,1,2,3,4]);self.assertEqual(result['progress']['completedPhotos'],4)
 def test_publish_registers_catalog_while_job_still_processing_before_marking_success(self):
  values={'name':'가상예식','date':'2099-01-28','time':'11:00'}
  self.input.update(action='publish',values=values)
  with self.worker.db() as c:c.execute("UPDATE jobs SET action='publish',input=? WHERE id=?",(json.dumps(self.input),self.key))
  items=[{'price':p,'productId':str(p),'orderUrl':f'https://erp.example/i/'+('b'*32)+f'/{p}/order','url':f'https://erp.example/erp/i/{self.public}/{p}.html'} for p in (93500,99000)]
  result={'status':'done','jobId':self.public,'fingerprint':'f'*64,'items':items}
  folder=self.worker.root/'public'/self.public;folder.mkdir(parents=True)
  for p in (93500,99000):(folder/f'{p}.html').write_text('<html>published</html>')
  (folder/'result.json').write_text(json.dumps(result))
  real_register=library.register;seen=[]
  def register(worker,root,pending=None):
   current=worker.get(self.key);seen.append((current['status'],current['progress']['step']))
   return real_register(worker,root,pending=pending)
  with patch.object(self.worker,'recover_publication',return_value=result),patch.object(library,'register',side_effect=register):self.worker.run(self.key)
  self.assertEqual(seen,[('processing',7)]);self.assertEqual(self.worker.get(self.key)['status'],'done');self.assertIn(self.public,library.state(self.root))
 def test_progress_survives_error_and_does_not_report_later_steps(self):
  self.worker.legacy=SimpleNamespace(parse_source=lambda *args:(_ for _ in ()).throw(RuntimeError('unavailable')))
  self.worker.run(self.key);value=self.worker.get(self.key);self.assertEqual(value['status'],'error');self.assertEqual(value['progress']['step'],1)
  self.assertIsNone(self.worker.progress(self.key,7,'late callback'))

if __name__=='__main__':unittest.main()
