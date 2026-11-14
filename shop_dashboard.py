"""Shop-only operational queues, linking to the same immutable order number."""
from html import escape
import json
import partial_reviews
import production_files

LABELS={'payment':'결제 확인 필요','production':'제작 보류·검수 대기','delivery':'배송 대기·진행','alerts':'알림 실패·결과 대조','receipts':'영수증 확인 필요'}


def categories(c,row):
    result=[]
    if row['state'] in ('requesting','waiting','failed'):result.append('payment')
    allowed=partial_reviews.allowed(c,row)
    if (not allowed and row['state'] in ('partial_cancel','cancelled')) or (allowed and not production_files.allowed(c,row)):
        result.append('production')
    fulfillment=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(row['id'],)).fetchone()
    if allowed and fulfillment and fulfillment['stage']!='delivered':result.append('delivery')
    if c.execute("SELECT 1 FROM shop_messages WHERE order_id=? AND state IN ('failed','uncertain')",(row['id'],)).fetchone():result.append('alerts')
    if c.execute("SELECT 1 FROM shop_cash_receipts WHERE order_id=? AND state IN ('failed','uncertain','held')",(row['id'],)).fetchone():result.append('receipts')
    return result


def panel(c):
    rows=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job ORDER BY o.created DESC LIMIT 200').fetchall()
    grouped={key:[] for key in LABELS}
    for row in rows:
        for key in categories(c,row):grouped[key].append(row['id'])
    result='<section id="shop-dashboard"><h2>운영 현황판</h2><p>최근 쇼핑몰 주문 200건 기준 · 주문은 여러 확인 항목에 함께 표시될 수 있습니다.</p><div class="shop-queue-filters">'
    result+='<button type="button" data-shop-filter="all">전체 '+str(len(rows))+'건</button>'
    for key,label in LABELS.items():result+=f'<button type="button" data-shop-filter="{key}">{label} {len(grouped[key])}건</button>'
    result+='</div><label>주문번호·문구·보내는 분 검색<input id="shop-order-search" type="search" placeholder="검색어 입력"></label>'
    backup=c.execute('SELECT * FROM shop_backup_runs ORDER BY id DESC LIMIT 1').fetchone()
    if backup:
        result+='<p>최근 자동 백업: '+escape({'verified':'복구 검증 통과','failed':'확인 필요','running':'진행 중'}[backup['state']])+'</p>'
        if backup['state']=='verified':result+='<p>SQLite 무결성·파일 체크섬·복구 SQL 대조 완료</p>'
    else:result+='<p>자동 백업 실행 이력 없음</p>'
    return result+'</section>'


SCRIPT='''<script>
(()=>{let filter='all';const search=document.getElementById('shop-order-search');
function apply(){for(const row of document.querySelectorAll('[data-shop-order]'))row.hidden=(filter!=='all'&&!row.dataset.queues.split(',').includes(filter))||!row.textContent.toLowerCase().includes(search.value.toLowerCase());}
for(const button of document.querySelectorAll('[data-shop-filter]'))button.addEventListener('click',()=>{filter=button.dataset.shopFilter;for(const other of document.querySelectorAll('[data-shop-filter]'))other.setAttribute('aria-pressed',String(other===button));apply();});search?.addEventListener('input',apply);
function openOrder(){const target=document.getElementById(location.hash.slice(1));if(target?.hasAttribute('data-shop-order')){const details=target.querySelector('details');if(details)details.open=true;}}window.addEventListener('hashchange',openOrder);openOrder();
})();</script>'''
