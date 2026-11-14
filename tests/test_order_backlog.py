import json,re,unittest
import test_order_lifecycle
import order_operations as ops,order_assistance as assist

class BacklogTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    def seed(self,count=120):
        with self.db() as c:
            c.execute('DELETE FROM order_operations')
            for i in range(count):
                row=ops.stage(c,f'case-{i:04d}','staff','kakao','',{'sender':'모의회사'},'2026-11-14T12:00:00+09:00')
                c.execute('UPDATE order_operations SET order_number=? WHERE id=?',(1000+i,row['id']))
    def test_pages_include_older_than_100_without_duplicates(self):
        self.seed();command='주문 검토 대기';seen=[]
        for _ in range(100):
            page=assist.command(self.db,'staff',command);self.assertLess(len(page),950)
            seen.extend(re.findall(r'주문 ([0-9]+) —',page))
            cursor=re.search(r'다음: (주문 검토 대기 다음 [A-Za-z0-9_-]+)',page)
            if not cursor:break
            command=cursor[1]
        self.assertEqual(len(seen),120);self.assertEqual(len(set(seen)),120)
    def test_cursor_owner_and_filter_bound(self):
        self.seed();page=assist.command(self.db,'staff','주문 검토 대기 누락')
        token=re.search(r'다음: 주문 검토 대기 다음 ([A-Za-z0-9_-]+)',page)[1]
        self.assertIn('계정·필터',assist.command(self.db,'other','주문 검토 대기 다음 '+token))
        self.assertIn('계정·필터',assist.command(self.db,'staff','주문 검토 대기 취소 다음 '+token))
    def test_reason_filter_finds_old_failure(self):
        self.seed()
        with self.db() as c:assist.record(c,'case-0000',{'sender':'모의회사'},error='artwork')
        result=assist.command(self.db,'staff','주문 검토 대기 오류')
        self.assertIn('주문 1000',result);self.assertNotIn('주문 1119',result)
        self.assertIn('원장 저장값 불일치: 작품',result)
    def test_bad_cursor_is_held(self):self.assertIn('처음부터',assist.command(self.db,'staff','주문 검토 대기 다음 invalid'))
    def test_filter_cancel_does_not_include_ordinary_pending(self):
        self.seed(2)
        with self.db() as c:c.execute("UPDATE order_operations SET state='cancel_requested' WHERE id='case-0000'")
        result=assist.command(self.db,'staff','주문 검토 대기 취소')
        self.assertIn('주문 1000',result);self.assertNotIn('주문 1001',result)
