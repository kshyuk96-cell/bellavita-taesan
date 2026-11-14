"""Automatic intake queue independent of the Windows capture strategy.

Reader must capture only the explicit room entry supplied to it. No sends,
Sheet writes, identity learning or invitation requests occur here.
"""
import hashlib
import json
import re
import sqlite3
from room_access import validate_config,RoomBlocked
from order_preview import fresh_messages,preview


class AutomaticIntake:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS automatic_baselines (scope TEXT PRIMARY KEY,seen TEXT NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS automatic_previews (
            id TEXT PRIMARY KEY,scope TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,result TEXT NOT NULL)''')
        self.db.commit()
    def poll(self,config_loader,reader):
        events=[]
        for entry in validate_config(config_loader()):
            if entry not in validate_config(config_loader()):continue
            snapshot=reader(entry)
            if entry not in validate_config(config_loader()):raise RoomBlocked('방 설정이 변경되어 수집 결과를 버렸습니다.')
            scope=json.dumps([entry['id'],entry['window_title'],entry.get('order_key','')],ensure_ascii=False)
            old=self.db.execute('SELECT seen FROM automatic_baselines WHERE scope=?',(scope,)).fetchone()
            seen=set(json.loads(old[0])) if old else set()
            messages=fresh_messages(snapshot,seen,config_loader().get('operator_senders',[]))
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO automatic_baselines VALUES (?,?)',(scope,json.dumps(sorted(seen))))
                if old is None:
                    events.append({'room':entry['window_title'],'state':'baseline_saved','new_orders':0})
                    continue
                added=0
                for message in messages:
                    body=message['text']
                    if not re.search(r'주문|화환|근조|청첩장|신랑|신부|보내는\s*분|https?://',body):continue
                    key=hashlib.sha256((scope+message['id']).encode()).hexdigest()
                    try:result=preview(body);status='needs_review' if result['missing'] or result['warnings'] else 'preview_ready'
                    except ValueError as e:result={'text':str(e),'sheet_written':False};status='needs_review'
                    added+=self.db.execute('INSERT OR IGNORE INTO automatic_previews VALUES (?,?,?,?,?)',
                        (key,scope,status,body,json.dumps(result,ensure_ascii=False))).rowcount
                events.append({'room':entry['window_title'],'state':'checked','new_orders':added})
        return events
    def queued(self):
        return [{'id':r[0],'room':json.loads(r[1])[1],'status':r[2],'text':r[3],'result':json.loads(r[4])}
                for r in self.db.execute('SELECT * FROM automatic_previews ORDER BY rowid')]
