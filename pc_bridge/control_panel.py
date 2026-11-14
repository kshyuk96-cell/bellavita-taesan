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
from order_review_checks import save_confirmation,current_confirmation,compare_fields,field_digest

ROOT=Path(__file__).resolve().parent
STATE=Path(os.getenv('LOCALAPPDATA',str(ROOT)))/'TaesanPlanningTest'


class Panel:
    def __init__(self):
        STATE.mkdir(parents=True,exist_ok=True)
        self.config_path=ROOT/'capture.json'
        self.receiver=Receiver(STATE/'receiver.sqlite3')
        self.db=sqlite3.connect(STATE/'panel.sqlite3');setup(self.db)
        self.db.execute('CREATE TABLE IF NOT EXISTS room_baselines (id TEXT PRIMARY KEY,seen TEXT NOT NULL)');self.db.commit()
        self.watch_process=None;self.review_signature=None;self.review_rows=[]
        self.root=tk.Tk();self.root.title('태산이 주문 테스트 · 문구 검토 6');self.root.geometry('920x760')
        self.status=tk.StringVar(value='업무 PC 모드 · 등록한 열린 방의 백그라운드 OCR 감시 · 시트 쓰기 없음')
        ttk.Label(self.root,textvariable=self.status,wraplength=880).pack(pady=8)
        self.manual_mode=tk.BooleanVar(value=False)
        ttk.Checkbutton(self.root,text='직접 조작하는 테스트 허용 · 카톡 창과 클립보드를 사용합니다',variable=self.manual_mode).pack(anchor='w',padx=10)
        bar=ttk.Frame(self.root);bar.pack(fill='x',padx=10)
        for label,action in [('자동 감시 시작',self.start_watch),('감시 중지',self.stop_watch),('고객방 설정',self.settings),('방 목록 새로고침',self.refresh),('문구 대조 확인',self.confirm_words),('결혼·행사 테스트',self.test_cases)]:
            ttk.Button(bar,text=label,command=lambda fn=action:self.guard(fn)).pack(side='left',padx=3)
        ttk.Label(self.root,text='등록한 모든 열린 방을 자동 순회합니다. 방별 선택이나 주문 키 변경은 필요 없습니다.').pack(anchor='w',padx=10,pady=6)
        self.manual_area=ttk.Frame(self.root)
        self.manual_mode.trace_add('write',lambda *args:self.toggle_manual())
        manual_buttons=ttk.Frame(self.manual_area);manual_buttons.pack(fill='x')
        for label,action in [('선택 방 읽기',self.capture),('연결 테스트',self.connect),('검토용 주문서 공유',self.share_review)]:
            ttk.Button(manual_buttons,text=label,command=lambda fn=action:self.guard(fn)).pack(side='left',padx=3)
        self.room=tk.StringVar();self.rooms=ttk.Combobox(self.manual_area,textvariable=self.room,state='readonly');self.rooms.pack(fill='x',pady=5)
        ttk.Label(self.manual_area,text='서버 연결용 전용 키 (수동 연결 테스트에서만 사용)').pack(anchor='w')
        self.key=tk.StringVar();ttk.Entry(self.manual_area,textvariable=self.key,show='*').pack(fill='x')
        ttk.Label(self.root,text='자동 감시 중 발견한 주문 후보는 아래에 표시됩니다. 직접 붙여넣기 분석도 가능합니다.').pack(anchor='w',padx=10,pady=6)
        self.input=tk.Text(self.root,height=10);self.input.pack(fill='both',expand=True,padx=10)
        ttk.Button(self.root,text='주문 분석 미리보기 · 시트 쓰기 없음',command=lambda:self.guard(self.analyze)).pack(pady=5)
        self.output=tk.Text(self.root,height=15,state='disabled');self.output.pack(fill='both',expand=True,padx=10,pady=6)
        self.root.protocol('WM_DELETE_WINDOW',self.close);self.refresh()
        self.root.after(1000,self.watch_status)
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
    def toggle_manual(self):
        if self.manual_mode.get():self.manual_area.pack(fill='x',padx=10,before=self.input)
        else:self.manual_area.pack_forget()
    def start_watch(self):
        if self.watch_process and self.watch_process.poll() is None:
            self.status.set('이미 자동 감시 중입니다.');return
        entries=validate_config(self.config())
        if not entries:raise ValueError('처음 한 번 고객방을 등록하고 저장해 주세요.')
        self.watch_process=subprocess.Popen([sys.executable,str(ROOT/'background_watch.py'),
            '--config',str(self.config_path),'--state',str(STATE),'--interval','5'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.status.set('자동 감시 시작 · 등록한 방을 열어두세요. 최소화하지 않고 다른 창 뒤에 둘 수 있습니다.')
    def stop_watch(self):
        if self.watch_process and self.watch_process.poll() is None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(self.watch_process.pid),'/T','/F'],
                    capture_output=True,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW)
            else:self.watch_process.terminate()
            self.watch_process.wait(timeout=5)
        self.watch_process=None;self.status.set('자동 감시 중지')
    def watch_status(self):
        try:
            if self.watch_process:
                if self.watch_process.poll() is not None:
                    self.status.set('감시 프로세스가 멈췄습니다.');self.watch_process=None
                elif (STATE/'background-status.json').exists():
                    data=json.loads((STATE/'background-status.json').read_text(encoding='utf-8'))
                    labels={'baseline_saved':'기존 화면 제외','unchanged':'변화 없음','checked':'주문 변화 없음',
                            'review_required':'주문 후보 검토 필요','error':'읽기 실패','unreadable':'본문 인식 실패'}
                    status=' / '.join(r['room']+': '+labels.get(r['state'],r['state'])+
                        (' '+r.get('error_type','')+' '+r.get('error_location','') if r['state']=='error' else '') for r in data.get('rooms',[]))
                    self.status.set('자동 감시 · '+(status or '등록 방·설정 확인 필요'))
                    database=STATE/'background-orders.sqlite3'
                    if database.exists():
                        with sqlite3.connect(database,timeout=.2) as c:
                            rows=c.execute('SELECT id,scope,changed_text,result,sightings FROM ocr_reviews ORDER BY rowid DESC LIMIT 20').fetchall()
                        allowed={json.dumps([e['id'],e['window_title'],'bubbles-v3'],ensure_ascii=False) for e in validate_config(self.config())}
                        rows=[r for r in rows if r[1] in allowed]
                        self.review_rows=rows
                        signature=tuple((r[0],r[4]) for r in rows)
                        if signature!=self.review_signature:
                            self.review_signature=signature
                            self.show('\n\n'.join(self.review_text(r) for r in rows) or '새 주문 후보 대기 중')
        except Exception:
            self.status.set('감시 상태 갱신 대기 · 설정 또는 읽기 오류 확인 필요')
        finally:self.root.after(1000,self.watch_status)
    def review_text(self,row):
        data=json.loads(row[3]);relation=data.get('relationship',{})
        labels={'amendment_request':'수정 요청 / 원본 연결 확인 필요','unlinked_change_request':'변경 요청 / 대상 주문 미확인',
                'cancellation_request':'취소 요청 / 적용하지 않음','explicit_new_request':'별도 신규 주문 요청 / 아직 미확정',
                'unconfirmed_order_candidate':'미확정 주문 후보'}
        text='방: '+json.loads(row[1])[1]+' / OCR 검토 필요\n'+data['text']+'\n'+data['warning']
        text+='\n구분: '+labels.get(relation.get('kind'),'문맥 확인 필요')
        if relation.get('target_hint'):text+=' / 읽힌 주문번호: '+relation['target_hint']+' (원장 연결 미검증)'
        if data.get('repeat_observation'):text+='\n'+data['repeat_observation']['message']
        if data.get('possible_ocr_variant_of'):text+='\n공백만 다른 이전 후보가 있습니다. OCR 차이인지 별도 요청인지 확인해야 합니다.'
        with sqlite3.connect(STATE/'background-orders.sqlite3',timeout=.2) as db:
            confirmed=current_confirmation(db,row[0],data.get('fields',{}))
        if confirmed:
            text+='\n[사용자가 원문 대조한 문구 / 이 후보에만 적용]'
            titles={'sender':'보내는 분','recipient':'받는 분','message':'문구'}
            comparisons=compare_fields(data.get('fields',{}),confirmed)
            for key,value in confirmed.items():
                text+='\n'+titles[key]+': '+value+' / '+{'exact':'OCR와 일치','whitespace_mismatch':'OCR와 공백 차이','different':'OCR와 내용 차이'}[comparisons[key]]
        return text+'\n[감지한 OCR 원문 / 오인식 가능]\n'+row[2]
    def confirm_words(self):
        if not self.review_rows:raise ValueError('먼저 주문 후보가 감지되어야 합니다.')
        dialog=tk.Toplevel(self.root);dialog.title('카톡 원문과 문구 대조');dialog.geometry('760x620')
        ttk.Label(dialog,text='카톡 원문을 직접 확인해 정확한 문구를 입력하세요. 고객 기본값이나 시트에는 저장하지 않습니다.',wraplength=720).pack(pady=8)
        selector=ttk.Combobox(dialog,state='readonly',width=90)
        selector['values']=[json.loads(r[1])[1]+' / '+json.loads(r[3]).get('fields',{}).get('event_type','미확인')+' / '+
                            json.loads(r[3]).get('fields',{}).get('sender','미확인')[:30] for r in self.review_rows]
        selector.pack(padx=10);selector.current(0)
        selected_rows=list(self.review_rows);widgets={}
        for key,label in [('sender','보내는 분'),('recipient','받는 분'),('message','인쇄 문구')]:
            ttk.Label(dialog,text=label).pack(anchor='w',padx=10)
            box=tk.Text(dialog,height=3);box.pack(fill='x',padx=10,pady=3);widgets[key]=box
        checked=tk.BooleanVar(value=False)
        ttk.Checkbutton(dialog,text='카톡 원문과 직접 대조했습니다.',variable=checked).pack(pady=8)
        def populate(*args):
            row=selected_rows[selector.current()];fields=json.loads(row[3]).get('fields',{})
            for key,box in widgets.items():box.delete('1.0','end');box.insert('1.0',fields.get(key,''))
            checked.set(False)
        selector.bind('<<ComboboxSelected>>',populate);populate()
        def save():
            row=selected_rows[selector.current()]
            scopes={json.dumps([e['id'],e['window_title'],'bubbles-v3'],ensure_ascii=False) for e in validate_config(self.config())}
            if row[1] not in scopes:raise ValueError('고객방 설정이 변경되어 확인을 보류했습니다.')
            values={k:box.get('1.0','end').rstrip('\n') for k,box in widgets.items() if box.get('1.0','end').strip()}
            with sqlite3.connect(STATE/'background-orders.sqlite3') as db:
                save_confirmation(db,row[0],values,source_checked=checked.get(),
                    expected_digest=field_digest(json.loads(row[3]).get('fields',{})))
            self.review_signature=None;dialog.destroy();self.status.set('이 후보의 원문 대조 기록 저장 · 시트 입력 없음')
        ttk.Button(dialog,text='이 후보의 문구 확인 기록 저장',command=lambda:self.guard(save)).pack(pady=8)
    def test_cases(self):
        cases=json.loads((ROOT/'synthetic-windows-cases.json').read_text(encoding='utf-8'))
        dialog=tk.Toplevel(self.root);dialog.title('합성 주문 테스트 문구');dialog.geometry('720x460')
        ttk.Label(dialog,text='합성 테스트 문구입니다. 복사 버튼을 누른 뒤 테스트방에 직접 붙여넣어 전송하세요. 자동 전송하지 않습니다.',wraplength=680).pack(pady=8)
        selector=ttk.Combobox(dialog,state='readonly',values=['결혼 테스트','행사 테스트']);selector.pack();selector.current(0)
        box=tk.Text(dialog,height=14);box.pack(fill='both',expand=True,padx=10,pady=8)
        def show(*args):box.delete('1.0','end');box.insert('1.0',cases[selector.current()]['text'])
        selector.bind('<<ComboboxSelected>>',show);show()
        def copy():
            self.root.clipboard_clear();self.root.clipboard_append(cases[selector.current()]['text'])
            self.status.set('합성 테스트 문구 복사 완료 · 테스트방에 직접 붙여넣어 전송하세요.')
        ttk.Button(dialog,text='선택한 테스트 문구 복사',command=copy).pack(pady=8)
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
        self.stop_watch()
        self.key.set('');self.receiver.db.close();self.db.close();self.root.destroy()


if __name__=='__main__':Panel().root.mainloop()
