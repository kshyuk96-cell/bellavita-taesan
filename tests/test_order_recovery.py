import json,unittest
from unittest.mock import Mock
import test_order_lifecycle
import order_operations as ops
import order_ledger as ledger

class RecoveryTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    row=test_order_lifecycle.LifecycleTests.row
    def test_write_completed_then_timeout_reuses_same_marker(self):
        with self.db() as c:c.execute("UPDATE order_operations SET state='pending',order_number=NULL WHERE id='parent'")
        remote={};writes=[]
        def writer(key,data,created,update):
            if key not in remote:remote[key]=(21380,dict(data));writes.append(key);raise TimeoutError()
            return remote[key]
        with self.assertRaises(TimeoutError):ops.save(self.db,'parent',Mock(),writer)
        self.assertEqual(self.row()['state'],'pending')
        number,_=ops.save(self.db,'parent',Mock(),writer)
        self.assertEqual(number,21380);self.assertEqual(writes,['parent']);self.assertEqual(self.row()['state'],'saved')
    def test_notification_failure_rolls_back_and_retries_same_order(self):
        with self.db() as c:c.execute("UPDATE order_operations SET state='update' WHERE id='parent'")
        def callback(c,row,number,details):
            c.execute("INSERT INTO order_review VALUES (?,?,?)",(row['id'],'staff','synthetic'))
            raise RuntimeError()
        writer=Mock(side_effect=lambda key,data,created,update:(21380,data))
        with self.assertRaises(RuntimeError):ops.save(self.db,'parent',Mock(),writer,callback)
        self.assertEqual(self.row()['state'],'update')
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_review').fetchone()[0],0)
        ops.save(self.db,'parent',Mock(),writer)
        self.assertTrue(all(call.kwargs['update'] for call in writer.call_args_list))
    def test_real_ledger_marker_retry_after_applied_batch_timeout(self):
        state={};calls=[];session=Mock()
        def post(url,**kwargs):
            calls.append(url)
            for request in kwargs['json']['requests']:
                change=request['updateCells'];state[change['start']['columnIndex']]=change['rows'][0]['values'][0].get('userEnteredValue',{})
            raise TimeoutError()
        session.post.side_effect=post
        class FakeLedger(ledger.Ledger):
            def row_cells(self,row):
                cells=[{'userEnteredValue':state[i]} if i in state else {} for i in range(29)]
                for index,value in ((4,'2호'),(6,'결혼'),(8,'구로')):cells[index]['dataValidation']={'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':value}]}}
                return cells
            def values(self,rng):
                if rng=="'액자화환'!A4:K4":return [ledger.HEADER]
                if rng=="'액자화환'!A5:A":return []
                if rng=="'액자화환'!AC5:AC":return [[state[28].get('stringValue','')]] if 28 in state else []
                if rng=="'배송채널DB'!A2:D200":return []
                if rng=="'액자화환'!A5:AC5":return [[next(iter(state[i].values()),'') if i in state else '' for i in range(29)]]
                raise AssertionError(rng)
        book=FakeLedger(session,'synthetic-sheet');data={'event_type':'결혼','venue':'모의홀','sender':'모의회사','recipient':'모의수신자'}
        with self.assertRaises(TimeoutError):book.save('synthetic-marker',data,'2026-11-14T09:00:00+09:00')
        number,_=book.save('synthetic-marker',data,'2026-11-14T09:00:00+09:00')
        self.assertEqual(number,5);self.assertEqual(len(calls),1)
