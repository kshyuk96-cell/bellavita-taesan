"""Current selling prices for the stable own-checkout URL slots."""
import re

WEIGHT = re.compile(r'(?<!\d)(10|20|30)\s*(?:kg|㎏|키로|킬로)', re.I)
UPGRADE_TOTALS = {20: 170000, 30: 220000}
OWN_ORDER = re.compile(r'/i/[a-f0-9]{32}/(?:93500|99000)/order$')


def current_base(amount):
    return 120000 if amount == 99000 else amount


def apply(product, slot):
    old_base = product.get('price')
    if (str(slot), old_base) not in {('93500', 93500), ('99000', 99000), ('99000', 120000)}:
        return product
    base = current_base(old_base)
    groups = []
    for group in product['groups']:
        if group['label'] != '상품 선택':
            groups.append(group)
            continue
        choices = {}
        for code, choice in group['choices'].items():
            weight = WEIGHT.search(choice['label'])
            if weight and int(weight[1]) in UPGRADE_TOTALS:
                choice = {**choice, 'amount': UPGRADE_TOTALS[int(weight[1])] - base}
            choices[code] = choice
        groups.append({**group, 'choices': choices})
    return {**product, 'price': base, 'groups': groups}


def public_copy(text, slot, order_url):
    if str(slot) == '99000' and OWN_ORDER.search(order_url or ''):
        return re.sub(r'(쌀빛그림화환 (?:주문|신청)하기|쌀빛그림화환 안내|화환 안내(?:🌾)?) · 99,000원',
                      r'\1 · 120,000원', text)
    return text
