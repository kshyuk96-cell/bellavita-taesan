"""Arrange existing controls by operator task without changing their actions."""
from lxml import html
import shop_admin_queries
import shop_product_guidance


def render(page,readiness,*,path='',filters=None,saved=False):
    tree=html.fromstring(page);main=tree.xpath('//main')[0]
    groups={key:html.Element('div',id='admin-'+key,attrib={'class':'admin-group'}) for key in ('orders','products','delivery','reports','settings')}
    labels={'orders':'주문·결제','products':'상품·판매 링크','delivery':'배정·배송','reports':'정산·집계','settings':'운영 설정'}
    for key,group in groups.items():
        heading=html.Element('h2',attrib={'class':'admin-group-title'});heading.text=labels[key];group.append(heading)
    for section in list(main.xpath('./section')):
        heading=''.join(section.xpath('./h2[1]//text()')).strip()
        product_form=bool(section.xpath('.//input[@name="action" and (@value="save_product" or @value="issue_offers")]'))
        key=('products' if product_form else 'orders' if heading in ('운영 현황판','쇼핑몰 주문·결제 내역','저장 검색','행사 빠른 보기','미처리 업무') else
            'delivery' if heading in ('배송 일정표','담당자 일괄 배정 · CSV 사전 검토','담당 배정 CSV · 사전 검토') or '배정' in heading or '일정' in heading else
            'reports' if '정산' in heading or '집계' in heading else
            'settings' if heading in ('직원 주문 알림','외부 연동 진단','백업 누락·실패 안내','직원 교육') else 'products')
        groups[key].append(section)
    groups['settings'].insert(1,html.fragment_fromstring(readiness))
    nav=html.Element('nav',attrib={'class':'admin-nav','aria-label':'관리 업무 바로가기'})
    for key,label in labels.items():
        a=html.Element('a',href='#admin-'+key);a.text=label;nav.append(a)
    if path:
        manual=html.Element('a',href=path+'/manual');manual.text='관리 매뉴얼';nav.append(manual)
        for form in [form for group in groups.values() for form in group.xpath('.//form[translate(@method,"POST","post")="post"]')]:
            query=html.Element('input',type='hidden',name='_return_query',value=shop_admin_queries.query(filters or {}));form.append(query)
            ancestor=next((x for x in form.iterancestors() if x.get('id','').startswith('admin-')),None)
            form.append(html.Element('input',type='hidden',name='_return_anchor',value=ancestor.get('id') if ancestor is not None else 'admin-orders'))
    main.insert(2,nav)
    if saved:
        message=html.Element('p',attrib={'role':'status','class':'admin-saved'});message.text='저장 완료 · 현재 주문 상태와 다음 할 일을 확인해 주세요.';main.insert(3,message)
    status=html.Element('p',id='shop-action-status',attrib={'role':'status'});main.append(status)
    for group in groups.values():main.append(group)
    style=html.Element('style');style.text='''
    main{max-width:1120px!important;padding:24px!important}html{scroll-behavior:smooth;scroll-padding-top:95px}
    .admin-nav{position:sticky;top:0;z-index:10;display:flex;gap:8px;flex-wrap:wrap;padding:14px 0;background:#f5f5ef;border-bottom:1px solid #d8dfd4;margin-bottom:24px}
    .admin-nav a{display:block;text-decoration:none;background:#fff;border:1px solid #d8dfd4;border-radius:7px;padding:9px 15px;font-size:14px;font-weight:600}
    .admin-group{scroll-margin-top:90px;margin-bottom:36px}.admin-group-title{font-size:22px!important;border-bottom:2px solid #315840;padding-bottom:12px}.admin-group section{box-shadow:0 3px 15px #243b2905}
    fieldset{border:1px solid #dce3d8;border-radius:8px;padding:14px;margin:18px 0}textarea{min-height:95px;resize:vertical}button{min-height:42px;cursor:pointer}input,select{min-height:42px}input[type=checkbox]{min-height:initial}
    .shop-queue-filters{display:flex;flex-wrap:wrap;gap:8px}.admin-group li{list-style:none;border:1px solid #e0e5dc;border-radius:8px;padding:12px}.admin-group ul{padding:0}summary{overflow-wrap:anywhere}
    @media(max-width:600px){main{padding:16px 12px!important}.admin-nav{gap:6px;padding:10px 0}.admin-nav a{font-size:12px;padding:9px 10px}.admin-group section{padding:15px}.admin-group-title{font-size:19px!important}}
    '''
    style.text+='\n.admin-search{border:1px solid #dce3d8;padding:16px;border-radius:8px;background:#fafbf8}.search-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.order-tools{display:flex;gap:8px;flex-wrap:wrap;padding:10px 0}.copy-source{font-size:12px!important}.order-identity{display:block;font-size:13px;font-weight:400;color:#526351}.order-next{display:block;font-size:14px}.order-next,.admin-saved{border-left:3px solid #315840;padding:10px 12px;background:#edf3e9}.order-pagination{display:flex;gap:18px;padding:12px 0}@media(max-width:600px){.search-grid{grid-template-columns:1fr}}'
    style.text+='\n.saved-search{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.comparison-wrap{overflow-x:auto;max-width:100%}.comparison-wrap table{border-collapse:collapse;width:100%;table-layout:fixed}.comparison-wrap th,.comparison-wrap td{border:1px solid #dce3d8;padding:8px;vertical-align:top;overflow-wrap:anywhere;white-space:pre-wrap}.order-timeline,.operator-handover{border-top:1px solid #dce3d8;margin-top:18px;padding:12px 0}.product-diff{padding:12px;background:#f3f7ef;margin:12px 0}'
    tree.append(style)
    script=html.Element('script');script.text=SCRIPT+shop_product_guidance.SCRIPT;tree.append(script)
    return '<!doctype html>'+html.tostring(tree,encoding='unicode',method='html')


