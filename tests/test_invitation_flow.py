import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
import threading
from http.server import ThreadingHTTPServer

from lxml import html
from PIL import Image

import agent
import invitation
from blogpay import BlogPay


class InvitationFlowTest(unittest.TestCase):
    def test_staff_prompt_and_scope(self):
        def payload(owner, message):
            return {'userRequest': {'user': {'id': owner}, 'utterance': message}}

        with tempfile.TemporaryDirectory() as folder, patch.object(agent, 'ROOT', Path(folder)):
            with agent.db() as db:
                db.execute("INSERT INTO staff VALUES ('employee','staff',0)")
                db.execute("INSERT INTO staff VALUES ('other','staff',0)")
            prompt = agent.dispatch(payload('employee', '초대장'))
            self.assertIn('청첩장 링크나 행사 정보를 올려주세요', str(prompt))
            self.assertIn('초대장', str(agent.dispatch(payload('employee', '메뉴'))))
            self.assertNotIn('청첩장 링크나 행사 정보를 올려주세요',
                             str(agent.dispatch(payload('unapproved', '초대장'))))
            with agent.db() as db:
                owners = [row[0] for row in db.execute('SELECT owner FROM invitation_input')]
            self.assertEqual(owners, ['employee'])

    def test_text_and_original_link(self):
        fields = {'name':'푸르지오 아파트 완공식', 'date':'2099-12-12 14:30', 'venue':'푸르지오 중앙광장'}
        source = '완공식은 2099년 12월 12일 오후 2시 30분 푸르지오 중앙광장에서 열려요. https://example.org/event'
        order_urls = {price:f'https://example.org/order/{price}' for price in (93500, 99000)}
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, 'ROOT', Path(tmp)):
            with ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler) as server:
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                try:
                    origin = 'https://example.org'
                    with patch.object(invitation, 'find_links', return_value=order_urls):
                        result = invitation.make(source, Path(tmp), origin, lambda _: fields, direct_blogpay=True)
                    base = f'http://127.0.0.1:{server.server_port}'
                    for item in result['items']:
                        response = urlopen(item['url'].replace(origin, base))
                        self.assertEqual(response.status, 200)
                        page = html.fromstring(response.read())
                        self.assertEqual(page.xpath('//a[@class="shop"]/@href'), [order_urls[item['price']]])
                        self.assertEqual(page.xpath('//a[@class="action original"]/@href'), ['https://example.org/event'])
                        self.assertIn('오후 2시 30분', page.xpath('//span[@class="clock"]/text()'))
                        self.assertIn(f"{item['price']:,}원", item['message'])
                        self.assertEqual(item['message'].count('https://'), 1)
                    image_url = page.xpath('//meta[@property="og:image"]/@content')[0].replace(origin, base)
                    with urlopen(image_url) as response:
                        self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
                        self.assertEqual(Image.open(response).size, (1200, 630))
                finally:
                    server.shutdown()
                    worker.join()

    def test_wedding_meta_and_name(self):
        event = {'name':'김동준 김지원 결혼', 'when':dt.datetime(2026,12,13,13), 'venue':'메이필드호텔 볼룸홀',
                 'address':'서울 강서구 방화대로 94', 'source':'https://example.org/wedding', 'image':'',
                 'families':[('김영남·장미연', '장남', '김동준'), ('김병선·연은숙', '차녀', '김지원')]}
        date, clock, title = invitation.display(event)
        self.assertEqual(title, '김동준 김지원 결혼 축하 쌀빛그림화환 (12월 13일 일 오후 1시 메이필드호텔 볼룸홀)')
        page = html.fromstring(invitation.page(event, date, clock, 'https://example.org/order',
                                               'https://example.org/letter.jpg', 'https://example.org/share'))
        self.assertEqual(page.xpath('//meta[@property="og:title"]/@content'), ['김영남·장미연의 장남 김동준 예식'])
        self.assertEqual(page.xpath('//meta[@property="og:description"]/@content'), ['2026년 12월 13일(일) 오후 1시'])
        text = invitation.kakao_copy(event, date, clock, 93500, 'https://example.org/share')
        self.assertNotIn('3단화환', text)
        self.assertIn('김영남·장미연 올림', text)

    def test_structured_general_event_and_existing_product_name(self):
        data = '''<html><head><script type="application/ld+json">
        {"@type":"Event","name":"푸르지오 완공식","startDate":"2026-12-12T14:30:00+09:00",
         "location":{"name":"중앙광장","address":{"addressLocality":"수원시","streetAddress":"예시로 12"}},
         "image":"https://example.org/event.jpg"}
        </script></head></html>'''
        event = invitation.parse_page('https://example.org/notice', data)
        self.assertEqual((event['name'], event['venue'], event['address']),
                         ('푸르지오 완공식', '중앙광장', '수원시 예시로 12'))
        self.assertEqual(event['when'], dt.datetime(2026, 12, 12, 14, 30))
        self.assertEqual(event['photos'], ['https://example.org/event.jpg'])
        self.assertEqual(BlogPay.normalized_title('정영욱 이노하라 나오 결혼 축하 쌀빛그림화환 (일시 장소)'),
                         BlogPay.normalized_title('정영욱 이노하라나오 결혼 축하 쌀빛그림화환 (일시 장소)'))

    def test_timed_out_copy_reconciles_without_second_copy(self):
        client = BlogPay('example', 'test')
        template = {'goodName':'쌀빛그림화환', 'goodPrice':99000, 'goodNum':1, 'isShow':0,
                    'isRecommend':0, 'optionState':1}
        existing = {'goodNum':2, 'goodName':'테스트', 'goodPrice':93500,
                    'isShow':1, 'isSoldout':0, 'isRecommend':0, 'shortGoodUrl':'https://bp2.kr/one'}
        clone = {**template, 'goodName':'쌀빛그림화환 - 복사본', 'goodNum':3}
        edited = {**clone, 'goodName':'테스트'}
        active = {**edited, 'isShow':1, 'isSoldout':0, 'shortGoodUrl':'https://bp2.kr/two'}
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(
            client, products=unittest.mock.DEFAULT, detail=unittest.mock.DEFAULT,
            request=unittest.mock.DEFAULT, set_sale=unittest.mock.DEFAULT,
            edit_copy=unittest.mock.DEFAULT, image_checksums=unittest.mock.DEFAULT
        ) as mocks:
            mocks['products'].side_effect = [[existing], [existing, clone]]
            mocks['detail'].side_effect = [template, clone, clone, edited, active]
            mocks['request'].side_effect = RuntimeError('timeout after commit')
            self.assertEqual(client.ensure_links('테스트', Path(tmp), 1),
                             {93500:'https://bp2.kr/one', 99000:'https://bp2.kr/two'})
            mocks['request'].assert_called_once_with('goodsList', post=True, goodNum=1, mode='copyGood')
