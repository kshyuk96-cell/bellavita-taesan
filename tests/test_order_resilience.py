import contextlib,json,multiprocessing,sqlite3,time,unittest
from unittest.mock import Mock,patch
import test_order_lifecycle
import order_operations as ops
import order_assistance as assist
import order_lifecycle as life
import order_sharing as sharing
from native_form_fake import native_payload
from order_lock import writer_lock

# Module-level subprocess targets for actual process-boundary lock verification.
def hold_lock(path,ready,release):
    @contextlib.contextmanager
    def db():
        c=sqlite3.connect(path)
        try:
            with c:yield c
        finally:c.close()
    with writer_lock(db):ready.set();release.wait(10)

def acquire_lock(path,acquired):
    @contextlib.contextmanager
    def db():
        c=sqlite3.connect(path)
        try:
            with c:yield c
        finally:c.close()
    with writer_lock(db):acquired.set()

class ResilienceTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    row=test_order_lifecycle.LifecycleTests.row
    def save(self):
        with self.db() as c:
            details={**self.original,'event_type':'결혼','artwork':'모의작품','size':'2호','delivery_date':'2099-12-01','delivery_time':'10:00','notes':'배송시각 10:00'}
            c.execute("UPDATE order_operations SET state='pending',details=? WHERE id='parent'",(json.dumps(details),))
        return ops.save(self.db,'parent',Mock(),lambda key,data,created,update:(21380,data))
    def validate_native(self):
        with self.db() as c:
            details=json.loads(c.execute("SELECT details FROM order_operations WHERE id='parent'").fetchone()[0])
            result=sharing.attach_native_form(c,'parent',native_payload(21380,details),'synthetic',5)
            self.assertTrue(result['ready_for_final_share'])
    def jobs(self):
        with self.db() as c:return [dict(r) for r in c.execute("SELECT * FROM order_share_jobs WHERE state!='stale'").fetchall()]
    def test_retry_budget_backoff_and_explicit_resume(self):
        with self.db() as c:c.execute("UPDATE order_operations SET state='pending' WHERE id='parent'")
        for count in range(1,6):
            with self.db() as c,patch.object(ops.time,'time',return_value=1000):ops.defer(c,'parent')
            row=self.row();self.assertEqual(row['attempts'],count);self.assertEqual(row['retry_after'],1000+60*2**(count-1))
        self.assertEqual(self.row()['state'],'review_hold')
        writer=Mock()
        with self.assertRaises(ValueError):ops.save(self.db,'parent',Mock(),writer)
        writer.assert_not_called()
        self.assertIn('재시도',assist.command(self.db,'staff','주문 재시도 21380'))
        self.assertEqual((self.row()['state'],self.row()['attempts']),('update',0))
    def test_early_retry_does_not_consume_attempt_budget(self):
        with self.db() as c:
            c.execute("UPDATE order_operations SET state='pending',retry_after=? WHERE id='parent'",(time.time()+1000,))
        try:ops.save(self.db,'parent',Mock(),Mock())
        except ops.RetryLater as error:
            with self.db() as c:ops.defer(c,'parent',error)
        else:self.fail('Early retry allowed')
        self.assertEqual(self.row()['attempts'],0)

    def test_defer_cannot_resurrect_cancel_or_saved(self):
        for state in ('saved','cancel_requested'):
            with self.db() as c:c.execute('UPDATE order_operations SET state=?',(state,));ops.defer(c,'parent')
            self.assertEqual(self.row()['attempts'],0)
    def test_only_two_jobs_and_both_receipts_required(self):
        self.save();ops.save(self.db,'parent',Mock(),Mock())
        self.validate_native()
        jobs=self.jobs();self.assertEqual(len(jobs),2)
        for i,job in enumerate(jobs):
            with self.db() as c:
                sharing.record_verified_receipt(c,job['id'],'기획방',job['body_digest'],f'synthetic-{i}','2026-11-14T12:00:00+09:00')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM order_transmissions').fetchone()[0],i)
        self.assertIn('카톡 공유: 기획방',assist.command(self.db,'staff','주문 상태 21380'))
    def test_wrong_room_digest_and_reused_receipt_held(self):
        self.save();self.validate_native();first,second=self.jobs()
        for dest,digest in (('확인방',first['body_digest']),('기획방','wrong')):
            with self.db() as c,self.assertRaises(ValueError):sharing.record_verified_receipt(c,first['id'],dest,digest,'synthetic','2026-11-14T12:00:00+09:00')
        with self.db() as c:sharing.record_verified_receipt(c,first['id'],'기획방',first['body_digest'],'synthetic','2026-11-14T12:00:00+09:00')
        with self.db() as c,self.assertRaises(ValueError):sharing.record_verified_receipt(c,second['id'],'기획방',second['body_digest'],'synthetic','2026-11-14T12:00:00+09:00')
    def test_change_invalidates_old_jobs_and_cancel_blocks_receipts(self):
        self.save();old=self.jobs()[0]
        life.command(self.db,'staff','주문 수정 21380\n보내는분: 다른 모의회사')
        self.assertEqual(self.jobs(),[])
        with self.db() as c,self.assertRaises(ValueError):sharing.record_verified_receipt(c,old['id'],'기획방',old['body_digest'],'old','2026-11-14T12:00:00+09:00')
        ops.save(self.db,'parent',Mock(),lambda key,data,created,update:(21380,data));new=self.jobs()[0]
        life.command(self.db,'staff','주문 취소 21380')
        with self.db() as c,self.assertRaises(ValueError):sharing.record_verified_receipt(c,new['id'],'기획방',new['body_digest'],'new','2026-11-14T12:00:00+09:00')
    def test_reverting_content_creates_new_share_revision(self):
        self.save();old={j['id'] for j in self.jobs()}
        for sender in ('다른 모의회사','모의회사'):
            life.command(self.db,'staff','주문 수정 21380\n보내는분: '+sender)
            ops.save(self.db,'parent',Mock(),lambda key,data,created,update:(21380,data))
        self.assertTrue(old.isdisjoint({j['id'] for j in self.jobs()}))
    def test_specific_verification_reason_is_visible(self):
        with self.db() as c:assist.record(c,'parent',self.original,error='artwork')
        self.assertIn('원장 저장값 불일치: 작품',assist.command(self.db,'staff','주문 상태 21380'))

    def test_process_lock_blocks_and_releases_after_crash(self):
        ctx=multiprocessing.get_context('spawn');ready=ctx.Event();release=ctx.Event();acquired=ctx.Event()
        holder=ctx.Process(target=hold_lock,args=(str(self.path),ready,release));waiter=ctx.Process(target=acquire_lock,args=(str(self.path),acquired))
        try:
            holder.start();self.assertTrue(ready.wait(3));waiter.start();self.assertFalse(acquired.wait(.15))
            holder.terminate();holder.join(3);self.assertTrue(acquired.wait(3));waiter.join(3)
            self.assertEqual(waiter.exitcode,0)
        finally:
            for process in (holder,waiter):
                if process.is_alive():process.terminate();process.join(3)

