"""Authenticated staff forwarding with separately confirmed source-room bindings."""
import re
import order_intake
from customer_memory import CustomerMemory, SenderDecision

FORWARD = re.compile(r'^고객 주문 ([A-Za-z0-9_-]{1,100}) ([A-Za-z0-9_-]{1,100})\n([\s\S]+)$')

def prepare(message, path):
    match = FORWARD.fullmatch(message)
    if not match:
        return message, None
    room, sender_id, text = match.groups()
    memory = CustomerMemory(path)
    try:
        allowed = memory.db.execute('SELECT 1 FROM customer_order_rooms WHERE room=?', (room,)).fetchone()
        if not allowed:
            raise ValueError('주문 수집이 허용된 방인지 먼저 확인해 주세요.')
        fields, _ = order_intake.parse_fields(order_intake.REQUEST.sub('', text))
        explicit = fields.get('sender', '')
        # Never replace an unlabeled current signature or new instructions with history.
        if explicit:
            decision = memory.resolve(room, sender_id, explicit_sender=explicit)
        else:
            residual = order_intake.LINK.sub('', order_intake.REQUEST.sub('', text)).strip()
            if residual:
                raise ValueError('이번 주문의 보내는 분 문구를 확인해 주세요. 기본값으로 덮어쓰지 않았어요.')
            decision = memory.resolve(room, sender_id, current_text=text)
        if decision.status != 'auto':
            raise ValueError('이 방의 발신자와 고객 연결을 확인해 주세요.')
        if not explicit:
            text += '\n보내는 분: ' + decision.sender
        return '주문 접수\n' + order_intake.REQUEST.sub('', text), decision
    finally:
        memory.db.close()

def allow_room(memory, room, confirmed_by):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', room) or not confirmed_by.strip():
        raise ValueError('Confirmed room ID required')
    memory.db.execute('INSERT OR REPLACE INTO customer_order_rooms VALUES (?,?)', (room,confirmed_by))
    memory.db.commit()

if __name__ == '__main__':
    import argparse,json
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Import locally confirmed customer bindings; never learn identities from display names.')
    parser.add_argument('--db',required=True)
    parser.add_argument('--confirmed-config',required=True)
    args=parser.parse_args()
    config=json.loads(Path(args.confirmed_config).read_text())
    memory=CustomerMemory(args.db)
    try:
        for profile in config['profiles']:
            memory.confirm_profile(profile['id'],profile['name'],profile['sender'],profile['confirmed_by'])
        for room in config['rooms']:
            allow_room(memory,room['id'],room['confirmed_by'])
        for binding in config['bindings']:
            memory.bind(binding['room'],binding['sender_id'],binding['customer_id'],binding['confirmed_by'])
        print('Confirmed configuration imported. No orders sent or written.')
    finally:memory.db.close()