SCRIPT="""
(()=>{
const status=document.getElementById('shop-action-status');
if(/^#product-[a-f0-9]{32}$/.test(location.hash)){const section=document.getElementById(location.hash.slice(1));const detail=section?.querySelector('details');if(detail)detail.open=true;}
if(document.querySelector('.admin-saved')&&/^#order-[a-f0-9]{32}$/.test(location.hash)&&!document.getElementById(location.hash.slice(1))){const link=document.createElement('a');link.href=location.pathname+'?order='+location.hash.slice(7)+location.hash;link.textContent=' 변경한 주문 바로 열기';document.querySelector('.admin-saved').append(link);}
for(const button of document.querySelectorAll('[data-copy-order],[data-copy-link],[data-copy-summary]'))button.addEventListener('click',async()=>{
 const value=button.dataset.copyOrder||button.dataset.copyLink||button.dataset.copySummary;
 try{if(!navigator.clipboard)throw Error('clipboard');await navigator.clipboard.writeText(value);status.textContent='복사했습니다. 해당 주문 담당자·주문자에게만 전달하세요.';button.textContent='복사 완료';}
 catch(error){const source=button.dataset.copySummary?button.closest('.customer-summary').querySelector('.summary-source'):button.closest('.order-tools').querySelector('.copy-source');if(source){source.value=value;source.focus();source.select();}status.textContent='복사할 내용을 선택했습니다. 직접 복사해 주세요.';}
});
for(const input of document.querySelectorAll('input[name=action][value=save_product]')){
 const form=input.form;if(!form.querySelector('[name=product_id]'))continue;
 const fields=()=>Array.from(form.elements).filter(el=>el.name&&!['hidden','submit','button'].includes(el.type)&&!el.disabled);
 const value=el=>el.type==='checkbox'?(el.checked?'선택':'해제'):el.value;
 const initial=new Map(fields().map(el=>[el.name,value(el)]));
 const preview=document.createElement('button');preview.type='button';preview.textContent='변경 전후 비교';preview.className='preview-product';
 const panel=document.createElement('div');panel.className='product-diff';panel.hidden=true;panel.setAttribute('role','status');form.append(preview,panel);
 preview.addEventListener('click',()=>{panel.replaceChildren();panel.hidden=false;let count=0;
 for(const el of fields()){const before=initial.get(el.name)||'';const after=value(el);if(before===after)continue;count++;const p=document.createElement('p');const label=el.closest('label')?.firstChild?.textContent?.trim()||el.name;p.textContent=label+' · 변경 전: '+(before||'(없음)')+' → 변경 후: '+(after||'(없음)');panel.append(p);}
 const p=document.createElement('p');p.textContent=count?count+'개 변경 항목 · 아직 저장하지 않았습니다. 내용을 확인한 뒤 상품 수정 저장을 누르세요.':'변경 항목 없음';panel.prepend(p);
 });
 form.addEventListener('input',()=>{panel.hidden=true;});
}
for(const form of document.querySelectorAll('form[method=post]'))form.addEventListener('submit',event=>{if(form.dataset.submitting==='1'){event.preventDefault();return;}form.dataset.submitting='1';form.setAttribute('aria-busy','true');setTimeout(()=>{for(const button of form.querySelectorAll('button[type=submit],button:not([type])'))if(!button.disabled){button.dataset.disabledSubmit='1';button.disabled=true;}},0);});
window.addEventListener('pageshow',()=>{for(const form of document.querySelectorAll('form[method=post]')){delete form.dataset.submitting;form.removeAttribute('aria-busy');for(const button of form.querySelectorAll('button[data-disabled-submit]')){button.disabled=false;delete button.dataset.disabledSubmit;}}});
})();
"""
