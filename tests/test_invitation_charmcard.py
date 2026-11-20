"""Charmcard HTTP/CP949 fields and original-gallery ingestion, without browser execution."""
import io
import socket
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
import invitation
import invitation_charmcard as charm
import erp_invitations as bridge

URL='http://charmcard.com/m/m9999/index.html'
def person(role,name):return '<div class="'+role+' fl"><div class="gb_box"><div class="name">이름 <span>'+name+'</span></div></div></div>'
SOURCE=('''<html><head><meta http-equiv="Content-Type" content="text/html; charset=euc-kr"><title>가상신랑 ♥ 가상신부</title></head><body>
<div class="top_space"><p class="top_space_t1">2099년 12월 19일 토요일 오전 11시 <br>가상웨딩컨벤션 3층 그랜드볼룸홀</p></div>
<div class="top_pic"><img src="img2/c9999-1.jpg"></div>
<p class="con_box_t3">소중한 날, 함께해 주세요. 뷁</p>'''+person('groom','가상신랑')+person('bride','가상신부')+person('groom_father','(故) 가상부친')+person('groom_mother','가상모친')+person('bride_father','다른부친')+person('bride_mother','다른모친')+
'''<div class="hall_detail"><p class="hall_name">가상웨딩컨벤션<br>3층 그랜드볼룸홀</p><p class="hall_addr">서울시 동작구 가상로5길 15 (가상동 395-70번지)</p></div>
<img src="img/location.png"><div class="swiper-container">'''+''.join('<div class="swiper-slide"><a href="image_view.asp?imgsrc=c9999-'+str(i)+'.jpg"><img src="img2/c9999-'+str(i)+'.jpg"></a></div>' for i in [2,3,4,5,6,7,1])+
'''<div class="swiper-slide"><img src="http://127.0.0.1/m/m9999/img2/evil.jpg"></div></div><script>throw new Error('scripts must not execute');</script></body></html>''')
RAW=SOURCE.encode('cp949')

