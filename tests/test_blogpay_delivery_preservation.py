import hashlib,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from blogpay import BlogPay
class DeliveryPreservation(unittest.TestCase):
 def test_retry_repairs_vendor_delivery_defaults_on_the_same_copy(self):
  client=BlogPay('example','fixture')
  title='가상 행사'
  template={'goodNum':1,'goodName':'쌀빛그림화환','goodPrice':99000,'isShow':0,'isRecommend':0,'optionState':1,'enablePayMethod':1,'isDeliveryPrice':0,'isTodayShipUse':'N','todayShipTime':'00:00:00'}
  clone={**template,'goodNum':2,'goodName':title,'goodPrice':93500,'isDeliveryPrice':1,'isTodayShipUse':None,'todayShipTime':None,'shortGoodUrl':'https://bp2.kr/test93500'}
  active={**template,'goodNum':3,'goodName':title,'isShow':1,'isSoldout':0,'shortGoodUrl':'https://bp2.kr/test99000'}
  def detail(number):return dict(template if str(number)=='1' else clone)
  def sale(item,visible):clone.update(isShow=int(visible),isSoldout=0)
  def request(endpoint,**fields):
   self.assertEqual(endpoint,'goodsModify');self.assertEqual(fields['goodNum'],2)
   # Reproduce the vendor's defaults when an update omits these fields.
   clone.update(goodName=fields['goodName'],goodPrice=fields['goodPrice'],isDeliveryPrice=fields.get('isDeliveryPrice',1),isTodayShipUse=fields.get('isTodayShipUse'),todayShipTime=fields.get('todayShipTime'))
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   with sqlite3.connect(root/'invitation_products.sqlite3') as db:
    db.execute('CREATE TABLE copies(key TEXT PRIMARY KEY,number TEXT,before TEXT)');db.execute('INSERT INTO copies VALUES(?,?,?)',(hashlib.sha256(f'{title}\0{93500}'.encode()).hexdigest(),'2','["1","3"]'))
   with patch.object(client,'products',return_value=[clone,active]),patch.object(client,'detail',side_effect=detail),patch.object(client,'set_sale',side_effect=sale),patch.object(client,'request',side_effect=request) as vendor,patch.object(client,'image_checksums',return_value=[]):
    links=client.ensure_links(title,root,1)
   self.assertEqual(set(links),{93500,99000});self.assertEqual(vendor.call_count,1);self.assertEqual(client.settings(clone),client.settings(template))