def concurrent_save(path,start,results):
    @contextlib.contextmanager
    def db():
        c=sqlite3.connect(path,timeout=5);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def writer(key,data,created,update):
        with db() as c:c.execute('INSERT INTO synthetic_writes VALUES (?)',(key,))
        return 21380,data
    start.wait(5)
    try:results.put(ops.save(db,'parent',Mock(),writer)[0])
    except Exception as error:results.put(type(error).__name__)

class ConcurrentSaveTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    def test_two_processes_one_actual_writer_call(self):
        with self.db() as c:
            c.execute('CREATE TABLE synthetic_writes (id TEXT PRIMARY KEY)')
            c.execute("UPDATE order_operations SET state='pending' WHERE id='parent'")
        ctx=multiprocessing.get_context('spawn');start=ctx.Event();results=ctx.Queue()
        workers=[ctx.Process(target=concurrent_save,args=(str(self.path),start,results)) for _ in range(2)]
        try:
            for process in workers:process.start()
            start.set();self.assertEqual([results.get(timeout=5) for _ in workers],[21380,21380])
            for process in workers:process.join(5);self.assertEqual(process.exitcode,0)
            with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM synthetic_writes').fetchone()[0],1)
        finally:
            for process in workers:
                if process.is_alive():process.terminate();process.join(3)
