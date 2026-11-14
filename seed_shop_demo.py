"""Seed five sample products into an explicitly isolated demo directory only."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

import checkout
import invitation
import shop

CATALOG = Path(__file__).with_name('sample_catalog.json')
MARKER = 'bellavita-shop-sample-v1'


def seed(root, origin='https://localhost'):
    root = Path(root).resolve()
    if not invitation.https_url(origin):
        raise ValueError('샘플 주문서의 HTTPS 주소를 확인해 주세요.')
    marker = root / 'demo-catalog.json'
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError('기존 데이터 폴더에는 샘플을 넣지 않습니다. 비어 있는 전용 경로를 사용하세요.')
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if marker.exists():
        state = json.loads(marker.read_text())
        if state.get('kind') != MARKER or state.get('origin') != origin:
            raise ValueError('다른 샘플 환경의 데이터입니다.')
    else:
        state = {'kind':MARKER, 'origin':origin, 'when':(datetime.now(checkout.SEOUL)+timedelta(days=30)).strftime('%Y-%m-%dT12:00')}
        marker.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    catalog = json.loads(CATALOG.read_text())
    result=[]
    with closing(sqlite3.connect(root/'state.sqlite3')) as c:
        c.row_factory=sqlite3.Row
        checkout.setup(c);shop.setup(c)
        c.commit()
        with c:
            c.execute('BEGIN IMMEDIATE')
            for item in catalog:
                existing = next((row for row in c.execute('SELECT * FROM shop_products')
                                 if json.loads(row['details'])['title']==item['title']),None)
                if existing is None:
                    shop.save_product(c, {'title':item['title'],'price':str(item['prices'][0]),
                        'images':origin+'/checkout-art/sunflower-easel.webp',
                        'options':'\n'.join(f'쌀 {weight["kg"]}kg | {weight["extra"]}' for weight in item['weights']),
                        'greetings':'\n'.join(item['greetings']), 'cash_tax_type':''})
                    existing=c.execute('SELECT * FROM shop_products ORDER BY rowid DESC LIMIT 1').fetchone()
                shop.issue_offers(c,{'product_id':existing['id'],'revision':str(existing['revision']),
                    'event':item['event'],'kind':item['kind'],'when':state['when'],'venue':item['venue'],
                    'address':item['address'],'prices':','.join(map(str,item['prices']))})
                offers=c.execute('SELECT id,price FROM shop_offers WHERE product_id=? ORDER BY price',(existing['id'],)).fetchall()
                result.append({'key':item['key'],'title':item['title'],'product_id':existing['id'],
                               'offers':[{'price':offer['price'],'url':origin+'/shop/order/'+offer['id']} for offer in offers]})
    (root/'sample-links.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--origin',default='https://localhost')
    args=parser.parse_args()
    try:
        products=seed(args.data,args.origin)
    except (OSError,ValueError,sqlite3.Error) as error:
        parser.exit(1,str(error)+'\n')
    print(json.dumps({'sample_products':len(products),'sample_offers':sum(len(item['offers']) for item in products),
                      'links':str(Path(args.data).resolve()/'sample-links.json'),'actual_payments':False},ensure_ascii=False,indent=2))
