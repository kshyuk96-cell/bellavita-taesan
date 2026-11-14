"""Local Windows room allowlist editor. Does not inspect any chat messages."""
import argparse,json,os,uuid
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from room_access import validate_config

DEFAULT={'mode':'floral','allowed_rooms':[], 'operator_senders':['어머니','희성플라워 벨라비타'], 'control_type':'Text','message_regex':r'\[(?P<sender>[^\]]+)\] \[(?P<time>\d{4}-\d{2}-\d{2} [^\]]+)\] (?P<text>[\s\S]+)','poll_seconds':3,'sync':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',default='capture.json');args=p.parse_args();path=Path(args.config)
    config=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else dict(DEFAULT)
    # Explicit registration required even for legacy configs.
    config.setdefault('allowed_rooms',[])
    from routing_defaults import DEFAULT_ROUTING
    config.setdefault('routing',dict(DEFAULT_ROUTING))
    root=tk.Tk();root.title('주문을 받을 카카오톡 방 설정');root.geometry('760x500')
    tk.Label(root,text='등록하고 체크한 방만 수집합니다. 카카오톡 별도 채팅창의 제목을 정확히 입력하세요.',wraplength=720).pack(pady=10)
    tk.Label(root,text='동명이인/같은 제목의 방은 구분할 수 없습니다. 고유한 이름으로 바꾼 뒤 등록하세요.',fg='#a02020').pack()
    listing=tk.Listbox(root,width=100,height=12);listing.pack(padx=12,pady=8,fill='both',expand=True)
    def refresh():
        listing.delete(0,tk.END)
        for item in config['allowed_rooms']:listing.insert(tk.END,f"{'[수집 ON]' if item['enabled'] else '[수집 OFF]'} {item['window_title']}   / 주문 키: {item.get('order_key','')}")
    form=tk.Frame(root);form.pack(fill='x',padx=12)
    title=tk.StringVar();key=tk.StringVar()
    tk.Label(form,text='방 제목').grid(row=0,column=0);tk.Entry(form,textvariable=title,width=55).grid(row=0,column=1)
    tk.Label(form,text='주문/예식 키').grid(row=1,column=0);tk.Entry(form,textvariable=key,width=55).grid(row=1,column=1)
    tk.Label(root,text='새 예식 주문은 키를 변경하세요. 방 자동 구분·수집 호환성 진단은 별도로 필요합니다.').pack(pady=5)
    def selected():
        indexes=listing.curselection()
        if not indexes:raise ValueError('목록에서 방을 선택하세요.')
        return indexes[0]
    def guard(action):
        try:action();refresh()
        except Exception as exc:messagebox.showerror('설정 확인',str(exc))
    def add():
        item={'id':uuid.uuid4().hex,'window_title':title.get().strip(),'order_key':key.get().strip(),'enabled':True}
        validate_config(dict(config,allowed_rooms=config['allowed_rooms']+[item]));config['allowed_rooms'].append(item);title.set('');key.set('')
    def toggle():
        item=config['allowed_rooms'][selected()];candidate=dict(item,enabled=not item['enabled'])
        validate_config(dict(config,allowed_rooms=[candidate if x is item else x for x in config['allowed_rooms']]));item['enabled']=candidate['enabled']
    def remove():del config['allowed_rooms'][selected()]
    def update_key():
        item=config['allowed_rooms'][selected()]
        if not key.get().strip():raise ValueError('주문 키를 입력하세요.')
        item['order_key']=key.get().strip()
    def save():
        validate_config(config)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,path)
        messagebox.showinfo('저장 완료','허용 목록을 저장했습니다. 실행 중 수집기도 다음 주기부터 반영합니다.')
    buttons=tk.Frame(root);buttons.pack(pady=12)
    for label,fn in [('방 추가',add),('ON/OFF',toggle),('방 삭제',remove),('주문 키 변경',update_key),('저장',save)]:tk.Button(buttons,text=label,command=lambda f=fn:guard(f)).pack(side='left',padx=5)
    refresh();root.mainloop()
if __name__=='__main__':main()