class Charmcard(unittest.TestCase):
 def test_cp949_and_euckr_declaration_preserve_korean_fields_and_all_original_photos(self):
  decoded=charm.decode(RAW);self.assertIn('가상신랑',decoded);self.assertIn('뷁',decoded)
  result=charm.fields(URL,RAW)
  self.assertEqual(result['venue'],'가상웨딩컨벤션 3층 그랜드볼룸홀');self.assertEqual(result['address'],'서울시 동작구 가상로5길 15 (가상동 395-70번지)')
  self.assertEqual(result['families'],[('故 가상부친·가상모친','','가상신랑'),('다른부친·다른모친','','가상신부')])
  self.assertEqual(len(result['photos']),7);self.assertEqual(len(set(result['photos'])),7)
  self.assertTrue(all('/img2/' in url and 'image_view' not in url for url in result['photos']))
  self.assertTrue(result['warnings']);self.assertNotIn('�',decoded)
 def test_provider_fetch_does_not_require_ai_and_keeps_missing_birth_order_blank(self):
  with patch.object(charm,'fetch',return_value=RAW),patch('invitation.get_page',side_effect=AssertionError('ordinary HTTPS fetch must not run')):
   event=invitation.parse_source(URL,lambda _:(_ for _ in ()).throw(AssertionError('no AI extraction required')))
  self.assertEqual(event['name'],'가상신랑 가상신부 결혼');self.assertEqual(event['when'].isoformat(),'2099-12-19T11:00:00');self.assertEqual(len(event['photos']),7)
  self.assertTrue(all(not family[1] for family in event['families']));self.assertEqual(event['source'],URL)
 def test_http_exception_is_exact_host_path_only_and_attachments_are_preserved(self):
  invalid=['http://127.0.0.1/m/m9999/index.html','http://charmcard.com.evil.test/m/m9999/index.html','http://charmcard.com:8080/m/m9999/index.html','http://user@charmcard.com/m/m9999/index.html','http://charmcard.com/m/../index.html',URL+'?redirect=http://127.0.0.1',URL+'#fragment']
  for url in invalid:
   with self.subTest(url=url):self.assertFalse(charm.is_card_url(url))
  self.assertFalse(charm.is_image_url('http://charmcard.com/m/m9999/img/location.png'))
  self.assertTrue(charm.is_image_url('http://charmcard.com/m/m9999/img2/c9999-1.jpg'))
  value=bridge.clean_values({'sourceUrl':URL,'images':['attachment:'+'a'*32]});self.assertEqual(value['sourceUrl'],URL);self.assertEqual(value['images'],['attachment:'+'a'*32])
  with self.assertRaises(ValueError):bridge.clean_values({'sourceUrl':'http://example.org/card'})
 def test_public_http_connection_rejects_private_dns_before_connecting(self):
  with patch.object(socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]),patch.object(socket,'create_connection') as connect:
   with self.assertRaises(ValueError):charm.PublicHTTPConnection('charmcard.com',80).connect()
   connect.assert_not_called()
 def test_fetch_never_follows_redirect_or_calls_scripts_and_restricts_image_content_type(self):
  class Response:
   status=302
   def read(self,_):return RAW
   def getheader(self,_):return 'text/html'
  connection=SimpleNamespace(request=lambda *args,**kwargs:None,getresponse=lambda:Response(),close=lambda:None)
  with patch.object(charm,'PublicHTTPConnection',return_value=connection),patch.object(charm,'getproxies',return_value={}):
   with self.assertRaisesRegex(ValueError,'302'):charm.fetch(URL)
   Response.status=200
   with self.assertRaisesRegex(ValueError,'사진 형식'):charm.fetch('http://charmcard.com/m/m9999/img2/c9999-1.jpg',image=True)
 def test_configured_runtime_proxy_still_requests_only_the_fixed_public_card(self):
  response=SimpleNamespace(status=200,read=lambda _:RAW,getheader=lambda _:'text/html')
  seen=[];opener=SimpleNamespace(open=lambda request,**kwargs:(seen.append(request.full_url) or nullcontext(response)))
  with patch.object(charm,'getproxies',return_value={'http':'http://runtime-proxy.test'}),patch.object(socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',80))]),patch.object(charm,'build_opener',return_value=opener),patch.object(charm,'PublicHTTPConnection') as direct:
   self.assertEqual(charm.fetch(URL),RAW);self.assertEqual(seen,[URL]);direct.assert_not_called()
 def test_erp_parse_downloads_http_originals_to_internal_attachments_and_keeps_real_fields(self):
  def fetch(url,**kwargs):
   if not kwargs.get('image'):return RAW
   index=int(url.rsplit('-',1)[1].split('.')[0]);stream=io.BytesIO();Image.new('RGB',(20+index,30),(index*25,40,80)).save(stream,format='JPEG');return stream.getvalue()
  with tempfile.TemporaryDirectory() as temporary,patch.object(charm,'fetch',side_effect=fetch):
   engine=bridge.Engine(Path(temporary),'https://erp.test',invitation,lambda _:(_ for _ in ()).throw(AssertionError('AI must not run')),async_jobs=False)
   try:
    result=engine.parse({'requestId':'charmcard-parser-fixture','kind':'wedding','source':URL,'automatic':False})
    values=result['values'];self.assertEqual(values['groomName'],'가상신랑');self.assertEqual(values['brideName'],'가상신부')
    self.assertEqual(values['date'],'2099-12-19');self.assertEqual(values['time'],'11:00');self.assertIn('3층 그랜드볼룸홀',values['venue']);self.assertIn('395-70',values['address'])
    self.assertEqual(values['groomRelation'],'');self.assertEqual(values['brideRelation'],'');self.assertEqual(len(values['images']),7)
    self.assertTrue(all(url.startswith('attachment:') for url in values['images']));self.assertTrue(values['hero'].startswith('attachment:'))
    with engine.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0],7)
    self.assertTrue(any('관계' in warning for warning in result['warnings']))
   finally:engine.executor.shutdown(wait=True)

if __name__=='__main__':unittest.main()
