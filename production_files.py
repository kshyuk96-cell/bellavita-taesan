"""Versioned PNG/raster-PDF artifacts and explicit production review."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from functools import lru_cache
from html import escape
from datetime import datetime, timezone

from PIL import Image, ImageDraw
import order_sample
import partial_reviews

RENDERER = 'bellavita-text-layout-v1'


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_artifacts (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, source_hash TEXT NOT NULL,
        layout TEXT NOT NULL, png_hash TEXT NOT NULL, pdf_hash TEXT NOT NULL,
        state TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(order_id,version), UNIQUE(order_id,source_hash))''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_artifact_reviews (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, version INTEGER NOT NULL,
        decision TEXT NOT NULL, reviewer TEXT NOT NULL, actor TEXT NOT NULL,
        note TEXT NOT NULL, checked TEXT NOT NULL)''')


def required(row):
    return json.loads(row['details']).get('product_snapshot', {}).get('production_review_required', False)


def latest(c, order_id):
    return c.execute('SELECT * FROM shop_artifacts WHERE order_id=? ORDER BY version DESC LIMIT 1', (order_id,)).fetchone()


def latest_review(c, order_id):
    return c.execute('SELECT * FROM shop_artifact_reviews WHERE order_id=? ORDER BY id DESC LIMIT 1', (order_id,)).fetchone()


def approved(c, row):
    artifact = latest(c, row['id'])
    return artifact if artifact and artifact['state'] == 'approved' and artifact['source_hash'] == source_hash(row, json.loads(artifact['layout'])) else None


def allowed(c, row):
    return approved(c, row) is not None if required(row) or latest(c, row['id']) else True


@lru_cache(maxsize=8)
def font_digest(path,mtime,size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_hash(row, layout):
    font=order_sample.font_path();stat=font.stat()
    data = json.dumps({'renderer':RENDERER,'order_id':row['id'],'details':json.loads(row['details']),
                       'layout':layout,'font':font_digest(str(font),stat.st_mtime_ns,stat.st_size)},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


def layout_for(fields):
    try:
        layout = {key:int(fields.get(key, default)) for key,default in (('width_mm',300),('height_mm',900),('dpi',150))}
    except (ValueError, TypeError):
        raise ValueError('출력 규격은 정수로 입력해 주세요.') from None
    w,h,dpi = (layout[k] for k in ('width_mm','height_mm','dpi'))
    if not 50 <= w <= 600 or not 150 <= h <= 1800 or dpi not in (150,300) or w*h*(dpi/25.4)**2 > 16_000_000:
        raise ValueError('지원 규격은 폭 50~600mm·높이 150~1800mm·150/300dpi·1,600만 픽셀 이내입니다.')
    return layout


def render(row, layout):
    w,h = (round(layout[k]*layout['dpi']/25.4) for k in ('width_mm','height_mm'))
    image=Image.new('RGB',(w,h),'#fffefa');draw=ImageDraw.Draw(image)
    margin=max(12,round(w*.08));draw.rectangle((margin//2,margin//2,w-margin//2,h-margin//2),outline='#ad9a6a',width=max(1,w//400))
    data=json.loads(row['details']);options=dict(data['options'])
    order_sample.fit(draw,options.get('상품 선택','쌀빛그림화환'),(margin,h*.06,w-margin,h*.15),max(16,w//17))
    order_sample.fit(draw,data['message'] or options.get('축하 문구',''),(margin,h*.18,w-margin,h*.63),max(20,w//10))
    order_sample.fit(draw,data['sender'],(margin,h*.66,w-margin,h*.84),max(18,w//15))
    order_sample.fit(draw,data['title']+'\n주문번호 '+row['id'],(margin,h*.88,w-margin,h*.97),max(10,w//34))
    png=io.BytesIO();image.save(png,format='PNG',dpi=(layout['dpi'],layout['dpi']))
    pdf=io.BytesIO();image.save(pdf,format='PDF',resolution=layout['dpi'],title='Bellavita '+row['id'])
    return {'png':png.getvalue(),'pdf':pdf.getvalue()}


def artifact_path(root, order_id, version, extension):
    # Callers supply DB identifiers or route regex matches, never arbitrary filenames.
    import re
    if not re.fullmatch(r'[a-f0-9]{32}',order_id) or not str(version).isdigit() or extension not in ('png','pdf'):
        raise ValueError('invalid_artifact_path')
    return Path(root)/'shop-production'/order_id/(str(version)+'.'+extension)


def write(path, content):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
        output.write(content);output.flush();os.fsync(output.fileno());temporary=Path(output.name)
    temporary.chmod(0o600);temporary.replace(path)


def generate(c, fields, root):
    row=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(fields.get('order_id',''),)).fetchone()
    if not row or not partial_reviews.allowed(c,row):raise ValueError('결제 또는 부분취소 잔액 대조를 먼저 확인해 주세요.')
    layout=layout_for(fields);digest=source_hash(row,layout)
    existing=c.execute('SELECT * FROM shop_artifacts WHERE order_id=? AND source_hash=?',(row['id'],digest)).fetchone()
    if existing:
        if latest(c,row['id'])['version'] != existing['version']:
            raise ValueError('예전 규격의 파일이 있습니다. 최신 파일을 검수하거나 새 규격으로 생성해 주세요.')
        return
    previous=latest(c,row['id']);version=previous['version']+1 if previous else 1
    contents=render(row,layout)
    for extension,content in contents.items():write(artifact_path(root,row['id'],version,extension),content)
    c.execute("INSERT INTO shop_artifacts VALUES (?,?,?,?,?,?,'pending',?)",(row['id'],version,digest,json.dumps(layout),hashlib.sha256(contents['png']).hexdigest(),hashlib.sha256(contents['pdf']).hexdigest(),datetime.now(timezone.utc).isoformat()))
    # A new revision invalidates any outstanding previous production lease.
    import order_handoff
    c.execute("UPDATE shop_handoff SET state='held' WHERE order_id=? AND kind IN ('ready','resume') AND state!='acked'",(row['id'],))
    if previous or c.execute("SELECT 1 FROM shop_handoff WHERE order_id=? AND state='acked'",(row['id'],)).fetchone():
        order_handoff.enqueue(c,row,'hold',row['id']+':artifact-pending:'+str(version),'pending')


def review(c, fields, root, actor='admin-link'):
    row=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(fields.get('order_id',''),)).fetchone()
    artifact=latest(c,row['id']) if row else None
    last=latest_review(c,row['id']) if row else None
    decision,reviewer,note=(fields.get(k,'').strip() for k in ('artifact_decision','artifact_reviewer','artifact_note'))
    if (not artifact or not partial_reviews.allowed(c,row) or str(artifact['version'])!=fields.get('artifact_version')
            or artifact['source_hash']!=fields.get('artifact_hash') or artifact['source_hash']!=source_hash(row,json.loads(artifact['layout']))
            or fields.get('artifact_compared')!='yes' or decision not in ('approved','rejected')
            or not 1<=len(reviewer)<=80 or not 1<=len(note)<=600):
        raise ValueError('최신 파일의 문구·보내는 분·중량·규격을 검수하고 확인자와 근거를 입력해 주세요.')
    if artifact['state']==decision:return
    if fields.get('artifact_review_revision')!=str(last['id'] if last else 0):raise ValueError('검수 결과가 변경됐어요. 최신 화면을 확인해 주세요.')
    for ext in ('png','pdf'):
        try:actual=hashlib.sha256(artifact_path(root,row['id'],artifact['version'],ext).read_bytes()).hexdigest()
        except OSError:raise ValueError('제작 파일이 없습니다. 보관된 백업에서 복구해 주세요.') from None
        if actual!=artifact[ext+'_hash']:
            raise ValueError('파일 무결성이 달라졌습니다. 제작을 보류하고 백업에서 복구해 주세요.')
    if json.loads(row['details']).get('wedding_preview') and not c.execute('SELECT 1 FROM invitation_photo_reviews WHERE order_id=?',(row['id'],)).fetchone():raise ValueError('신랑신부 사진 검토를 먼저 완료해 주세요.')
    c.execute('INSERT INTO shop_artifact_reviews(order_id,version,decision,reviewer,actor,note,checked) VALUES (?,?,?,?,?,?,?)',
              (row['id'],artifact['version'],decision,reviewer,actor,note,datetime.now(timezone.utc).isoformat()))
    check_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute('UPDATE shop_artifacts SET state=? WHERE order_id=? AND version=?',(decision,row['id'],artifact['version']))
    import order_handoff
    if decision=='approved':
        kind='ready' if row['state']=='paid' else 'resume'
        order_handoff.enqueue(c,row,kind,row['id']+':artifact-approved:'+str(check_id),'pending')
    else:
        c.execute("UPDATE shop_handoff SET state='held' WHERE order_id=? AND kind IN ('ready','resume') AND state!='acked'",(row['id'],))
        order_handoff.enqueue(c,row,'hold',row['id']+':artifact-rejected:'+str(check_id),'pending')


def manifest(c,row):
    artifact=approved(c,row)
    if not artifact:return None
    check=latest_review(c,row['id'])
    return {'version':artifact['version'],'source_hash':artifact['source_hash'],'approval_id':check['id'],
            'layout':json.loads(artifact['layout']),**{ext:{'sha256':artifact[ext+'_hash'],
            'path':f'/shop/handoff/file/{row["id"]}/{artifact["version"]}.{ext}'} for ext in ('png','pdf')}}


def panel(c,row,path):
    import order_ops
    result='<h3>제작 파일·검수</h3><p>입력 문구·보내는 분·중량의 PNG와 PDF를 생성합니다. 기본 300×900mm·150dpi는 검증 규격입니다. 실제 출력 전 사용할 규격을 검수하세요.</p>'
    hidden=f'<input type="hidden" name="order_id" value="{row["id"]}">'
    if partial_reviews.allowed(c,row):
        result+=f'<form method="post" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="generate_artifact">'
        for key,label,value in (('width_mm','폭(mm)',300),('height_mm','높이(mm)',900),('dpi','해상도(dpi)',150)):
            result+=f'<label>{label}<input type="number" name="{key}" value="{value}" required></label>'
        result+='<button>제작 파일 생성</button></form>'
    artifact=latest(c,row['id'])
    if not artifact:return result+'<p>제작 파일 없음 · 신규 주문은 파일 검수 후 공장에 전달됩니다.</p>'
    result+='<p>버전 '+str(artifact['version'])+' · '+{'pending':'검수 대기','approved':'검수 승인','rejected':'반려'}[artifact['state']]+'</p>'
    for ext in ('png','pdf'):
        signed=order_ops.signed_path('production-'+str(artifact['version'])+'-'+ext,row['id'],os.environ['TAESAN_WEBHOOK_SECRET'])
        result+=f'<p><a href="{signed}">버전 {artifact["version"]} {ext.upper()} 확인</a></p>'
    last=latest_review(c,row['id'])
    result+=(f'<form method="post" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="review_artifact">'
        +f'<input type="hidden" name="artifact_version" value="{artifact["version"]}"><input type="hidden" name="artifact_hash" value="{artifact["source_hash"]}">'
        +f'<input type="hidden" name="artifact_review_revision" value="{last["id"] if last else 0}">'
        +'<label>검수자 (직접 입력)<input name="artifact_reviewer" maxlength="80" required></label><label>검수 근거<textarea name="artifact_note" maxlength="600" required></textarea></label>'
        +'<label>결정<select name="artifact_decision"><option value="approved">제작 승인</option><option value="rejected">반려</option></select></label>'
        +'<label><input type="checkbox" name="artifact_compared" value="yes" required>두 파일의 문구·보내는 분·중량·규격을 확인했습니다</label><button>제작 검수 저장</button></form>')
    for check in c.execute('SELECT * FROM shop_artifact_reviews WHERE order_id=? ORDER BY id',(row['id'],)):
        result+='<p>'+escape(f'v{check["version"]} · {check["reviewer"]} ({check["actor"]}) · {check["decision"]} · {check["checked"][:16]} · {check["note"]}')+'</p>'
    return result
