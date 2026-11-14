"""Test-only planning-room outbox. No network or UI sending implementation."""
import hashlib,json,contextlib
import order_intake

DESTINATION='기획방'

def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS order_share_jobs (
        id TEXT PRIMARY KEY,order_id TEXT NOT NULL,revision TEXT NOT NULL,destination TEXT NOT NULL,
        kind TEXT NOT NULL,body TEXT NOT NULL,body_digest TEXT NOT NULL,state TEXT NOT NULL,
        receipt_id TEXT,verified_at TEXT,UNIQUE(destination,receipt_id))''')
    c.execute('CREATE INDEX IF NOT EXISTS share_order_state ON order_share_jobs(order_id,state)')
    if 'native_checked' not in {row[1] for row in c.execute('PRAGMA table_info(order_share_jobs)')}:
        c.execute('ALTER TABLE order_share_jobs ADD COLUMN native_checked INTEGER NOT NULL DEFAULT 0')

def invalidate(c,key):
    c.execute("UPDATE order_share_jobs SET state='stale' WHERE order_id=? AND state!='stale'",(key,))
    c.execute('DELETE FROM order_transmissions WHERE order_id=?',(key,))

def enqueue(c,row,number,details):
    if row['kind']!='kakao':return
    generation=c.execute('SELECT COUNT(*) FROM order_changes WHERE order_id=?',(row['id'],)).fetchone()[0]
    canonical=json.dumps({'number':number,'details':details,'generation':generation},sort_keys=True,ensure_ascii=False)
    revision=hashlib.sha256(canonical.encode()).hexdigest()
    existing=c.execute('SELECT DISTINCT revision FROM order_share_jobs WHERE order_id=? AND state!=?',(row['id'],'stale')).fetchall()
    if any(r[0]!=revision for r in existing):invalidate(c,row['id'])
    report=order_intake.report(number,details)
    for kind,title in (('summary','테스트 주문 요약'),('form','테스트 주문서 요약 / 시트 원본 복사 미검증')):
        body=f'[{title}]\n실배송·청구 금지 / 기획방 테스트 전용\n'+report
        digest=hashlib.sha256(body.encode()).hexdigest()
        key=hashlib.sha256((row['id']+revision+kind+DESTINATION).encode()).hexdigest()
        c.execute('''INSERT OR IGNORE INTO order_share_jobs
            (id,order_id,revision,destination,kind,body,body_digest,state) VALUES (?,?,?,?,?,?,?,?)''',
            (key,row['id'],revision,DESTINATION,kind,body,digest,'form_review' if kind=='form' else 'waiting'))

@contextlib.contextmanager
def source_permission(c,key,source_guard):
    import customer_sources
    row=c.execute('SELECT context FROM order_sources WHERE order_id=?',(key,)).fetchone()
    if row and source_guard is None:raise customer_sources.SourceRevoked('원본 고객방 연결을 먼저 검증해야 합니다.')
    if not row:
        import order_origin
        if not order_origin.approved(c,key):raise order_origin.OriginUnverified('원본 출처 또는 직원 직접입력을 먼저 확인해야 합니다.')
    with source_guard(json.loads(row['context'])) if row else contextlib.nullcontext():yield


def record_verified_receipt(c,job_id,destination,body_digest,receipt_id,verified_at,*,source_guard=None):
    job=c.execute('SELECT order_id FROM order_share_jobs WHERE id=?',(job_id,)).fetchone()
    if not job:raise ValueError('Unknown share job')
    with source_permission(c,job[0],source_guard):
        return _record_verified_receipt(c,job_id,destination,body_digest,receipt_id,verified_at)


def _record_verified_receipt(c,job_id,destination,body_digest,receipt_id,verified_at):
    """Internal adapter hook: caller must already verify actual matching outgoing content.

    No user command/HTTP route exposes this hook. Tests supply synthetic receipts.
    """
    import datetime as dt
    if not c.in_transaction:c.execute('BEGIN IMMEDIATE')
    if destination!=DESTINATION or not receipt_id or not verified_at:raise ValueError('Verified planning-room receipt required')
    try:
        stamp=dt.datetime.fromisoformat(verified_at)
        if stamp.tzinfo is None:raise ValueError()
    except (TypeError,ValueError):raise ValueError('Timezone-aware verified receipt time required')
    job=c.execute("SELECT * FROM order_share_jobs WHERE id=?",(job_id,)).fetchone()
    if not job or job['state']=='stale' or job['destination']!=destination or job['body_digest']!=body_digest:raise ValueError('Receipt does not match the current queued content')
    operation=c.execute('SELECT state FROM order_operations WHERE id=?',(job['order_id'],)).fetchone()
    if not operation or operation['state']!='saved':raise ValueError('Only the current saved order can confirm sharing')
    if job['kind']=='form' and not job['native_checked']:raise ValueError('원본 주문서 검증 전에는 최종 공유 확인을 기록할 수 없어요.')
    if job['state']=='confirmed':
        if job['receipt_id']!=receipt_id:raise ValueError('Different receipt already recorded')
        return
    used=c.execute('SELECT id FROM order_share_jobs WHERE destination=? AND receipt_id=?',(destination,receipt_id)).fetchone()
    if used:raise ValueError('Receipt belongs to another queued message')
    c.execute("UPDATE order_share_jobs SET state='confirmed',receipt_id=?,verified_at=? WHERE id=?",(receipt_id,verified_at,job_id))
    jobs=c.execute("SELECT kind,state,receipt_id FROM order_share_jobs WHERE order_id=? AND revision=? AND state!='stale'",(job['order_id'],job['revision'])).fetchall()
    if {r['kind'] for r in jobs}=={'summary','form'} and all(r['state']=='confirmed' for r in jobs):
        receipts=json.dumps(sorted(r['receipt_id'] for r in jobs))
        c.execute('INSERT OR REPLACE INTO order_transmissions VALUES (?,?,?,?)',(job['order_id'],destination,receipts,verified_at))

def pending_summary(c,owner):
    rows=c.execute('''SELECT o.order_number,j.kind,j.state FROM order_share_jobs j JOIN order_operations o ON o.id=j.order_id
                      WHERE o.owner=? AND j.state!='stale' ORDER BY o.created DESC,j.kind LIMIT 20''',(owner,)).fetchall()
    return '기획방 공유 대기 / 실제 전송 기능 미연결\n'+'\n'.join(f'주문 {r[0]} / {r[1]} / '+('확인 기록 있음' if r[2]=='confirmed' else '원본 주문서 검증 대기' if r[2]=='form_review' else '전송·수신 미확인') for r in rows) if rows else '이 계정의 공유 대기 기록이 없어요.'


def attach_native_form(c,key,payload,spreadsheet_id,sheet_id,*,source_guard=None):
    """Internal read-adapter hook. No Sheet write, copying API or chat send occurs."""
    from native_order_form import inspect_form
    with source_permission(c,key,source_guard):
        if not c.in_transaction:c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM order_operations WHERE id=? AND kind='kakao'",(key,)).fetchone()
        if not row or row['state']!='saved':raise ValueError('확인된 현재 원장 주문만 대조할 수 있어요.')
        details=json.loads(row['details'])
        jobs=c.execute("SELECT * FROM order_share_jobs WHERE order_id=? AND kind='form' AND state!='stale'",(key,)).fetchall()
        if not jobs:
            enqueue(c,row,row['order_number'],details)
            jobs=c.execute("SELECT * FROM order_share_jobs WHERE order_id=? AND kind='form' AND state!='stale'",(key,)).fetchall()
        if len(jobs)!=1:raise ValueError('Current form job is ambiguous')
        job=jobs[0]
        try:result=inspect_form(payload,spreadsheet_id,sheet_id,row['order_number'],details)
        except ValueError:
            result={'ready_for_final_share':False,'hazards':[{'code':'native_read_mismatch','message':'주문서 파일·선택 행·범위를 다시 확인해야 합니다.'}]}
        if not result['ready_for_final_share']:
            if job['state']!='confirmed':c.execute("UPDATE order_share_jobs SET native_checked=0,state='form_review' WHERE id=?",(job['id'],))
            return result
        body='[기획방 검증된 원본 주문서 텍스트]\n실배송·청구 금지 / 기획방 테스트 전용\n'+result['text']
        digest=hashlib.sha256(body.encode()).hexdigest()
        if job['state']=='confirmed' and job['body_digest']!=digest:raise ValueError('이미 확인한 주문서와 달라 새 주문 변경 검토가 필요해요.')
        c.execute("UPDATE order_share_jobs SET body=?,body_digest=?,native_checked=1,state=CASE WHEN state='confirmed' THEN state ELSE 'waiting' END WHERE id=?",(body,digest,job['id']))
        return result
