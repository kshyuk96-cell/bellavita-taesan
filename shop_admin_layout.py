"""Arrange existing controls by operator task without changing their actions."""
from lxml import html


def render(page,readiness):
    tree=html.fromstring(page);main=tree.xpath('//main')[0]
    groups={key:html.Element('div',id='admin-'+key,attrib={'class':'admin-group'}) for key in ('orders','products','delivery','reports','settings')}
    labels={'orders':'주문·결제','products':'상품·판매 링크','delivery':'배정·배송','reports':'정산·집계','settings':'운영 설정'}
    for key,group in groups.items():
        heading=html.Element('h2',attrib={'class':'admin-group-title'});heading.text=labels[key];group.append(heading)
    for section in list(main.xpath('./section')):
        heading=''.join(section.xpath('./h2[1]//text()')).strip()
        key=('orders' if heading in ('운영 현황판','쇼핑몰 주문·결제 내역') else
            'delivery' if heading in ('배송 일정표','담당자 일괄 배정 · CSV 사전 검토','담당 배정 CSV · 사전 검토') or '배정' in heading or '일정' in heading else
            'reports' if '정산' in heading or '집계' in heading else
            'settings' if heading in ('직원 주문 알림','외부 연동 진단') else 'products')
        groups[key].append(section)
    groups['settings'].insert(1,html.fragment_fromstring(readiness))
    nav=html.Element('nav',attrib={'class':'admin-nav','aria-label':'관리 업무 바로가기'})
    for key,label in labels.items():
        a=html.Element('a',href='#admin-'+key);a.text=label;nav.append(a)
    main.insert(2,nav)
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
    tree.append(style)
    return '<!doctype html>'+html.tostring(tree,encoding='unicode',method='html')
