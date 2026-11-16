import io,json,os,sqlite3,tempfile,hashlib,unittest
from pathlib import Path
from unittest.mock import patch
import erp_invitations as bridge
class DiagnosticsTest(unittest.TestCase):
 def test_records_are_read_without_vendor_writes(self):
  with tempfile.TemporaryDirectory() as tmp:
   worker=bridge.Engine(Path(tmp),'https://example.org',None,None,async_jobs=False)
   values={'name':'가상 행사','date':'2099-12-12','time':'14:00','venue':'검증홀','address':'가상 주소','host':'검증'}
   data={'kind':'event','values':values};title=bridge.render.product_name(values,'event');job='diagnostics-fixture-01'
   with worker.db() as db:db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(job,'invitation-fixture',1,'preview','hash',json.dumps(data),'error','{}','public-fixture','2099-01-01'))
   with sqlite3.connect(worker.root/'invitation_products.sqlite3') as db:
    db.execute('CREATE TABLE copies (key TEXT PRIMARY KEY,number TEXT,before TEXT)');db.execute('INSERT INTO copies VALUES(?,?,?)',(hashlib.sha256(f'{title}\0{93500}'.encode()).hexdigest(),'2','[]'))
   class Client:
    def __init__(self,*a):pass
    def detail(self,n):return {'goodName':title,'goodPrice':93500}
    def settings(self,row):return ()
    def image_checksums(self,row):return []
   class Handler:
    headers={'Authorization':'Bearer fixture-token'}
    def send_json(self,r,status=200):self.result=r;self.status=status
   h=Handler()
   with patch.dict(os.environ,{'ERP_INVITATION_TOKEN':'fixture-token','BLOGPAY_INVITATION_TEMPLATE_ID':'1'}),patch.object(bridge,'engine',return_value=worker),patch('blogpay.BlogPay',Client):
    bridge.dispatch(h,'GET','/erp/invitations/jobs/'+job+'/diagnostics',tmp,'https://example.org',None,None)
   self.assertEqual(h.status,200);self.assertTrue(h.result['copies'][0]['matches']['settings'])
