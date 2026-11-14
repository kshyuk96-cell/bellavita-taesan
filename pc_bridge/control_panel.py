"""One local control panel. Customer analysis stays local; chat sharing is planning-only."""
import json
import os
import sqlite3
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox,ttk

from order_preview import preview,fresh_messages
from room_access import validate_config,RoomBlocked
from planning_receiver import Receiver,WindowsChatCopyPort,request_probe
from native_paste import WindowsPastePort,paste_checked
from share_receipts import setup,reserve

ROOT=Path(__file__).resolve().parent
STATE=Path(os.getenv('LOCALAPPDATA',str(ROOT)))/'TaesanPlanningTest'


class Panel:
    def __init__(self):
        STATE.mkdir(parents=True,exist_ok=True)
        self.config_path=ROOT/'capture.json'
        self.receiver=Receiver(STATE/'receiver.sqlite3')
        self.db=sqlite3.connect(STATE/'panel.sqlite3');setup(self.db)
        self.db.execute('CREATE TABLE IF NOT EXISTS room_baselines (id TEXT PRIMARY KEY,seen TEXT NOT NULL)');self.db.commit()
        self.root=tk.Tk();self.root.title('태산이 주문 테스트');self.root.geometry('920x760')
        self.status=tk.StringVar(value='업무 PC 모드 · 백그라운드 수신 연결 미완료 · 주문 분석은 PC 안에서만 처리합니다.')
        ttk.Label(self.root,textvariable=self.status,wraplength=880).pack(pady=8)
        self.manual_mode=tk.BooleanVar(value=False)
        ttk.Checkbutton(self.root,text='직접 조작하는 테스트 허용 · 카톡 창과 클립보드를 사용합니다',variable=self.manual_mode).pack(anchor='w',padx=10)
        bar=ttk.Frame(self.root);bar.pack(fill='x',padx=10)
        for label,action in [('고객방 설정',self.settings),('방 목록 새로고침',self.refresh),
                             ('선택 방 읽기',self.capture),('연결 테스트',self.connect),
                             ('검토용 주문서 공유',self.share_review)]:
            ttk.Button(bar,text=label,command=lambda fn=action:self.guard(fn)).pack(side='left',padx=3)
        self.room=tk.StringVar();self.rooms=ttk.Combobox(self.root,textvariable=self.room,state='readonly');self.rooms.pack(fill='x',padx=10,pady=8)
        ttk.Label(self.root,text='서버 연결용 전용 키 (연결 테스트에서만 사용)').pack(anchor='w',padx=10)
        self.key=tk.StringVar();ttk.Entry(self.root,textvariable=self.key,show='*').pack(fill='x',padx=10)
        ttk.Label(self.root,text='주문 한 건을 붙여넣거나 새 메시지를 선택해 분석하세요.').pack(anchor='w',padx=10,pady=6)
        self.input=tk.Text(self.root,height=10);self.input.pack(fill='both',expand=True,padx=10)
        ttk.Button(self.root,text='주문 분석 미리보기 · 시트 쓰기 없음',command=lambda:self.guard(self.analyze)).pack(pady=5)
        self.output=tk.Text(self.root,height=15,state='disabled');self.output.pack(fill='both',expand=True,padx=10,pady=6)
        self.root.protocol('WM_DELETE_WINDOW',self.close);self.refresh()
    def guard(self,fn):
        try:fn()
        except Exception as e:
            # Do not log request headers, raw customer transcripts or credentials.
            import traceback
            trace=traceback.extract_tb(e.__traceback__)
            where=(' / '+Path(trace[-1].filename).name+':'+str(trace[-1].lineno)) if trace else ''
            self.status.set('중단: '+(str(e) if isinstance(e,(ValueError,RoomBlocked)) else type(e).__name__+where))
    def config(self):
        if not self.config_path.exists():return {'allowed_rooms':[]}
        result=json.loads(self.config_path.read_text(encoding='utf-8-sig'));validate_config(result);return result
    def refresh(self):
        self.rooms['values']=[e['window_title'] for e in validate_config(self.config())]
        if self.room.get() not in self.rooms['values']:self.room.set('')
    def settings(self):
        subprocess.Popen([sys.executable,str(ROOT/'room_settings.py'),'--config',str(self.config_path)])
    def later(self,instruction,fn):
        self.status.set(instruction+' · 8초 뒤 실행합니다. 다른 조작은 잠시 멈춰주세요.')
        self.root.after(8000,lambda:self.guard(fn))
    def show(self,text):
        self.output.configure(state='normal');self.output.delete('1.0','end');self.output.insert('1.0',text);self.output.configure(state='disabled')
    def analyze(self):self.show(preview(self.input.get('1.0','end'))['text'])
    def capture(self):
        if not self.manual_mode.get():raise ValueError('업무 PC 모드에서는 창·클립보드를 조작하는 수집을 실행하지 않습니다.')
        config=self.config();name=self.room.get()
        entries=[e for e in validate_config(config) if e['window_title']==name]
        if len(entries)!=1:raise ValueError('설정에서 고객방을 등록하고 선택하세요.')
        entry=entries[0]
        def read():
            if entry not in validate_config(self.config()):raise RoomBlocked('방 설정이 변경되었습니다.')
            snapshot=WindowsChatCopyPort(name,self.config()).copy()
            if entry not in validate_config(self.config()):raise RoomBlocked('방 설정이 변경되었습니다.')
            key=json.dumps([entry['id'],name,entry.get('order_key','')],ensure_ascii=False)
            old=self.db.execute('SELECT seen FROM room_baselines WHERE id=?',(key,)).fetchone()
            seen=set(json.loads(old[0])) if old else set()
            messages=fresh_messages(snapshot,seen,config.get('operator_senders',[]))
            with self.db:self.db.execute('INSERT OR REPLACE INTO room_baselines VALUES (?,?)',(key,json.dumps(sorted(seen))))
            if old is None:self.status.set('기존 대화 기준 상태 저장. 기존 주문은 분석하지 않았습니다.');return
            if not messages:self.status.set('새 고객 메시지가 없습니다.');return
            choices=tk.Toplevel(self.root);choices.title('분석할 새 메시지 선택')
            listing=tk.Listbox(choices,width=90,height=12);listing.pack()
            for m in messages:listing.insert('end',m['sender']+': '+m['text'][:100].replace('\n',' '))
            def select():
                selected=listing.curselection()
                if not selected:return
                self.input.delete('1.0','end');self.input.insert('1.0',messages[selected[0]]['text']);choices.destroy();self.guard(self.analyze)
            ttk.Button(choices,text='선택한 메시지 한 건 분석',command=select).pack()
        self.later('선택한 고객방의 대화 말풍선을 클릭하세요',read)
    def connect(self):
        if not self.manual_mode.get():raise ValueError('업무 PC 모드에서는 카톡 조작 테스트를 실행하지 않습니다.')
        token=self.key.get()
        if len(token)<32:raise ValueError('Render의 전용 연결 키를 입력하세요. 키를 채팅에 보내지 마세요.')
        def baseline():
            snapshot=WindowsChatCopyPort().copy()
            if not self.receiver.db.execute('SELECT 1 FROM receiver_state WHERE id=1').fetchone():
                self.receiver.observe(snapshot,'기획',lambda p:None,lambda j:False)
            marker=self.receiver.arm()
            def send_marker():
                self.send_once('test:'+marker,marker)
                self.later('기획 방 대화 말풍선을 클릭하세요',read_new)
            def read_new():
                text=WindowsChatCopyPort().copy();responses=[]
                self.receiver.observe(text,'기획',lambda p:request_probe(p,token),lambda j:responses.append(j) or False)
                if len(responses)!=1:raise ValueError('새 테스트 응답이 없습니다. 자동 재전송하지 않습니다.')
                body=responses[0]['body']
                self.later('기획 방 아래 메시지 입력칸을 클릭하세요',lambda:self.send_once('reply:'+marker,body))
            self.later('기획 방 아래 메시지 입력칸을 클릭하세요',send_marker)
        self.later('기획 방 대화 말풍선을 클릭하세요',baseline)
    def send_once(self,key,body):
        # Reserve before opening/focusing any target: errors never auto-resend.
        if not reserve(self.db,key,'기획',body):raise ValueError('기존 시도가 있어 중복 전송을 차단했습니다.')
        try:confirmed=paste_checked({'destination':'기획','body':body},WindowsPastePort(),send=True)
        except Exception:
            with self.db:self.db.execute("UPDATE receipts SET state='uncertain' WHERE id=?",(key,))
            raise
        with self.db:self.db.execute('UPDATE receipts SET state=? WHERE id=?',('sent' if confirmed else 'uncertain',key))
        self.status.set('전송을 시도했습니다. 실제 말풍선을 확인해 주세요. 자동 재전송은 차단됩니다.')
    def share_review(self):
        if not self.manual_mode.get():raise ValueError('업무 PC 모드에서는 카톡 자동 입력을 실행하지 않습니다.')
        path=ROOT/'planning-test-manifest.json'
        if not path.exists():raise ValueError('검토용 주문서 파일을 먼저 준비해야 합니다.')
        m=json.loads(path.read_text(encoding='utf-8'))
        if m.get('destination')!='기획' or m.get('master_row')!=21380 or not m.get('test_only'):
            raise ValueError('검토용 테스트 대상이 다릅니다.')
        review=m.get('review_form',{})
        if not review.get('native_read_verified') or not review.get('body','').startswith('[태산이 검토용 주문서 / 최종 확정 아님]'):
            raise ValueError('검증된 검토용 주문서가 없습니다.')
        self.show(review['body'])
        self.later('기획 방 아래 메시지 입력칸을 클릭하세요',lambda:self.send_once('review:'+m['order_id'],review['body']))
    def close(self):
        self.key.set('');self.receiver.db.close();self.db.close();self.root.destroy()


if __name__=='__main__':Panel().root.mainloop()
