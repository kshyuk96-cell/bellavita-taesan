"""Manual AS/redelivery cases with private evidence. Never recharges or prints."""
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime,timezone
from html import escape
from pathlib import Path
import delivery_photos
import production_files
import shop_dispatch

STATES={'requested':'접수','assigned':'담당 배정','in_progress':'처리 중','resolved':'해결','closed':'종결'}


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_service_cases (
        id TEXT PRIMARY KEY,order_id TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,
        assignee TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_service_events (
        case_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,assignee TEXT NOT NULL,
        note TEXT NOT NULL,actor TEXT NOT NULL,changed TEXT NOT NULL,PRIMARY KEY(case_id,version))''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_service_photos (
        id TEXT PRIMARY KEY,case_id TEXT NOT NULL,order_id TEXT NOT NULL,sha256 TEXT NOT NULL,
        note TEXT NOT NULL,actor TEXT NOT NULL,created TEXT NOT NULL,UNIQUE(case_id,sha256))''')


def create(c,fields,actor='admin-link'):
    key=fields.get('case_attempt','');order_id=fields.get('order_id','');kind=fields.get('case_kind');reason=fields.get('case_reason','').strip()
    if not re.fullmatch(r'[a-f0-9]{32}',key) or kind not in ('as','redelivery') or not 1<=len(reason)<=600:
        raise ValueError('AS·재배송 종류와 사유를 확인해 주세요.')
    if not c.execute('SELECT 1 FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(order_id,)).fetchone():raise ValueError('원주문을 찾지 못했습니다.')
    old=c.execute('SELECT * FROM shop_service_cases WHERE id=?',(key,)).fetchone()
    if old:
        if (old['order_id'],old['kind'],old['reason'])!=(order_id,kind,reason):raise ValueError('같은 AS 요청번호의 내용이 달라졌습니다.')
        return
    now=datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO shop_service_cases VALUES (?,?,?,?,'','requested',1,?,?)",(key,order_id,kind,reason,now,now))
    c.execute("INSERT INTO shop_service_events VALUES (?,1,'requested','',?,?,?)",(key,reason,actor,now))


def update(c,fields,actor='admin-link'):
    case=c.execute('SELECT * FROM shop_service_cases WHERE id=? AND order_id=?',(fields.get('case_id',''),fields.get('order_id',''))).fetchone()
    target=fields.get('case_state');owner=fields.get('case_assignee','');note=fields.get('case_note','').strip()
    if not case or case['state']=='closed' or fields.get('case_version')!=str(case['version']) or target not in STATES or owner and owner not in shop_dispatch.roster(c) or not 1<=len(note)<=600:
        raise ValueError('최신 AS 상태·담당자·처리 근거를 확인해 주세요.')
    permitted={'requested':('requested','assigned','closed'),'assigned':('assigned','in_progress','closed'),'in_progress':('in_progress','resolved'),'resolved':('resolved','closed')}
    if target not in permitted[case['state']] or target in ('assigned','in_progress') and not owner:raise ValueError('접수 → 배정 → 처리 중 → 해결 → 종결 순서를 확인해 주세요.')
    now=datetime.now(timezone.utc).isoformat();version=case['version']+1
    c.execute('UPDATE shop_service_cases SET state=?,assignee=?,version=?,updated=? WHERE id=?',(target,owner,version,now,case['id']))
    c.execute('INSERT INTO shop_service_events VALUES (?,?,?,?,?,?,?)',(case['id'],version,target,owner,note,actor,now))


def photo_path(root,case_id,photo_id):
    if not all(re.fullmatch(r'[a-f0-9]{32}',v) for v in (case_id,photo_id)):raise ValueError('invalid_service_photo_path')
    return Path(root)/'shop-service-photos'/case_id/(photo_id+'.jpg')


def upload(c,fields,root,actor='admin-link'):
    case=c.execute('SELECT * FROM shop_service_cases WHERE id=? AND order_id=?',(fields.get('case_id',''),fields.get('order_id',''))).fetchone()
    note=fields.get('photo_note','').strip()
    if not case or case['state']=='closed' or not 1<=len(note)<=600:raise ValueError('종결 전 AS 요청과 사진 설명을 확인해 주세요.')
    content=delivery_photos.normalized(fields.get('_photo_bytes'));digest=hashlib.sha256(content).hexdigest()
    if c.execute('SELECT 1 FROM shop_service_photos WHERE case_id=? AND sha256=?',(case['id'],digest)).fetchone():return
    if c.execute('SELECT count(*) FROM shop_service_photos WHERE case_id=?',(case['id'],)).fetchone()[0]>=10:raise ValueError('AS별 사진은 10개까지 보관합니다.')
    key=secrets.token_hex(16);production_files.write(photo_path(root,case['id'],key),content)
    c.execute('INSERT INTO shop_service_photos VALUES (?,?,?,?,?,?,?)',(key,case['id'],case['order_id'],digest,note,actor,datetime.now(timezone.utc).isoformat()))


def photo_link(photo):
    import order_ops
    expiry=str(int(time.time())+600);signature=order_ops.signature('service-photo-'+photo['case_id']+'-'+photo['id'],photo['order_id'],expiry,os.environ['TAESAN_WEBHOOK_SECRET'])
    return f'/shop/service-photo/{photo["order_id"]}/{photo["case_id"]}/{photo["id"]}/{expiry}/{signature}.jpg'


def panel(c,row,path):
    result='<h3>재배송·AS</h3><p>원주문에 처리 기록을 연결합니다. 결제·재출력·재배송은 자동 실행하지 않습니다.</p>'
    order=f'<input type="hidden" name="order_id" value="{row["id"]}">'
    result+=f'<form method="post" action="{escape(path)}">{order}<input type="hidden" name="action" value="create_service_case"><input type="hidden" name="case_attempt" value="{secrets.token_hex(16)}"><label>종류<select name="case_kind"><option value="as">AS</option><option value="redelivery">재배송</option></select></label><label>요청 사유<textarea name="case_reason" maxlength="600" required></textarea></label><button>AS·재배송 접수</button></form>'
    for case in c.execute('SELECT * FROM shop_service_cases WHERE order_id=? ORDER BY created,id',(row['id'],)):
        hidden=order+f'<input type="hidden" name="case_id" value="{case["id"]}"><input type="hidden" name="case_version" value="{case["version"]}">'
        result+='<details class="shop-service-case"><summary>'+escape(('AS' if case['kind']=='as' else '재배송')+' · '+STATES[case['state']]+' · '+case['reason'])+'</summary>'
        if case['state']!='closed':
            result+=f'<form method="post" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="update_service_case"><label>처리 담당<select name="case_assignee"><option value="">미배정</option>'
            for owner,name in shop_dispatch.roster(c).items():result+=f'<option value="{escape(owner)}"'+(' selected' if owner==case['assignee'] else '')+'>'+escape(name)+'</option>'
            result+='</select></label><label>처리 상태<select name="case_state">'
            for state,name in STATES.items():result+=f'<option value="{state}"'+(' selected' if state==case['state'] else '')+'>'+name+'</option>'
            result+='</select></label><label>처리 근거<textarea name="case_note" maxlength="600" required></textarea></label><button>AS 처리 저장</button></form>'
            result+=f'<form method="post" enctype="multipart/form-data" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="upload_service_photo"><label>추가 사진<input type="file" name="photo" accept="image/jpeg,image/png,image/webp" required></label><label>사진 설명<textarea name="photo_note" maxlength="600" required></textarea></label><button>AS 사진 추가</button></form>'
        for event in c.execute('SELECT * FROM shop_service_events WHERE case_id=? ORDER BY version',(case['id'],)):
            result+='<p>'+escape(f'v{event["version"]} · {STATES[event["state"]]} · {event["assignee"]} · {event["actor"]} · {event["note"]}')+'</p>'
        for photo in c.execute('SELECT * FROM shop_service_photos WHERE case_id=? ORDER BY created',(case['id'],)):
            result+='<p>'+escape(photo['note'])+f'</p><img src="{photo_link(photo)}" alt="AS 증빙 사진" style="width:100%;max-width:300px;height:auto">'
        result+='</details>'
    return result
