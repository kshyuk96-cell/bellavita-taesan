"""Server-side BlogPay product lookup and copying for invitation checkout links."""
import argparse
import hashlib
import secrets
import sqlite3
import json
import os
import time
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


PRICES = (93500, 99000)
PAYMENT_FLAGS = (1, 4, 8, 16, 32, 128, 256, 512, 1024, 2048, 8192, 16384, 32768, 262144)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BlogPay:
    def __init__(self, shop, token):
        if not shop or not shop.isascii() or not shop.isalnum() or not token:
            raise ValueError('블로그페이 판매점 ID와 API 토큰을 설정해 주세요.')
        self.origin = f'https://{shop}.shop.blogpay.co.kr'
        self.token = token
        self.last_call = 0
        self.http = build_opener(NoRedirect)

    def request(self, endpoint, *, post=False, multipart=False, **fields):
        params = urlencode({'token': self.token, **fields}, doseq=True)
        url = f'{self.origin}/Api/{endpoint}'
        req = Request(url, params.encode() if post else None) if post else Request(f'{url}?{params}')
        if multipart:
            boundary = secrets.token_hex(24)
            parts = []
            for key, value in {'token': self.token, **fields}.items():
                for item in value if isinstance(value, list) else [value]:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{item}\r\n')
            body = ''.join(parts) + f'--{boundary}--\r\n'
            req = Request(url, body.encode(), headers={'Content-Type':f'multipart/form-data; boundary={boundary}'})
        # Keep catalog reads under the documented limit and retry transient reads only.
        for attempt in range(3 if not post else 1):
            time.sleep(max(0, 4.1 - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            try:
                with self.http.open(req, timeout=12) as response:
                    data = json.load(response)
                break
            except HTTPError as error:
                retryable = error.code in (429, 500, 502, 503, 504)
            except (TimeoutError, URLError, OSError):
                retryable = True
            except ValueError:
                retryable = False
            if post or not retryable or attempt == 2:
                raise RuntimeError('블로그페이 응답을 확인하지 못했습니다.') from None
        if str(data.get('result', {}).get('resCode')) != '1':
            raise RuntimeError(f'블로그페이 {endpoint} 요청이 거절되었습니다.')
        return data

    def products(self, all_pages=False):
        page = 1
        while True:
            data = self.request('goodsList', page=page)
            yield from data.get('goodsList') or []
            if not all_pages or page >= int(data.get('pagenation', {}).get('pageCount', 1)):
                return
            page += 1

    def detail(self, number):
        data = self.request('goodsModify', goodNum=number)['goodList']
        return data[0] if isinstance(data, list) else data

    def order_link(self, product):
        url = product.get('shortGoodUrl') or ''
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in (
            urlsplit(self.origin).hostname, 'bp2.kr', 'blogpay.co.kr'
        ):
            raise RuntimeError('상품 주문 주소를 확인하지 못했습니다.')
        return url

    def existing_links(self, title, products=None):
        return {price: self.order_link(row) for price, row in self.existing_products(title, products).items()}

    def existing_products(self, title, products=None):
        matches = {}
        for product in self.products(all_pages=True) if products is None else products:
            if product['goodName'] != title:
                continue
            price = int(product['goodPrice'])
            if (price in PRICES and str(product.get('isShow')) == '1'
                    and str(product.get('isSoldout')) == '0'
                    and str(product.get('isRecommend')) == '0'):
                if price in matches:
                    raise RuntimeError(f'{price:,}원 판매 상품이 중복 등록되어 있습니다.')
                matches[price] = product
        return matches

    def copy_product(self, template_number):
        """Copy a product and identify its new ID from the product list.

        The copy response has no ID. If another product appears concurrently,
        require reconciliation rather than editing an uncertain product.
        """
        template = self.detail(template_number)
        before = {str(row['goodNum']) for row in self.products()}
        self.request('goodsList', post=True, goodNum=template_number, mode='copyGood')
        new = [row for row in self.products() if str(row['goodNum']) not in before]
        if len(new) != 1 or template['goodName'] not in new[0]['goodName'] or (
            int(template['goodPrice']) != int(new[0]['goodPrice'])
        ):
            raise RuntimeError('복사된 상품을 특정할 수 없습니다. 관리자 상품 목록에서 확인해 주세요.')
        return new[0]


    def set_sale(self, item, visible):
        self.request('goodsList', post=True, mode='updateGood', goodNum=item['goodNum'],
                     isShow=int(visible), isSoldout=0, isRecommend=0, sort=item['sort'],
                     inven=item['inven'], is_show_detail=item['is_show_detail'])

    def edit_copy(self, item, title, price):
        fields = {key:item[key] for key in (
            'goodsCode', 'shipSubNum', 'cateNum', 'cusNum', 'basisPrice', 'displayPrice', 'orign', 'maker',
            'brand', 'isDelivery', 'eDeliBPrice', 'eDeliFPrice', 'goodContent', 'tag', 'isEvent',
            'supplyUserId', 'isSmUse', 'isCpUse', 'goodSmValue', 'cpNum', 'subtitle', 'subtitleColor',
            'goodsft', 'addShip', 'bsFree', 'perLimitCount', 'buyDCinfo', 'is_show_detail', 'gBindNum',
            'IsReserve', 'adminMemo') if item.get(key) is not None}
        fields.update(mode='updateGoodsNew', goodNum=item['goodNum'], goodName=title, goodPrice=price,
                      categorySelect='useSelect' if item.get('cateNum') else 'useEmptyCate',
                      isDisplay=0, isRecommend=0, optionState=item.get('optionState',1),
                      useNormalOption=int(bool(item.get('normalOption'))),
                      taxType=item.get('gTaxType', 'TG01'),
                      useInputOption=int(bool(item.get('inputOption'))),
                      sDate=item.get('SDate') or '', eDate=item.get('EDate') or '')
        fields['enablePayMethod[]'] = [flag for flag in PAYMENT_FLAGS if int(item['enablePayMethod']) & flag]
        for option in item.get('normalOption') or []:
            group = int(option['goodsNormalOptionNum'])
            fields[f'noTitle[{group}]'] = option['noTitle']
            for choice in option['list']:
                entry = int(choice['goodsNormalOptionListNum'])
                fields[f'noValue[{group}][{entry}]'] = choice['noValue']
                fields[f'noPrice[{group}][{entry}]'] = choice['noPrice']
        for option in item.get('inputOption') or []:
            entry = int(option['goodsInputOptionNum'])
            fields[f'ioTitle[{entry}]'] = option['ioTitle']
            fields[f'ioPrice[{entry}]'] = option['ioPrice']
        self.request('goodsModify', post=True, multipart=True, **fields)

    def settings(self, row):
        normal = [(x.get('noTitle'), x.get('noRequire'), [
            tuple(v.get(k) for k in ('noValue','noPrice','noInven','noSourcePrice','optionView'))
            for v in x.get('list',[])]) for x in row.get('normalOption') or []]
        inputs = [tuple(x.get(k) for k in ('ioTitle','ioPrice','ioRequire')) for x in row.get('inputOption') or []]
        keys = ('shipSubNum','enablePayMethod','goodContent','cateNum','isDelivery','eDeliBPrice',
                'eDeliFPrice','is_show_detail','isSmUse','isCpUse','gBindNum','IsReserve','SDate','EDate',
                'gTaxType','isDeliveryPrice','isTodayShipUse','todayShipTime')
        return tuple(row.get(k) for k in keys), normal, inputs

    def image_checksums(self, item):
        base = item.get('goodImgUri', '').rsplit('/', 1)[0]
        hashes = []
        for key in ('goodImg', 'subImg1', 'subImg2', 'subImg3', 'subImg4'):
            name = item.get(key)
            if not name:
                hashes.append(None)
                continue
            if not base.startswith('/img/g/') or '/' in name or chr(92) in name:
                raise ValueError('상품 사진 경로를 확인할 수 없어요.')
            url = urljoin(self.origin, base + '/' + name)
            if urlsplit(url).netloc != urlsplit(self.origin).netloc:
                raise ValueError('상품 사진 주소를 확인할 수 없어요.')
            with self.http.open(Request(url), timeout=8) as response:
                content = response.read(5_000_001)
            if not content or len(content) > 5_000_000:
                raise ValueError('상품 사진 크기를 확인할 수 없어요.')
            hashes.append(hashlib.sha256(content).digest())
        return hashes

    def ensure_links(self, title, root, template_number=None):
        rows = list(self.products(all_pages=True))
        links = self.existing_links(title, rows)
        if set(links) == set(PRICES) or not template_number:
            return links
        template = self.detail(template_number)
        if ('쌀빛그림화환' not in template.get('goodName','') or str(template.get('isRecommend')) != '0'
                or str(template.get('isShow')) != '0'
                or str(template.get('optionState')) != '1' or template.get('addNormalOption')):
            raise ValueError('쌀빛그림화환 원본 상품의 옵션과 진열 설정을 확인해 주세요.')
        template_images = self.image_checksums(template)
        with sqlite3.connect(root / 'invitation_products.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS copies (key TEXT PRIMARY KEY, number TEXT, before TEXT)')
            for price in PRICES:
                if price in links:
                    continue
                key = hashlib.sha256(f'{title}\0{price}'.encode()).hexdigest()
                record = db.execute('SELECT number,before FROM copies WHERE key=?',(key,)).fetchone()
                if any(p.get('goodName') == title and int(p.get('goodPrice',0)) == price
                       and (not record or str(p['goodNum']) != record[0]) for p in rows):
                    raise ValueError(f'{price:,}원 상품이 이미 있어요. 쇼핑몰에서 판매와 메인진열 상태를 확인해 주세요.')
                if record is None:
                    before = {str(x['goodNum']) for x in rows}
                    db.execute('INSERT INTO copies VALUES (?,NULL,?)',(key,json.dumps(sorted(before))))
                    db.commit()
                    try:
                        self.request('goodsList', post=True, goodNum=template_number, mode='copyGood')
                    except RuntimeError:
                        pass  # A timeout can happen after the copy was committed.
                else:
                    before = set(json.loads(record[1]))
                if record and record[0]:
                    number = record[0]
                else:
                    copy_name = template['goodName'] + ' - 복사본'
                    candidates = []
                    for _ in range(2):
                        candidates = [row for row in self.products(all_pages=True)
                                      if str(row['goodNum']) not in before and row.get('goodName') == copy_name
                                      and int(row.get('goodPrice', 0)) == int(template['goodPrice'])]
                        if candidates:
                            break
                    if len(candidates) != 1:
                        raise ValueError('상품 복제 결과를 확인해야 해요. 중복을 막기 위해 다시 복제하지 않았어요.')
                    number = str(candidates[0]['goodNum'])
                    db.execute('UPDATE copies SET number=? WHERE key=?',(number,key))
                    db.commit()
                    rows.append(candidates[0])
                item = self.detail(number)
                if item.get('goodName') != title or int(item.get('goodPrice',0)) != price:
                    self.set_sale(item, False)
                    item = self.detail(number)
                    if self.settings(item) != self.settings(template):
                        raise ValueError('복제 상품의 상세 설명, 옵션 또는 배송 설정이 원본과 달라 판매를 중지했어요.')
                    try:
                        self.edit_copy(item, title, price)
                    except RuntimeError:
                        # A subsequent detail read determines whether the update committed.
                        pass
                item = self.detail(number)
                if (item.get('goodName') != title or int(item.get('goodPrice',0)) != price
                        or self.settings(item) != self.settings(template)
                        or self.image_checksums(item) != template_images):
                    self.set_sale(item, False)
                    raise ValueError('상품명, 가격 또는 옵션을 확인해야 해요. 확인 전에는 주문 링크를 보내지 않아요.')
                try:
                    self.set_sale(item, True)
                except RuntimeError:
                    pass
                active = self.detail(number)
                if any(str(active.get(k)) != v for k,v in
                       {'isShow':'1','isSoldout':'0','isRecommend':'0'}.items()):
                    raise ValueError('판매 상태나 메인진열 설정을 확인해야 해요.')
                if (active.get('goodName') != title or int(active.get('goodPrice', 0)) != price
                        or self.settings(active) != self.settings(template)):
                    self.set_sale(active, False)
                    raise ValueError('공개된 상품의 가격과 설정을 확인해야 해요.')
                order = active if active.get('shortGoodUrl') else next(
                    (x for x in self.products(all_pages=True) if str(x['goodNum']) == number), active)
                links[price] = self.order_link(order)
        return links


def main():
    parser = argparse.ArgumentParser(description='블로그페이 예식 상품 주문 링크 조회')
    action = parser.add_subparsers(dest='command', required=True)
    lookup = action.add_parser('lookup')
    lookup.add_argument('title', help='블로그페이 상품명 전체')
    copy = action.add_parser('copy', help='기존 상품의 복사본 1개를 생성합니다')
    copy.add_argument('template_number', type=int)
    args = parser.parse_args()
    client = BlogPay(os.getenv('BLOGPAY_SHOP_ID', ''), os.getenv('BLOGPAY_API_TOKEN', ''))
    if args.command == 'lookup':
        result = client.existing_links(args.title)
    else:
        product = client.copy_product(args.template_number)
        result = {field: product[field] for field in ('goodNum', 'goodName', 'goodPrice')}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
