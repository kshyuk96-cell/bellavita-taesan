"""Shared feature pins, immutable shift snapshots and launch checks."""
import json
import os
from pathlib import Path
import re
import secrets
from html import escape
import shop_admin_workflow as workflow
import shop_admin_team as team
import shop_admin_feature_search as features
import shop_readiness

CHECKS={'merchant':'가맹점 승인·취소 검증','messages':'직원·고객 실제 단말 수신','receipt':'과세·발급 주체·실제 영수증 대조','restore':'백업·복구·재배포 보존','handover':'운영자 인수인계 리허설'}
FEATURE_IDS=features.FEATURE_IDS


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS shop_feature_favorites(feature_id TEXT PRIMARY KEY,version INTEGER NOT NULL,favorite INTEGER NOT NULL,updated TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS shop_feature_favorite_events(feature_id TEXT NOT NULL,version INTEGER NOT NULL,favorite INTEGER NOT NULL,changed TEXT NOT NULL,PRIMARY KEY(feature_id,version))')
    c.execute('CREATE TABLE IF NOT EXISTS shop_shift_snapshots(id TEXT PRIMARY KEY,title TEXT NOT NULL,author TEXT NOT NULL,owner TEXT NOT NULL,until_date TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS shop_launch_checks(version INTEGER PRIMARY KEY,author TEXT NOT NULL,note TEXT NOT NULL,checks TEXT NOT NULL,created TEXT NOT NULL)')


def toggle_feature(c,f):
    key=f.get('feature_id','');target=f.get('feature_target');old=c.execute('SELECT * FROM shop_feature_favorites WHERE feature_id=?',(key,)).fetchone();version=old['version'] if old else 0;current=old['favorite'] if old else 0
    if key not in FEATURE_IDS or target not in ('0','1') or f.get('feature_version')!=str(version) or int(target)==current:raise ValueError('기능 즐겨찾기가 변경됐습니다. 최신 화면을 확인하세요.')
    if target=='1' and c.execute('SELECT count(*) FROM shop_feature_favorites WHERE favorite=1').fetchone()[0]>=8:raise ValueError('관리자 기능 즐겨찾기는 최대 8개입니다.')
    changed=workflow.now();version+=1
    c.execute('INSERT INTO shop_feature_favorites VALUES (?,?,?,?) ON CONFLICT(feature_id) DO UPDATE SET version=excluded.version,favorite=excluded.favorite,updated=excluded.updated',(key,version,int(target),changed))
    c.execute('INSERT INTO shop_feature_favorite_events VALUES (?,?,?,?)',(key,version,int(target),changed))


def favorite_nav(c):
    selected={r[0] for r in c.execute('SELECT feature_id FROM shop_feature_favorites WHERE favorite=1')}
    buttons=''.join('<button type="button" data-admin-feature="'+key+'">'+escape(item[0])+' 위치로 이동</button>' for key,item in zip(FEATURE_IDS,features.FEATURES) if key in selected)
    return '<div class="feature-pinned"><p>공유 기능 즐겨찾기 · 위치 이동</p>'+buttons+'</div>' if buttons else ''


def favorite_panel(c,path):
    body='<section class="feature-favorite-settings"><h2>관리자 기능 즐겨찾기</h2><p>관리자 공유 · 최대 8개 · 저장/발송 작업을 실행하지 않고 해당 위치로 이동합니다.</p><details><summary>기능 즐겨찾기 설정</summary>'
    for key,item in zip(FEATURE_IDS,features.FEATURES):
        old=c.execute('SELECT * FROM shop_feature_favorites WHERE feature_id=?',(key,)).fetchone();favorite=bool(old and old['favorite'])
        hidden=workflow.hidden('feature_id',key)+workflow.hidden('feature_version',old['version'] if old else 0)+workflow.hidden('feature_target','0' if favorite else '1')
        body+=workflow.form(path,'toggle_feature_favorite',hidden,('기능 즐겨찾기 해제 · ' if favorite else '기능 즐겨찾기 추가 · ')+escape(item[0]))
    return body+'</details></section>'


def save_snapshot(c,f):
    identity=f.get('snapshot_id','');title=f.get('snapshot_title','').strip();author=f.get('snapshot_author','').strip();owner=f.get('snapshot_owner','').strip();until=f.get('snapshot_until','').strip()
    if not re.fullmatch('[a-f0-9]{32}',identity) or not 1<=len(title)<=80 or not 1<=len(author)<=60:raise ValueError('보관 제목·작성자·요청번호를 확인하세요.')
    old=c.execute('SELECT * FROM shop_shift_snapshots WHERE id=?',(identity,)).fetchone()
    if old:
        if (old['title'],old['author'],old['owner'],old['until_date'])!=(title,author,owner,until):raise ValueError('같은 보관 요청의 내용이 달라졌습니다.')
        return
    if c.execute('SELECT count(*) FROM shop_shift_snapshots').fetchone()[0]>=200:raise ValueError('교대 요약은 최대 200개까지 보관합니다. 기존 이력을 확인하세요.')
    report=team.shift_report(c,{'handover_owner':owner,'handover_until':until})
    c.execute('INSERT INTO shop_shift_snapshots VALUES (?,?,?,?,?,?,?)',(identity,title,author,owner,until,report['text'],workflow.now()))


def snapshot_form(path,owner,until):
    hidden=workflow.hidden('snapshot_id',secrets.token_hex(16))+workflow.hidden('snapshot_owner',owner)+workflow.hidden('snapshot_until',until)
    return '<details class="shift-snapshot-form"><summary>현재 조건으로 교대 요약 보관</summary><p>저장 시점의 최신 기록을 보관합니다. 업무가 바뀌어도 보관 내용은 바뀌지 않습니다.</p>'+workflow.form(path,'save_shift_snapshot',hidden+'<label>보관 제목<input name="snapshot_title" maxlength="80" required></label><label>작성자<input name="snapshot_author" maxlength="60" required></label>','교대 요약 보관')+'</details>'


def snapshot_panel(c,path):
    count=c.execute('SELECT count(*) FROM shop_shift_snapshots').fetchone()[0]
    body='<section class="shift-history"><h2>교대 요약 보관 이력</h2><p>직원 내부용 · 전체 '+str(count)+'개 / 최대 200개 · 최근 20개 표시 · 시각 UTC · 작성자 이름은 로그인 인증과 구분합니다.</p>'
    for row in c.execute('SELECT * FROM shop_shift_snapshots ORDER BY created DESC,id DESC LIMIT 20'):
        body+='<details><summary>'+escape(row['title']+' · '+row['author']+' · '+row['created'][:16])+'</summary><p>담당자 조건: '+escape(row['owner'] or '전체')+' · 기한까지: '+escape(row['until_date'] or '전체')+'</p><p><a class="snapshot-download" href="'+escape(path+'/shift-history/'+row['id']+'.txt')+'">보관 요약 다운로드</a></p><pre>'+escape(row['content'])+'</pre></details>'
    return body+'</section>'


def launch_report(c,root):
    r=shop_readiness.report(c);backup=workflow.backup_status(c)
    disk=Path(root).resolve().is_relative_to(Path('/var/data')) and os.path.ismount('/var/data')
    pending=c.execute("SELECT count(*) FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.state='requesting'").fetchone()[0]
    return {'mock_environment':os.getenv('SHOP_VERIFICATION_MODE')=='1','merchant_settings':r['payapp']['merchant_keys_present'] and r['payapp']['enabled'],'messages_settings':r['alimtalk']['enabled'] and not r['alimtalk']['missing_fields'] and r['alimtalk']['route_selected'],'receipt_settings':r['cash_receipt']['enabled'] and r['cash_receipt']['owner_configured'] and not r['cash_receipt']['products_missing_tax_type'],'persistent_disk':disk,'backup_warnings':backup['warnings'],'last_local_restore':backup['last_local'],'automatic_external_configured':r['external_backup']['configured'],'payment_checks_needed':pending,'actual_provider_validation':'not_verified_by_this_screen','external_requests':0}


def save_launch_check(c,f):
    version=c.execute('SELECT COALESCE(MAX(version),0) FROM shop_launch_checks').fetchone()[0];author=f.get('launch_author','').strip();note=f.get('launch_note','').strip()
    if f.get('launch_version')!=str(version) or not 1<=len(author)<=60 or len(note)>1200:raise ValueError('최신 점검 기록·작성자·근거를 확인하세요.')
    checks={key:f.get('launch_'+key)=='yes' for key in CHECKS}
    c.execute('INSERT INTO shop_launch_checks VALUES (?,?,?,?,?)',(version+1,author,note,json.dumps(checks,ensure_ascii=False),workflow.now()))


def launch_panel(c,path,root):
    r=launch_report(c,root)
    rows=[('현재 환경','독립 모의 검증 환경' if r['mock_environment'] else '일반 실행 환경 · 운영 여부 별도 대조'),('페이앱 설정','설정 항목 확인 · 실거래 별도 검증' if r['merchant_settings'] else '실가맹점 키·결제 설정 확인 필요'),('알림톡 설정','설정 항목 확인 · 단말 수신 별도 검증' if r['messages_settings'] else '채널·템플릿·발송 설정 확인 필요'),('현금영수증 설정','설정 항목 확인 · 실제 발급 별도 검증' if r['receipt_settings'] else '발급 주체·과세·발급 설정 확인 필요'),('영구 디스크','/var/data 마운트 아래 데이터 확인' if r['persistent_disk'] else '영구 디스크·데이터 경로 확인 필요'),('최근 로컬 복구 검증',r['last_local_restore']),('자동 외부 백업','서버 인증 설정 있음 · 복구 이력 별도 대조' if r['automatic_external_configured'] else '서버 자동 업로드 인증 미연결'),('결제 확인 필요 주문',str(r['payment_checks_needed'])+'건')]
    body='<section class="launch-check"><h2>운영 전 점검</h2><p>설정과 확인 기록을 모아 봅니다. 이 화면이 실거래·실수신·실제 발급 성공을 판정하거나 운영 병합·결제·발송·발급을 활성화하지 않습니다.</p><dl>'+''.join('<dt>'+escape(label)+'</dt><dd>'+escape(value)+'</dd>' for label,value in rows)+'</dl>'+''.join('<p>'+escape(w)+'</p>' for w in r['backup_warnings'])
    records=c.execute('SELECT * FROM shop_launch_checks ORDER BY version DESC LIMIT 10').fetchall();latest=records[0] if records else None;checks=json.loads(latest['checks']) if latest else {}
    fields=workflow.hidden('launch_version',latest['version'] if latest else 0)
    for key,label in CHECKS.items():fields+='<label><input type="checkbox" name="launch_'+key+'" value="yes"'+(' checked' if checks.get(key) else '')+'>'+label+'</label>'
    fields+='<label>확인 작성자<input name="launch_author" required maxlength="60" value="'+escape(latest['author'] if latest else '')+'"></label><label>근거·남은 일<textarea name="launch_note" maxlength="1200">'+escape(latest['note'] if latest else '')+'</textarea></label>'
    body+='<h3>운영자 확인 기록 · 자동 검증 결과와 별도</h3><p>체크는 작성 시점의 관리자 확인 메모이며 운영 승인이나 활성화가 아닙니다. 설정이 바뀌면 다시 대조하세요.</p>'+workflow.form(path,'save_launch_check',fields,'운영 점검 기록 저장')+'<h4>최근 확인 기록 10회 · UTC</h4>'
    for record in records:
        saved=json.loads(record['checks']);body+='<p>'+escape(record['created'][:16]+' · '+record['author']+' · '+str(sum(saved.values()))+'/5 확인 기록')+'</p><p class="preserve-lines">'+escape(record['note'])+'</p>'
    return body+'</section>'
