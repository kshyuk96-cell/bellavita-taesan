"""Server-side BlogPay product lookup and copying for invitation checkout links."""
import argparse
import json
import os
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PRICES = (93500, 99000)
PAYMENT_FLAGS = (1, 4, 8, 16, 128, 256, 512, 1024, 2048, 8192, 16384, 32768, 262144)


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

    def request(self, endpoint, *, post=False, **fields):
        # The developer guide limits API reads to 15 per minute.
        time.sleep(max(0, 4.1 - (time.monotonic() - self.last_call)))
        params = urlencode({'token': self.token, **fields}, doseq=True)
        url = f'{self.origin}/Api/{endpoint}'
        req = Request(url, params.encode() if post else None) if post else Request(f'{url}?{params}')
        self.last_call = time.monotonic()
        try:
            with self.http.open(req, timeout=20) as response:
                data = json.load(response)
        except (OSError, ValueError):
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

    def existing_links(self, title):
        return {price: self.order_link(row) for price, row in self.existing_products(title).items()}

    def existing_products(self, title):
        matches = {}
        for product in self.products(all_pages=True):
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
