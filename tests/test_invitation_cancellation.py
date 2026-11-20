"""Durable cancellation fences late workers and the native publication commit."""
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import erp_invitations as bridge
import erp_invitation_library as library
import erp_invitation_management as management
import erp_local_checkout as local


class Cancellation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.worker=bridge.Engine(self.root,'https://erp.example',None,None,async_jobs=False)
        self.invitation='cancel-invitation-0001';self.fingerprint='f'*64
        self.values={'name':'검증 행사','host':'가상 주최','date':'2099-12-19','time':'11:00','venue':'가상홀','address':'서울시 가상로 10'}
    def tearDown(self):self.worker.executor.shutdown(wait=True);self.temp.cleanup()
    def enqueue(self,action='parse',key='cancel-request-0001',version=1,**extra):
        request={'requestId':key,'invitationId':self.invitation,'version':version,'action':action,'kind':'event','source':'검증 행사 안내','orderMode':'invitation-checkout','values':self.values,**extra}
        with patch.object(self.worker,'schedule'):self.worker.submit(request)
        return request
    def row(self,key):
        with self.worker.db() as db:return db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
    def complete(self,key,result):
        with self.worker.db() as db:db.execute("UPDATE jobs SET state='done',result=? WHERE id=?",(json.dumps(result),key))
    def publication(self):
        preview=self.enqueue('preview','cancel-preview-0001');self.complete(preview['requestId'],{'status':'done','fingerprint':self.fingerprint})
        request=self.enqueue('publish','cancel-publish-0001',previewRequestId=preview['requestId'],fingerprint=self.fingerprint)
        local_key=hashlib.sha256(('local-checkout:'+self.invitation+':1').encode()).hexdigest()[:32]
        items=[{'price':price,'productId':str(price+1),'orderUrl':f'https://erp.example/i/{local_key}/{price}/order','verified':True,'source':'own-checkout'} for price in local.PRICES]
        products={price:{'number':str(price+1),'price':price,'title':'검증 행사','kind':'event','date':'2099-12-19','clock':'11:00','venue':'가상홀','address':'서울시 가상로 10','event_time':'2099-12-19T11:00:00+09:00','localCheckout':True,'groups':[],'images':[]} for price in local.PRICES}
        result={'status':'done','items':items,'fingerprint':self.fingerprint,'expires':'2099-12-20'}
        assets={'sample.webp':b'sample','hero.webp':b'','thumbnail.jpg':b'jpeg','reference-components.json':b'{}','local-products.json':json.dumps({'key':local_key,'products':products}).encode()}
        return request,result,assets,local_key
    def start(self,key,target):
        thread=threading.Thread(target=target or self.worker.run,args=(key,));thread.start();return thread
    def join(self,thread):thread.join(5);self.assertFalse(thread.is_alive(),'worker did not finish')
    def test_queued_cancel_is_idempotent_and_same_request_is_never_rescheduled(self):
        request=self.enqueue();first=self.worker.cancel(request['requestId'],{'invitationId':self.invitation,'version':1,'actor':'검증 직원'})
        self.assertEqual(first['status'],'cancelled');self.assertFalse(first['replayed']);stored=self.row(request['requestId'])['result']
        self.assertTrue(self.worker.cancel(request['requestId'])['replayed']);self.assertEqual(self.row(request['requestId'])['result'],stored)
        with patch.object(self.worker,'schedule') as schedule,patch.object(self.worker,'parse') as parse:
            self.assertEqual(self.worker.submit(request)['status'],'cancelled');self.worker.run(request['requestId']);schedule.assert_not_called();parse.assert_not_called()
        with patch.object(self.worker,'schedule') as schedule:
            self.worker.submit({**request,'requestId':'cancel-new-request-0002','version':2});schedule.assert_called_once_with('cancel-new-request-0002')
    def test_running_late_parse_result_cannot_resurrect_cancelled_job(self):
        request=self.enqueue();started=threading.Event();release=threading.Event()
        def parse(_):started.set();self.assertTrue(release.wait(5));return {'status':'done','values':{'name':'late'}}
        with patch.object(self.worker,'parse',side_effect=parse):
            thread=self.start(request['requestId'],None)
            try:self.assertTrue(started.wait(3));self.worker.cancel(request['requestId']);self.assertEqual(self.worker.get(request['requestId'])['status'],'cancelled')
            finally:release.set();self.join(thread)
        self.assertEqual(self.row(request['requestId'])['state'],'cancelled');self.assertNotIn('late',self.row(request['requestId'])['result'])
    def test_running_late_error_and_progress_cannot_overwrite_cancellation(self):
        request=self.enqueue();started=threading.Event();release=threading.Event()
        def parse(_):started.set();self.assertTrue(release.wait(5));raise ValueError('late provider error')
        with patch.object(self.worker,'parse',side_effect=parse),patch('builtins.print'):
            thread=self.start(request['requestId'],None)
            try:self.assertTrue(started.wait(3));self.worker.cancel(request['requestId'])
            finally:release.set();self.join(thread)
        with self.assertRaises(bridge.JobCancelled):self.worker.progress(request['requestId'],3,'late progress')
        self.assertEqual(self.worker.get(request['requestId'])['status'],'cancelled');self.assertNotIn('late provider error',self.row(request['requestId'])['result'])
    def test_cancelled_jobs_are_not_recovered_after_process_restart(self):
        request=self.enqueue();self.worker.cancel(request['requestId'])
        with patch.object(bridge.Engine,'schedule') as schedule:
            restarted=bridge.Engine(self.root,'https://erp.example',None,None,async_jobs=False)
            try:schedule.assert_not_called();self.assertEqual(restarted.get(request['requestId'])['status'],'cancelled')
            finally:restarted.executor.shutdown(wait=True)
    def test_completed_result_and_wrong_scope_are_preserved(self):
        request=self.enqueue()
        with self.assertRaises(ValueError):self.worker.cancel(request['requestId'],{'invitationId':'wrong-invitation','version':1})
        self.assertEqual(self.row(request['requestId'])['state'],'queued')
        self.complete(request['requestId'],{'status':'done','values':{'name':'finished'}})
        with self.assertRaises(bridge.CancellationConflict) as error:self.worker.cancel(request['requestId'])
        self.assertEqual(error.exception.result['status'],'done');self.assertEqual(self.worker.get(request['requestId'])['values']['name'],'finished')
    def test_preview_cancel_after_prepare_creates_no_preview_and_no_success(self):
        request=self.enqueue('preview');started=threading.Event();release=threading.Event()
        def prepare(_):started.set();self.assertTrue(release.wait(5));return {'status':'done'},{'sample.webp':b'sample','hero.webp':b'','thumbnail.jpg':b'jpeg'}
        with patch.object(self.worker,'prepare',side_effect=prepare):
            thread=self.start(request['requestId'],None)
            try:self.assertTrue(started.wait(3));self.worker.cancel(request['requestId'])
            finally:release.set();self.join(thread)
        self.assertEqual(self.worker.get(request['requestId'])['status'],'cancelled');self.assertFalse((self.worker.root/'private'/request['requestId']/'preview.html').exists())
    def test_publish_cancel_before_final_commit_exposes_no_manifest_products_or_library_pair(self):
        request,result,assets,local_key=self.publication();started=threading.Event();release=threading.Event()
        def page(*args,**kwargs):started.set();self.assertTrue(release.wait(5));return '<html>prepared privately</html>'
        with patch.object(self.worker,'prepare',return_value=(result,assets)),patch.object(bridge.render,'page',side_effect=page),patch.object(local,'publish') as publish:
            thread=self.start(request['requestId'],None)
            try:self.assertTrue(started.wait(3));self.worker.cancel(request['requestId'])
            finally:release.set();self.join(thread)
            publish.assert_not_called()
        row=self.row(request['requestId']);self.assertEqual(row['state'],'cancelled');self.assertFalse((self.worker.root/'public'/row['public_id']/'result.json').exists());self.assertFalse((self.root/'invitations'/local_key).exists())
        self.assertEqual(library.catalog(self.worker,self.root)['items'],[]);self.assertEqual(management.products(self.worker,self.root),[])
        self.assertEqual(self.worker.public(row['public_id'],'93500.html')[0],404)
    def test_publish_final_commit_wins_race_and_cancel_reports_completed_without_reversing_it(self):
        request,result,assets,local_key=self.publication();started=threading.Event();release=threading.Event();cancel_started=threading.Event();cancel_finished=threading.Event();outcome=[]
        publish=local.publish
        def blocked_publish(*args,**kwargs):started.set();self.assertTrue(release.wait(5));return publish(*args,**kwargs)
        def cancel():
            cancel_started.set()
            try:self.worker.cancel(request['requestId'])
            except bridge.CancellationConflict as error:outcome.append(error.result)
            finally:cancel_finished.set()
        with patch.object(self.worker,'prepare',return_value=(result,assets)),patch.object(bridge.render,'page',return_value='<html>complete</html>'),patch.object(local,'publish',side_effect=blocked_publish):
            thread=self.start(request['requestId'],None);self.assertTrue(started.wait(3));canceller=threading.Thread(target=cancel);canceller.start()
            try:self.assertTrue(cancel_started.wait(3));self.assertFalse(cancel_finished.wait(.05))
            finally:release.set();self.join(thread);self.join(canceller)
        self.assertEqual(outcome[0]['status'],'done');self.assertEqual(self.worker.get(request['requestId'])['status'],'done')
        self.assertTrue((self.worker.root/'public'/self.row(request['requestId'])['public_id']/'result.json').is_file());self.assertEqual(len(library.catalog(self.worker,self.root)['items']),1)
        self.assertTrue(all((self.root/'invitations'/local_key/f'{price}.order.json').is_file() for price in local.PRICES))
    def test_stale_unpublished_native_snapshots_are_quarantined_and_removed_from_legacy_index(self):
        request,result,assets,local_key=self.publication();row=self.row(request['requestId']);folder=self.root/'invitations'/local_key;folder.mkdir(parents=True)
        originals={}
        for price in local.PRICES:
            raw=json.dumps({'title':'중단 전 임시 상품','localCheckout':True,'publicationId':row['public_id'],'price':price,'event_time':'2099-12-19T11:00:00+09:00'}).encode();originals[f'{price}.order.json']=raw;(folder/f'{price}.order.json').write_bytes(raw)
        self.assertEqual(len(library.catalog(self.worker,self.root)['items']),1)
        self.worker.cancel(request['requestId']);self.assertEqual(library.catalog(self.worker,self.root)['items'],[]);self.assertEqual(management.products(self.worker,self.root),[])
        backups=list((self.worker.root/'private'/request['requestId']).glob('cancelled-checkout-*'))
        self.assertEqual(len(backups),1)
        for name,raw in originals.items():self.assertEqual((backups[0]/name).read_bytes(),raw);self.assertFalse((folder/name).exists())
    def test_stale_running_job_with_committed_manifest_is_reconciled_to_done(self):
        request,result,assets,local_key=self.publication();row=self.row(request['requestId']);public=self.worker.root/'public'/row['public_id'];public.mkdir(parents=True)
        for name in ('93500.html','99000.html','sample.webp','thumbnail.jpg'):(public/name).write_bytes(b'committed-output')
        committed={**result,'jobId':row['public_id'],'items':[{**item,'url':f'https://erp.example/erp/i/{row["public_id"]}/{item["price"]}.html'} for item in result['items']]}
        raw=json.dumps(committed).encode();(public/'result.json').write_bytes(raw)
        with self.worker.db() as db:db.execute("UPDATE jobs SET state='running' WHERE id=?",(request['requestId'],))
        with self.assertRaises(bridge.CancellationConflict) as error:self.worker.cancel(request['requestId'])
        self.assertEqual(error.exception.result['status'],'done');self.assertEqual(self.worker.get(request['requestId'])['status'],'done');self.assertEqual((public/'result.json').read_bytes(),raw)
        with self.worker.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)
        self.assertEqual(len(library.catalog(self.worker,self.root)['items']),1)
    def test_api_requires_authentication_and_returns_terminal_cancelled(self):
        request=self.enqueue();replies=[];headers={'Content-Length':'0'}
        handler=SimpleNamespace(headers=headers,rfile=io.BytesIO(),send_json=lambda value,status=200:replies.append((status,value)))
        path='/erp/invitations/jobs/'+request['requestId']+'/cancel'
        with patch.dict('os.environ',{'ERP_INVITATION_TOKEN':'cancel-test-secret'}),patch.object(bridge,'engine',return_value=self.worker):
            bridge.dispatch(handler,'POST',path,self.root,'https://erp.example',None,None);self.assertEqual(replies[-1][0],401);self.assertEqual(self.row(request['requestId'])['state'],'queued')
            headers['Authorization']='Bearer cancel-test-secret';bridge.dispatch(handler,'POST',path,self.root,'https://erp.example',None,None)
            self.assertEqual(replies[-1][0],200);self.assertEqual(replies[-1][1]['status'],'cancelled')
            bridge.dispatch(handler,'GET',path,self.root,'https://erp.example',None,None);self.assertEqual(replies[-1][0],405)

if __name__=='__main__':unittest.main()
