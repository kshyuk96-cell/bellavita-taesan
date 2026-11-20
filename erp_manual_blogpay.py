"""Connect staff-supplied BlogPay products without catalog scans or product creation."""
import os
import re
from urllib.parse import urlsplit,urljoin
from urllib.request import Request,build_opener
from urllib.error import HTTPError
from lxml import html
from blogpay import NoRedirect, BlogPay

PRICES=(93500,99000)
FIELDS={f'blogpay{price}Url':1000 for price in PRICES}

def order_url(value, shop=None):
    """Accept only ordinary public BlogPay product URLs, never arbitrary fetch targets."""
    if not isinstance(value,str) or len(value)>1000:return False
    try:
        p=urlsplit(value)
        if p.scheme!='https' or p.username or p.password or p.fragment or p.query or p.port not in (None,443):return False
        host=(p.hostname or '').lower()
        if host=='bp2.kr':return bool(re.fullmatch(r'/[A-Za-z0-9]+/?',p.path))
        if not re.fullmatch(r'[a-z0-9]+\.shop\.blogpay\.co\.kr',host):return False
        if shop and host!=shop.lower()+'.shop.blogpay.co.kr':return False
        return bool(re.fullmatch(r'/v/g/[A-Za-z0-9]+/?',p.path))
    except ValueError:return False

def validate_pair(values,required=False):
    urls={price:values.get(f'blogpay{price}Url','') for price in PRICES}
    if not any(urls.values()) and not required:return {}
    shop=os.getenv('BLOGPAY_SHOP_ID','') or None
    if not all(order_url(url,shop) for url in urls.values()):raise ValueError('93,500원과 99,000원 블로그페이 상품 링크를 각각 입력하세요. 현재 상점의 HTTPS 상품 링크만 사용할 수 있습니다.')
    if len(set(urls.values()))!=2:raise ValueError('가격별로 서로 다른 블로그페이 상품 링크를 입력하세요.')
    return urls

def product_number(raw):
    tree=html.fromstring(raw)
    ids=set(tree.xpath('//input[@name="goodNum" or @name="good_num" or @name="goodNo" or @name="goodsNo"]/@value'))
    for pattern in [r'\b(?:goodNum|good_num|goodsNo|goodNo)\b[\s"\']*[:=]\s*["\']?(\d{1,20})(?=["\'\s,;})])',r'/goods/(\d{1,20})(?:[/?"\'])']:
        ids.update(re.findall(pattern,raw))
    ids={value for value in ids if re.fullmatch(r'\d{1,20}',value)}
    if len(ids)!=1:raise ValueError('블로그페이 주문서의 상품번호를 하나로 확인하지 못했습니다. 상품 링크와 공개 판매 상태를 확인하세요.')
    return ids.pop()

def read_public(url,shop):
    opener=build_opener(NoRedirect)
    for _ in range(5):
        if not order_url(url,shop):raise ValueError('현재 상점의 블로그페이 상품 주소인지 확인하세요.')
        try:
            with opener.open(Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html'}),timeout=12) as reply:
                raw=reply.read(4_000_001)
            if not raw or len(raw)>4_000_000:raise ValueError('상품 주문서를 확인하지 못했습니다.')
            return raw.decode('utf-8')
        except HTTPError as error:
            location=error.headers.get('Location','')
            if error.code not in (301,302,303,307,308) or not location:raise ValueError('블로그페이 상품 주문서를 열지 못했습니다. 공개 판매 상태 또는 접속 제한을 확인하세요.') from None
            url=urljoin(url,location)
        except OSError:raise ValueError('블로그페이 상품 연결에 실패했습니다. 상품 생성은 요청하지 않았습니다.') from None
    raise ValueError('블로그페이 상품의 최종 연결 주소를 확인하세요.')

def products(values,client=None,reader=read_public):
    urls=validate_pair(values,required=True)
    shop=os.getenv('BLOGPAY_SHOP_ID','')
    if client is None:client=BlogPay(shop,os.getenv('BLOGPAY_API_TOKEN',''))
    items=[]
    for price,url in urls.items():
        number=product_number(reader(url,shop))
        row=client.detail(number)
        if str(row.get('goodNum'))!=number or int(row.get('goodPrice') or 0)!=price:raise ValueError(f'{price:,}원 상품의 실제 상품번호·가격이 일치하지 않습니다.')
        if str(row.get('isShow'))!='1' or str(row.get('isSoldout'))!='0':raise ValueError(f'{price:,}원 상품이 판매 중인지 확인하세요.')
        items.append({'price':price,'productId':number,'orderUrl':url,'verified':True,'source':'manual-blogpay'})
    if len({item['productId'] for item in items})!=2:raise ValueError('두 가격이 같은 상품으로 연결되어 있습니다.')
    return items
