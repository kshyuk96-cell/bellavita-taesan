import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

import erp_invitation_stats as stats

class ReadonlyInvitationStats(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.origin='https://render.example.invalid'
        self.file=self.root/'state.sqlite3'
        self.library=self.root/'erp-invitation-library'/'state.json'
        self.library.parent.mkdir()
        self.pair1='a'*32; self.pair2='b'*32; self.pair3='c'*32
        self.url=lambda job,price:f'{self.origin}/i/{job}/{price}/order'
        self.row=lambda when,job,price,prod,**more:{'date':when,'title':'가상행사','kind':'wedding','invitationId':'event_a',
            'createdAt':when+'T00:00:00Z','links':[{'orderUrl':self.url(job,price),'productId':prod}],**more}
        with sqlite3.connect(self.file) as db:
            db.executescript('CREATE TABLE invitation_orders(id TEXT PRIMARY KEY,job TEXT,price INTEGER,state TEXT,details TEXT);'
                             'CREATE TABLE order_operations(id TEXT PRIMARY KEY,order_number INTEGER);')
            for oid,job,price,state,prod in [('1'*32,self.pair1,93500,'paid','x'),('2'*32,self.pair2,99000,'bank_pending','y'),
                                             ('3'*32,self.pair3,93500,'cancelled','z')]:
                db.execute('INSERT INTO invitation_orders VALUES(?,?,?,?,?)',(oid,job,price,state,
                           json.dumps({'product':prod,'options':[['상품 선택','쌀빛 20kg']],'buyer_phone':'sensitive-fixture'})))
            db.execute('INSERT INTO order_operations VALUES(?,?)',('11111111-1111-1111-1111-111111111111',101))
    def test_exact_pairs_keep_old_versions_and_verify_order_state_without_writes(self):
        self.library.write_text(json.dumps({
            'old':self.row('2026-11-24',self.pair1,93500,'x',deleted=True),
            'new':self.row('2026-11-24',self.pair2,99000,'y'),
            'separate':self.row('2026-11-24',self.pair3,93500,'z',invitationId='event_b'),
        }))
        before=self.file.read_bytes(),self.library.read_bytes()
        result=stats.query(self.root,self.origin,{'ids':['new']})
        self.assertEqual([e['id'] for e in result['events']],['event:event_a'])
        e=result['events'][0];self.assertEqual(set(e['itemIds']),{'old','new'})
        self.assertEqual([(f['state'],f['legacyOrderId']) for f in e['facts']],[('paid','101'),('bank_pending','')])
        self.assertNotIn('sensitive-fixture',json.dumps(result))
        self.assertEqual(before,(self.file.read_bytes(),self.library.read_bytes()))
    def test_shared_order_pair_never_guesses_event_even_when_only_one_selected(self):
        self.library.write_text(json.dumps({'left':self.row('2026-11-23',self.pair1,93500,'x'),
            'right':self.row('2026-11-23',self.pair1,93500,'x',invitationId='event_b')}))
        result=stats.query(self.root,self.origin,{'ids':['left']})['events'][0]
        self.assertEqual(result['facts'],[])
        self.assertEqual(result['unlinked'],1)
        self.assertEqual(result['ambiguousUrls'],[self.url(self.pair1,93500)])
    def test_unavailable_catalog_is_not_empty_success_and_week_is_event_day(self):
        self.assertTrue(stats.query(self.root,self.origin,{'from':['2026-11-23'],'to':['2026-11-29'],'today':['2026-11-24']})['catalogUnavailable'])
        self.library.write_text(json.dumps({'x':self.row('2026-11-30',self.pair1,93500,'x'),
                                            'y':self.row('2026-11-29',self.pair2,99000,'y',invitationId='event_b')}))
        result=stats.query(self.root,self.origin,{'from':['2026-11-23'],'to':['2026-11-29'],'today':['2026-11-24']})
        self.assertEqual([e['id'] for e in result['events']],['event:event_b'])

    def test_memorials_follow_verified_start_and_departure_not_wedding_week(self):
        still_open={'invitationId':'open', 'date':'2026-11-29', 'kind':'memorial', 'createdAt':'2026-11-18',
                    'title':'발인 다음 월요일', 'links':[], 'values':{'entrance':'2026년 11월 22일',
                    'departure':'2026년 11월 30일 (월) 오전 9시'}}
        rows={
            'next_monday':still_open,
            'today':{**still_open,'invitationId':'today','values':{'departure':'2026-11-24 06:00'}},
            'past':{**still_open,'invitationId':'past','values':{'departure':'2026-11-23T01:00:00+09:00'}},
            'upcoming':{**still_open,'invitationId':'later','values':{'entrance':'2026-11-25','departure':'2026-11-27'}},
            'missing':{**still_open,'invitationId':'unknown','values':{'admission':'2026-11-24','deathDate':'2026-11-23'}},
            'alternate':{**still_open,'invitationId':'alternate',
                 'values':{'funeralDepartureAt':'2026-11-25T15:05:00Z','admission':'2026-11-25'}},
            'monday':self.row('2026-11-23',self.pair1,93500,'x',invitationId='wedding_mon'),
            'sunday':self.row('2026-11-29',self.pair2,99000,'y',invitationId='wedding_sun'),
            'next':self.row('2026-11-30',self.pair3,93500,'z',invitationId='wedding_out'),
        }
        self.library.write_text(json.dumps(rows,ensure_ascii=False))
        result=stats.query(self.root,self.origin,{'from':['2026-11-23'],'to':['2026-11-29'],'today':['2026-11-24']})
        dates={e['id']:e['date'] for e in result['events']}
        self.assertEqual(dates,{'event:wedding_mon':'2026-11-23','event:wedding_sun':'2026-11-29',
            'event:open':'2026-11-30','event:today':'2026-11-24','event:alternate':'2026-11-26'})
        self.assertEqual(result['dateCheck'],1)

    def test_exact_published_journal_supplies_departure_without_using_catalog_event_date(self):
        key='d'*32;journal=self.root/'journal.sqlite'
        with sqlite3.connect(journal) as c:
            c.execute('CREATE TABLE jobs(public_id TEXT,invitation_id TEXT,input TEXT,action TEXT,state TEXT,created_at TEXT)')
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(key,'obit',json.dumps({'kind':'memorial','values':{
                 'departure':'2026/11/26 (목) 8시','entrance':'2026-11-23'}}),'publish','done','2026-11-22'))
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(key,'obit',json.dumps({'kind':'memorial','values':{
                 'departure':'2026-11-25'}}),'publish','running','2026-11-23'))
        self.library.write_text(json.dumps({key:{'invitationId':'obit','date':'2026-11-02','kind':'memorial','links':[]}}))
        before=journal.read_bytes()
        result=stats.query(self.root,self.origin,{'from':['2026-11-23'],'to':['2026-11-29'],'today':['2026-11-24']},journal=journal)
        self.assertEqual([(e['id'],e['date']) for e in result['events']],[('event:obit','2026-11-26')])
        self.assertEqual(result['dateCheck'],0)
        self.assertEqual(before,journal.read_bytes())

    def test_yearless_or_conflicting_departure_is_not_guessed(self):
        import datetime as dt
        today=dt.date(2026,11,24)
        self.assertEqual(stats.memorial_window({'date':'2026-11-25','departure':'11월 25일'},None,today),('check',None))
        row={'departureDate':'2026-11-25','values':{'departure':'발인 2026-11-26'}}
        self.assertEqual(stats.memorial_window(row,None,today),('check',None))

    def test_actual_protected_bridge_dispatch_does_not_initialize_engine(self):
        import ast,hmac,io,os,re
        from unittest.mock import patch
        file=Path(stats.__file__).with_name('erp_invitations.py')
        root=ast.parse(file.read_text(encoding='utf-8'))
        fn=next(node for node in root.body if isinstance(node,ast.FunctionDef) and node.name=='dispatch')
        def no_engine(*args):self.fail('read-only stats route initialized the writing Engine')
        ns={'re':re,'os':os,'hmac':hmac,'engine':no_engine}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(file),'exec'),ns)
        dispatch=ns['dispatch']
        class Handler:
            path='/erp/invitations/order-stats?ids=missing'
            def __init__(self,auth=''):
                self.headers={'Authorization':auth};self.headers_out={};self.wfile=io.BytesIO()
            def send_json(self,body,status=200):self.status=status
            def send_response(self,status):self.status=status
            def send_header(self,k,v):self.headers_out[k]=v
            def end_headers(self):pass
        with patch.dict(os.environ,{'ERP_INVITATION_TOKEN':'local-fixture-only'},clear=True):
            unauth=Handler();self.assertTrue(dispatch(unauth,'GET','/erp/invitations/order-stats',self.root,self.origin,None,None))
            self.assertEqual(unauth.status,401)
            write=Handler('Bearer local-fixture-only');self.assertTrue(dispatch(write,'POST','/erp/invitations/order-stats',self.root,self.origin,None,None))
            self.assertEqual(write.status,405)
            read=Handler('Bearer local-fixture-only');self.assertTrue(dispatch(read,'GET','/erp/invitations/order-stats',self.root,self.origin,None,None))
            self.assertEqual(read.status,200)
            self.assertTrue(json.loads(read.wfile.getvalue())['catalogUnavailable'])
            self.assertEqual(read.headers_out.get('Cache-Control'),'private, no-store')
            self.assertFalse(self.library.exists())
