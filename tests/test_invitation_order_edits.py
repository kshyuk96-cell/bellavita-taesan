import contextlib,copy,io,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import checkout
import order_operations
import erp_invitation_management as management
import erp_invitation_order_edits as edits

class OrderEdits(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.journal=self.root/'journal.sqlite';self.source='a'*32
  with sqlite3.connect(self.journal) as db:db.execute('CREATE TABLE jobs(input,result,created_at,invitation_id,action,state,public_id)')
  @contextlib.contextmanager
  def journal():
   with sqlite3.connect(self.journal) as db:db.row_factory=sqlite3.Row;yield db
  self.worker=SimpleNamespace(root=self.root/'erp-invitations',db=journal,origin='https://erp.test')
  with sqlite3.connect(self.root/'state.sqlite3') as db:
   checkout.setup(db);order_operations.setup(db)
   self.original={'amount':99000,'product':'101','title':'가상 예식','buyer':'가상 구매자','buyer_phone':'01000000000','sender':'가상 단체','recipient':'가상 혼주','venue':'기존홀','address':'기존 주소','event_time':'2099-01-28T11:00:00+09:00','options':[['상품 선택','2호 선물형']],'wedding_preview':True}
   db.execute('INSERT INTO invitation_orders VALUES(?,?,?,?,?,?,?,?)',(self.source,'b'*32,'99000','2099-01-01T01:00:00+09:00',json.dumps(self.original),'paid','payment-original','https://payment.test/original'))
   db.commit()
 def tearDown(self):self.tmp.cleanup()
 def update(self,action,revision=0,fields=None):
  p={'sourceId':'payapp:'+self.source,'action':action,'revision':revision}
  if fields is not None:p['fields']=fields
  return edits.update(self.root,p,'staff')
 def query(self,**extra):
  params={'from':['2099-01-01'],'to':['2099-01-31'],**extra}
  with patch.object(management,'notifications',return_value={}):return management.query(self.worker,self.root,params)
 def original_row(self):
  with sqlite3.connect(self.root/'state.sqlite3') as db:return db.execute('SELECT * FROM invitation_orders WHERE id=?',(self.source,)).fetchone()
 def test_fulfillment_edit_is_applied_to_list_and_paid_registration_recheck_without_financial_mutation(self):
  before=self.original_row();self.update('edit',fields={'sender':'수정 단체','recipient':'수정 혼주','date':'2099-02-01','time':'12:30','address':'수정 주소','message':'수정 문구','buyer':'수정 구매자','buyerPhone':'01000000001'})
  row=self.query(id=['payapp:'+self.source])['orders'][0]
  self.assertEqual(row['state'],'paid');self.assertEqual(row['amount'],99000);self.assertEqual(row['quantity'],1);self.assertEqual(row['productId'],'101')
  self.assertEqual(row['fields']['sender'],'수정 단체');self.assertEqual(row['fields']['date'],'2099-02-01');self.assertEqual(row['buyer'],'수정 구매자');self.assertEqual(row['buyerPhone'],'01000000001');self.assertEqual(row['editRevision'],1)
  self.assertEqual(self.original_row(),before)
  with self.assertRaisesRegex(ValueError,'다른 직원'):self.update('edit',fields={'sender':'stale'})
 def test_immutable_financial_fields_cannot_be_edited(self):
  before=self.original_row()
  for field in ['amount','quantity','state','price','payment_number','productId','id','createdAt']:
   with self.subTest(field=field),self.assertRaises(ValueError):self.update('edit',fields={field:'123'})
  self.assertEqual(before,self.original_row());self.assertEqual(self.query()['orders'][0]['editRevision'],0)
 def test_delete_restore_preserves_original_payment_and_hidden_cannot_be_registered(self):
  before=self.original_row();first=self.update('delete');self.assertEqual(self.query()['orders'],[]);self.assertEqual(self.query(id=['payapp:'+self.source])['orders'],[]);self.assertEqual(self.query()['total'],0)
  self.assertEqual(before,self.original_row());self.assertTrue(self.update('delete')['replayed'])
  hidden=self.query(includeHidden=['1'])['orders'][0];self.assertTrue(hidden['hidden']);self.assertEqual(hidden['state'],'paid')
  with self.assertRaisesRegex(ValueError,'먼저 복원'):self.update('edit',1,{'sender':'hidden edit'})
  self.update('restore',1);self.assertEqual(len(self.query()['orders']),1);self.assertEqual(self.query()['orders'][0]['editRevision'],2);self.assertEqual(before,self.original_row())
 def test_hidden_orders_are_excluded_before_pagination_count_and_limits(self):
  with sqlite3.connect(self.root/'state.sqlite3') as db:
   for i in range(500):db.execute('INSERT INTO invitation_orders VALUES(?,?,?,?,?,?,?,?)',(f'test-order-{i:04}', 'b'*32,'99000','2099-01-02T01:00:00+09:00',json.dumps(self.original),'waiting',None,None))
   db.commit()
  self.assertEqual(self.query()['pages'],2)
  edits.update(self.root,{'sourceId':'payapp:test-order-0000','action':'delete','revision':0},'staff')
  result=self.query();self.assertEqual(result['pages'],1);self.assertEqual(result['total'],500);self.assertEqual(len(result['orders']),500)
  self.assertNotIn('payapp:test-order-0000',{r['id'] for r in result['orders']})
 def test_later_payment_state_changes_remain_authoritative_after_fulfillment_edit(self):
  self.update('edit',fields={'sender':'수정 단체'})
  with sqlite3.connect(self.root/'state.sqlite3') as db:db.execute("UPDATE invitation_orders SET state='cancelled' WHERE id=?",(self.source,));db.commit()
  row=self.query(id=['payapp:'+self.source])['orders'][0];self.assertEqual(row['state'],'cancelled');self.assertEqual(row['fields']['sender'],'수정 단체')
 def test_bad_dates_times_nonexistent_orders_and_malformed_source_are_rejected(self):
  for fields in [{'date':'2099-02-30'},{'time':'25:99'},{'sender':None},{'amount':99000}]:
   with self.assertRaises(ValueError):self.update('edit',fields=fields)
  for source in ['blogpay:123','payapp:missing','payapp:../../file']:
   with self.assertRaises(ValueError):edits.update(self.root,{'sourceId':source,'action':'delete','revision':0},'staff')
 def test_dispatch_only_accepts_post_and_bridge_auth_is_required(self):
  payload={'sourceId':'payapp:'+self.source,'action':'delete','revision':0};raw=json.dumps(payload).encode();answers=[]
  h=SimpleNamespace(headers={'Content-Length':str(len(raw))},rfile=io.BytesIO(raw),send_json=lambda data,status=200:answers.append((data,status)))
  self.assertTrue(management.dispatch(h,'POST','/erp/invitations/order-update',self.root,self.worker));self.assertTrue(answers[-1][0]['hidden'])
  self.assertTrue(management.dispatch(h,'GET','/erp/invitations/order-update',self.root,self.worker));self.assertEqual(answers[-1][1],405)
  import erp_invitations
  with patch.dict('os.environ',{'ERP_INVITATION_TOKEN':'secret'}):self.assertTrue(erp_invitations.dispatch(h,'POST','/erp/invitations/order-update',self.root,'https://erp.test',None,None))
  self.assertEqual(answers[-1][1],401)

if __name__=='__main__':unittest.main()
