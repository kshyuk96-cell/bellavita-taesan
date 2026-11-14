"""Private, normalized delivery photographs and versioned customer acknowledgments."""
import hashlib
import io
import os
from datetime import datetime
from html import escape
import re
import secrets
import time
import warnings
from email import policy
from email.parser import BytesParser

from PIL import Image,ImageOps,UnidentifiedImageError
import partial_reviews
import production_files
from shop_dispatch import SEOUL

MAX_UPLOAD=5*1024*1024


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_delivery_photos (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL, sha256 TEXT NOT NULL,
        state TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
        note TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(order_id,sha256))''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_delivery_photo_changes (
        id INTEGER PRIMARY KEY, photo_id TEXT NOT NULL, version INTEGER NOT NULL,
        state TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, changed TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_delivery_confirms (
        order_id TEXT NOT NULL, photo_id TEXT NOT NULL, photo_version INTEGER NOT NULL,
        actor TEXT NOT NULL, confirmed TEXT NOT NULL, PRIMARY KEY(order_id,photo_id,photo_version))''')


def multipart(content_type,body):
    if len(body)>MAX_UPLOAD+32768:raise ValueError('사진은 5MB 이내로 업로드해 주세요.')
    message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
    if not message.is_multipart():raise ValueError('사진 업로드 형식을 확인해 주세요.')
    fields={};seen=set()
    for part in message.iter_parts():
        name=part.get_param('name',header='content-disposition')
        if not name or name in seen or len(seen)>=12 or part.is_multipart():raise ValueError('사진 업로드 항목을 확인해 주세요.')
        seen.add(name)
        content=part.get_payload(decode=True)
        if not isinstance(content,bytes):raise ValueError('사진 업로드 내용을 확인해 주세요.')
        if part.get_filename() is not None:
            if name=='csv' and (not content or len(content)>131072):raise ValueError('CSV는 비어 있지 않은 128KB 이내 파일로 업로드해 주세요.')
            if name not in ('photo','csv') or not content or len(content)>(131072 if name=='csv' else MAX_UPLOAD):raise ValueError('사진은 JPEG·PNG·WebP, 5MB 이내로 업로드해 주세요.')
            fields['_csv_bytes' if name=='csv' else '_photo_bytes']=content
        else:
            if len(content)>4096:raise ValueError('사진 설명이 너무 깁니다.')
            fields[name]=content.decode('utf-8')
    action=fields.get('action')
    if action=='preview_assignment_csv':
        if '_csv_bytes' not in fields:raise ValueError('CSV 파일을 선택해 주세요.')
    elif action not in ('upload_delivery_photo','upload_service_photo') or '_photo_bytes' not in fields:raise ValueError('지원하는 사진·CSV 업로드를 선택해 주세요.')
    return fields


def normalized(content):
    if not isinstance(content,bytes) or not content or len(content)>MAX_UPLOAD:raise ValueError('사진은 5MB 이내로 업로드해 주세요.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as probe:
                if probe.format not in ('JPEG','PNG','WEBP') or probe.width*probe.height>20_000_000 or getattr(probe,'n_frames',1)!=1:
                    raise ValueError('정지 사진 JPEG·PNG·WebP, 2천만 픽셀 이내로 업로드해 주세요.')
                probe.verify()
            with Image.open(io.BytesIO(content)) as raw:
                image=ImageOps.exif_transpose(raw).convert('RGB');image.thumbnail((1600,1600));output=io.BytesIO()
                image.save(output,format='JPEG',quality=88,optimize=True)
                return output.getvalue()
    except (OSError,SyntaxError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError('정상적인 정지 사진 JPEG·PNG·WebP로 다시 업로드해 주세요.') from None


def photo_path(root,order_id,photo_id):
    from pathlib import Path
    if not re.fullmatch(r'[a-f0-9]{32}',order_id) or not re.fullmatch(r'[a-f0-9]{32}',photo_id):raise ValueError('invalid_photo_path')
    return Path(root)/'shop-delivery-photos'/order_id/(photo_id+'.jpg')


def deliverable(c,order_id):
    row=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job JOIN shop_fulfillment f ON f.order_id=o.id WHERE o.id=? AND f.stage=\'delivered\'',(order_id,)).fetchone()
    return row if row and partial_reviews.allowed(c,row) else None


def upload(c,fields,root,actor='admin-link'):
    order_id=fields.get('order_id','');note=fields.get('photo_note','').strip()
    if not deliverable(c,order_id) or not 1<=len(note)<=600:raise ValueError('배송 완료와 결제 상태를 확인하고 사진 설명을 입력해 주세요.')
    content=normalized(fields.get('_photo_bytes'));digest=hashlib.sha256(content).hexdigest()
    old=c.execute('SELECT * FROM shop_delivery_photos WHERE order_id=? AND sha256=?',(order_id,digest)).fetchone()
    if old:return old['id']
    if c.execute('SELECT count(*) FROM shop_delivery_photos WHERE order_id=?',(order_id,)).fetchone()[0]>=10:raise ValueError('주문별 배송 사진은 10개까지 보관합니다.')
    photo_id=secrets.token_hex(16);production_files.write(photo_path(root,order_id,photo_id),content)
    created=datetime.now(SEOUL).isoformat()
    c.execute("INSERT INTO shop_delivery_photos VALUES (?,?,?,'pending',1,?,?,?)",(photo_id,order_id,digest,actor,note,created))
    c.execute("INSERT INTO shop_delivery_photo_changes(photo_id,version,state,actor,note,changed) VALUES (?,1,'pending',?,?,?)",(photo_id,actor,note,created))
    return photo_id


def publish(c,fields,root,actor='admin-link'):
    photo=c.execute('SELECT * FROM shop_delivery_photos WHERE id=? AND order_id=?',(fields.get('photo_id',''),fields.get('order_id',''))).fetchone()
    state=fields.get('photo_state');note=fields.get('photo_review_note','').strip()
    if (not photo or fields.get('photo_version')!=str(photo['version']) or state not in ('public','hidden')
            or fields.get('photo_checked')!='yes' or not 1<=len(note)<=600):raise ValueError('최신 사진·주문·고객 공개 여부와 근거를 확인해 주세요.')
    if state=='public':
        if not deliverable(c,photo['order_id']):raise ValueError('배송 완료·결제 대조 후 고객에게 공개할 수 있습니다.')
        try:content=photo_path(root,photo['order_id'],photo['id']).read_bytes()
        except OSError:raise ValueError('사진이 없습니다. 백업에서 복구해 주세요.') from None
        if hashlib.sha256(content).hexdigest()!=photo['sha256']:raise ValueError('사진 무결성을 확인해 주세요.')
    if photo['state']==state:return
    c.execute('UPDATE shop_delivery_photos SET state=?,version=version+1 WHERE id=?',(state,photo['id']))
    c.execute('INSERT INTO shop_delivery_photo_changes(photo_id,version,state,actor,note,changed) VALUES (?,?,?,?,?,?)',
              (photo['id'],photo['version']+1,state,actor,note,datetime.now(SEOUL).isoformat()))


def link(photo,audience='customer',lifetime=600):
    import order_ops
    expiry=str(int(time.time())+lifetime);kind='delivery-photo-'+audience+'-'+photo['id']
    signature=order_ops.signature(kind,photo['order_id'],expiry,os.environ['TAESAN_WEBHOOK_SECRET'])
    return f'/shop/delivery-photo/{audience}/{photo["order_id"]}/{photo["id"]}/{expiry}/{signature}.jpg'


def confirm(c,order_id,fields):
    import order_ops
    photo=c.execute("SELECT * FROM shop_delivery_photos WHERE id=? AND order_id=? AND state='public'",(fields.get('photo_id',''),order_id)).fetchone()
    expiry=fields.get('photo_expiry','');signature=fields.get('photo_signature','')
    if (not photo or fields.get('photo_version')!=str(photo['version']) or fields.get('photo_confirmed')!='yes'
            or not re.fullmatch(r'[0-9]{10}',expiry) or not re.fullmatch(r'[a-f0-9]{64}',signature) or not deliverable(c,order_id)
            or not order_ops.authorized('photo-confirm-'+photo['id']+'-'+str(photo['version']),order_id,expiry,signature,os.environ['TAESAN_WEBHOOK_SECRET'])):
        raise ValueError('최신 배송 사진을 다시 열어 확인해 주세요.')
    c.execute('INSERT OR IGNORE INTO shop_delivery_confirms VALUES (?,?,?,?,?)',(order_id,photo['id'],photo['version'],'customer-order-link',datetime.now(SEOUL).isoformat()))


def panel(c,row,path,staff=False):
    result='<h3>배송 완료 사진</h3>'
    if deliverable(c,row['id']):
        result+=f'<form method="post" enctype="multipart/form-data" action="{escape(path)}"><input type="hidden" name="action" value="upload_delivery_photo"><input type="hidden" name="order_id" value="{row["id"]}"><label>배송 사진 (JPEG·PNG·WebP, 5MB 이내)<input type="file" name="photo" accept="image/jpeg,image/png,image/webp" required></label><label>사진 설명<textarea name="photo_note" maxlength="600" required></textarea></label><button>배송 사진 업로드</button></form><p>업로드 사진은 관리자 검수·공개 후 고객에게 표시됩니다.</p>'
    for photo in c.execute('SELECT * FROM shop_delivery_photos WHERE order_id=? ORDER BY created,id',(row['id'],)):
        result+=f'<p>{escape(photo["note"])} · '+{'pending':'공개 검수 대기','public':'고객 공개','hidden':'고객 숨김'}[photo['state']]+f'</p><img src="{link(photo,"admin")}" alt="배송 완료 사진" style="width:100%;max-width:350px;height:auto">'
        if not staff:
            result+=f'<form method="post" action="{escape(path)}"><input type="hidden" name="action" value="publish_delivery_photo"><input type="hidden" name="order_id" value="{row["id"]}"><input type="hidden" name="photo_id" value="{photo["id"]}"><input type="hidden" name="photo_version" value="{photo["version"]}"><label>공개 결정<select name="photo_state"><option value="public">고객 공개</option><option value="hidden">고객 숨김</option></select></label><label>검수 근거<textarea name="photo_review_note" maxlength="600" required></textarea></label><label><input type="checkbox" name="photo_checked" value="yes" required>주문·사진·고객 공개 범위를 확인했습니다</label><button>배송 사진 공개 저장</button></form>'
    if not staff:
        for check in c.execute('SELECT a.* FROM shop_delivery_confirms a WHERE order_id=? ORDER BY confirmed',(row['id'],)):
            result+='<p>'+escape('고객 사진 확인 · '+check['confirmed'][:16]+' · '+check['actor'])+'</p>'
    if not staff:
        for change in c.execute('SELECT a.* FROM shop_delivery_photo_changes a JOIN shop_delivery_photos p ON p.id=a.photo_id WHERE p.order_id=? ORDER BY a.id',(row['id'],)):
            result+='<p>'+escape(f'사진 검수 v{change["version"]} · {change["state"]} · {change["actor"]} · {change["changed"][:16]} · {change["note"]}')+'</p>'
    return result


def customer(c,row):
    if not deliverable(c,row['id']):return ''
    import order_ops
    result='<section><h2>배송 완료 사진</h2>'
    for photo in c.execute("SELECT * FROM shop_delivery_photos WHERE order_id=? AND state='public' ORDER BY created,id",(row['id'],)):
        result+=f'<img src="{link(photo)}" alt="배송 완료 사진" style="width:100%;max-width:450px;height:auto">'
        check=c.execute('SELECT 1 FROM shop_delivery_confirms WHERE order_id=? AND photo_id=? AND photo_version=?',(row['id'],photo['id'],photo['version'])).fetchone()
        if check:result+='<p>배송 사진 확인을 기록했습니다.</p>';continue
        expiry=str(int(time.time())+86400);signed=order_ops.signature('photo-confirm-'+photo['id']+'-'+str(photo['version']),row['id'],expiry,os.environ['TAESAN_WEBHOOK_SECRET'])
        result+=f'<form method="post" action="/invitation-orders/{row["id"]}/delivery-confirm"><input type="hidden" name="photo_id" value="{photo["id"]}"><input type="hidden" name="photo_version" value="{photo["version"]}"><input type="hidden" name="photo_expiry" value="{expiry}"><input type="hidden" name="photo_signature" value="{signed}"><label><input type="checkbox" name="photo_confirmed" value="yes" required>배송 사진을 확인했습니다</label><button>고객 사진 확인</button></form>'
    return result+'</section>'
