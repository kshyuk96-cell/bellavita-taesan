"""Only the explicitly chosen customer's public account may reach a saved parse result."""
import datetime as dt,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import erp_memorial_invitation as m
import erp_invitations as bridge

URL='https://www.wooribugo4.com/page/funeral/view.php?_=fixture&_m=123'
SOURCE='''<html><body><section class="intro"><h1>故 <p>가상고인 (여, 80세)</p></h1></section>
<ul class="name-wrap"><li><p class="title">상주</p><div class="names"><p><b>김병섭</b></p><p><b>김다른</b></p></div></li><li><p class="title">딸</p><div class="names"><p><b>가상가족</b></p></div></li></ul>
<p>임종일</p><p>2099년 11월 19일 15시 00분</p><p>입실일</p><p>2099년 11월 19일 20시 00분</p><p>입관일</p><p>2099년 11월 20일 16시 00분</p><p>발인일</p><p>2099년 11월 21일 09시 30분</p>
<p>장례식장</p><p>가상장례식장 특1호실</p><p>1차 장지</p><p>가상승화원</p><p>2차 장지</p><p>가상선영</p><p>알림글</p><p>조문을 부탁드립니다.</p>
<div class="map-wrap"><p id="copytarget_addr">서울 서대문구 가상로 10-1</p><a href="tel:0200000000">장례식장</a></div>
<div class="user-list"><b class="name">김병섭</b><a href="tel:01000000001">전화</a></div>
<script>function load_account(){return '/page/ajax/funeral/ajax_funeral_account_load.php';}</script></body></html>'''
def account(name,number,bank='가상은행',index='0',visible=None,copy=True):
 return f'''<div class="user-list"><div class="item0"><b class="name">상주</b><span copybank-mourner_name="{index}">{name}</span>{('<button copybank="'+index+'">계좌복사</button>') if copy else ''}</div><span copybank-name="{index}">{name}</span><span copybank-bank="{index}">{bank}</span><span class="bank_num">{visible or number}</span><span class="bank_num_copy" style="display:none" copybank-num="{index}">{number}</span></div>'''
ACCOUNTS=account('김병섭','111-0000-0001')+account('김다른','222-0000-0002',index='1')

