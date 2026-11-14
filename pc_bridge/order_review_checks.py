"""Exact field checks and explicit change hints. Never infer another order's fields."""
import hashlib,json,re

INSCRIPTIONS=('sender','recipient','message')


def field_digest(fields):
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def compare_fields(fields,confirmed):
    result={}
    for key,value in confirmed.items():
        observed=str(fields.get(key) or '')
        if observed==value:status='exact'
        elif re.sub(r'\s+','',observed)==re.sub(r'\s+','',value):status='whitespace_mismatch'
        else:status='different'
        result[key]=status
    return result


def relationship(text):
    target=re.search(r'주문\s*번호\s*[:：#]?\s*([1-9][0-9]{0,8})',text)
    if re.search(r'취소|환불',text):
        return {'kind':'cancellation_request','target_hint':target[1] if target else None,'applied':False}
    if re.search(r'수정|변경|바꿔|바꾸|정정',text):
        return {'kind':'amendment_request' if target else 'unlinked_change_request',
                'target_hint':target[1] if target else None,'applied':False}
    if re.search(r'추가\s*주문|신규\s*주문|새\s*주문',text):
        return {'kind':'explicit_new_request','target_hint':None,'applied':False}
    return {'kind':'unconfirmed_order_candidate','target_hint':None,'applied':False}


def save_confirmation(db,key,confirmed,*,source_checked=False,expected_digest=None):
    if source_checked is not True:raise ValueError('카톡 원문과 대조한 뒤 확인해 주세요.')
    if not confirmed or set(confirmed)-set(INSCRIPTIONS):raise ValueError('확인할 문구 항목을 확인해 주세요.')
    if any(not isinstance(v,str) or not v.strip() or len(v)>1000 for v in confirmed.values()):
        raise ValueError('빈 문구 또는 너무 긴 문구는 확정할 수 없습니다.')
    row=db.execute('SELECT result FROM ocr_reviews WHERE id=?',(key,)).fetchone()
    if not row:raise ValueError('현재 주문 후보가 없습니다.')
    fields=json.loads(row[0]).get('fields',{})
    if expected_digest!=field_digest(fields):raise ValueError('분석 내용이 바뀌었습니다. 현재 원문과 다시 대조해 주세요.')
    db.execute('''CREATE TABLE IF NOT EXISTS ocr_confirmed_fields (
        id TEXT PRIMARY KEY,digest TEXT NOT NULL,fields TEXT NOT NULL,comparison TEXT NOT NULL,source TEXT NOT NULL)''')
    with db:db.execute('INSERT OR REPLACE INTO ocr_confirmed_fields VALUES (?,?,?,?,?)',
        (key,field_digest(fields),json.dumps(confirmed,ensure_ascii=False),
         json.dumps(compare_fields(fields,confirmed)), 'user_checked_chat_source'))


def current_confirmation(db,key,fields):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ocr_confirmed_fields'").fetchone():return None
    row=db.execute('SELECT digest,fields FROM ocr_confirmed_fields WHERE id=?',(key,)).fetchone()
    return json.loads(row[1]) if row and row[0]==field_digest(fields) else None