class TargetAccount(unittest.TestCase):
 def parse(self,target='',accounts=ACCOUNTS):return m.extract(URL,lambda url:SOURCE,target_mourner=target,account_fetcher=lambda url,fields:accounts)
 def test_all_obituary_details_and_family_names_preserved_but_only_exact_customer_account_returned(self):
  v,w=self.parse('김병섭 상주님');self.assertEqual(len(v['mournerAccounts']),1);self.assertEqual(v['mournerAccounts'][0]['name'],'김병섭');self.assertEqual(v['mournerAccounts'][0]['account'],'111-0000-0001')
  self.assertNotIn('222-0000-0002',json.dumps(v,ensure_ascii=False));self.assertIn('김다른',v['mourners']);self.assertIn('가상가족',v['mourners'])
  self.assertEqual(v['deceasedName'],'가상고인');self.assertIn('80세',v['deceasedDescription']);self.assertEqual(v['date'],'2099-11-21');self.assertEqual(v['time'],'09:30');self.assertEqual(v['venue'],'가상장례식장');self.assertEqual(v['funeralRoom'],'특1호실');self.assertEqual(v['address'],'서울 서대문구 가상로 10-1');self.assertEqual(v['burial'],'가상승화원 / 가상선영');self.assertTrue(v['deathDate']);self.assertTrue(v['admission']);self.assertTrue(v['entrance']);self.assertEqual(v['contactPhone'],'01000000001')
 def test_missing_target_partial_name_different_name_and_ambiguous_accounts_never_fall_back(self):
  for target in ['', '김병','김병섭X','없는상주']:
   v,w=self.parse(target);self.assertEqual(v['mournerAccounts'],[]);self.assertTrue(any('계좌' in x for x in w));self.assertNotIn('111-0000-0001',json.dumps(v))
  v,w=self.parse('김병섭',ACCOUNTS+account('김병섭','333-0000-0003',index='2'));self.assertEqual(v['mournerAccounts'],[])
 def test_same_row_official_copy_value_is_authorized_but_unpublished_hidden_full_number_not_used(self):
  rows=m.account_rows(account('김병섭','111-0000-0001',visible='111-****-0001'));self.assertEqual(rows[0]['account'],'111-0000-0001')
  rows=m.account_rows(account('김병섭','111-0000-0001',visible='111-****-0001',copy=False));self.assertEqual(rows[0]['account'],'111-****-0001')
  v,w=self.parse('김병섭',account('김병섭','111-****-0001'));self.assertEqual(v['mournerAccounts'][0]['account'],'111-****-0001');self.assertTrue(any('가려' in x for x in w))
 def test_target_normalizer_is_terminal_only_and_handles_manual_product_prefix(self):
  for target in ['김병섭','김병섭 상주님','김병섭(상주님)','김병섭 회장님 모친상',' 김 병 섭 대표님 ']:self.assertEqual(m.normalize_mourner_name(target),'김병섭')
  self.assertNotEqual(m.normalize_mourner_name('김병섭X'),'김병섭');self.assertNotEqual(m.normalize_mourner_name('김병'),'김병섭')
 def test_literal_person_names_ending_in_role_words_are_never_shortened(self):
  for source,expected in [('김상주','김상주'),('김상주님','김상주'),('김 상주님','김상주'),('김상주 상주님','김상주'),('김상주상주님','김상주'),('김상주모친상','김상주'),('김병섭상주님','김병섭'),('정명천회장님','정명천'),('회장님','')]:
   with self.subTest(source=source):self.assertEqual(m.normalize_mourner_name(source),expected)
 def test_person_name_role_collision_cannot_select_a_different_customers_account(self):
  rows=[{'name':'김','bank':'가상은행','account':'222-0000-0002'},{'name':'김상주','bank':'가상은행','account':'111-0000-0001'}]
  selected=m.clean_mourner_accounts(rows,'김상주님')
  self.assertEqual(selected,[rows[1]]);self.assertEqual(m.clean_mourner_accounts([rows[0]],'김상주님'),[])
 def test_no_target_does_not_even_fetch_account_popup(self):
  with patch('builtins.print') as output:
   fetch=lambda *args:(_ for _ in ()).throw(AssertionError('unnecessary account collection'))
   v,w=m.extract(URL,lambda url:SOURCE,account_fetcher=fetch);self.assertEqual(v['mournerAccounts'],[]);output.assert_not_called()
 def test_clean_values_drops_stale_other_customer_accounts_before_storage(self):
  value=bridge.clean_values({'host':'김병섭 상주님','mournerAccounts':[{'name':'김다른','bank':'가상은행','account':'222-0000-0002'},{'name':'김병섭','bank':'가상은행','account':'111-0000-0001'}]})
  self.assertEqual([r['name'] for r in value['mournerAccounts']],['김병섭']);self.assertNotIn('222-0000-0002',json.dumps(value))
 def test_all_relationship_groups_keep_literal_source_names_without_surname_inference(self):
  groups=[('상주',['김병섭','김다른']),('자부',['가상며느리']),('딸',['가상딸']),('사위',['가상사위']),('손',['가상손자','가상손녀']),('외손',['가상외손'])]
  family='<ul class="name-wrap">'+''.join('<li><p class="title">'+role+'</p><div class="names">'+''.join('<p><b>'+name+'</b></p>' for name in names)+'</div></li>' for role,names in groups)+'</ul>'
  v,w=m.extract(URL,lambda _:SOURCE+family,target_mourner='김병섭',account_fetcher=lambda *args:ACCOUNTS)
  for role,names in groups:self.assertIn(role+': '+', '.join(names),v['mourners'])
  self.assertEqual(v['mournerAccounts'][0]['name'],'김병섭');self.assertNotIn('222-0000-0002',json.dumps(v))
 def test_bank_popup_and_notice_cannot_leak_unselected_account_into_other_fields(self):
  embedded=SOURCE.replace('조문을 부탁드립니다.','김다른 222-0000-0002')+account('김다른','222-0000-0002')
  v,w=m.extract(URL,lambda _:embedded,target_mourner='김병섭',account_fetcher=lambda *args:ACCOUNTS)
  self.assertEqual(v['originalNotice'],'');self.assertNotIn('222-0000-0002',json.dumps(v));self.assertIn('김다른',v['mourners'])
 def test_conflicting_account_after_many_unrelated_rows_still_fails_closed(self):
  rows=[{'name':'김병섭','account':'111-0000-0001'}]+[{'name':'다른'+str(i),'account':'200-0000-'+str(i)} for i in range(110)]+[{'name':'김병섭','account':'333-0000-0003'}]
  self.assertEqual(m.clean_mourner_accounts(rows,'김병섭 상주님'),[])
 def test_parse_job_journal_contains_no_other_customers_account(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(m,'fetch_source',side_effect=lambda url,bridge,**kw:ACCOUNTS if kw.get('method')=='POST' else SOURCE):
   engine=bridge.Engine(Path(tmp),'https://erp.test',None,None,async_jobs=False)
   result=engine.submit({'requestId':'memorial-target-parse-01','invitationId':'memorial-target-invitation','version':1,'kind':'memorial','action':'parse','orderMode':'invitation-checkout','source':URL,'values':{'host':'김병섭 상주님','productPrefix':'김병섭 상주님 모친상'}})
   self.assertEqual(result['status'],'done');self.assertEqual(result['values']['host'],'김병섭 상주님')
   with engine.db() as c:raw=c.execute('SELECT result FROM jobs').fetchone()[0]
   self.assertIn('111-0000-0001',raw);self.assertNotIn('222-0000-0002',raw);self.assertIn('김다른',raw)
   engine.executor.shutdown(wait=True)

if __name__=='__main__':unittest.main()
