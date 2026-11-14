"""Planning-room test markers only; no customer content or Sheet commands sent."""
import json
import re
import sqlite3
import secrets
import time
import urllib.request
from room_access import RoomBlocked
from share_receipts import setup,reserve

ROOM='기획'
ORIGIN='https://bellavita-taesan-test.onrender.com'
MARKER=re.compile(r'태산이 수신 테스트 ([a-f0-9]{24})')


def markers(snapshot):
    if not isinstance(snapshot,str) or len(snapshot)>1_000_000:
        raise ValueError('Invalid snapshot')
    # Kakao exported/copied text may prefix a message with [sender] [time].
    # Ignore every other line. Only the fixed marker and random nonce leave PC.
    result=set()
    for line in snapshot.splitlines():
        text=re.sub(r'^\[[^\]\r\n]{1,100}\] \[[^\]\r\n]{1,100}\] ', '', line)
        if MARKER.fullmatch(text):result.add(text)
    return result


class Receiver:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS seen_test_markers (marker TEXT PRIMARY KEY)')
        self.db.execute('CREATE TABLE IF NOT EXISTS receiver_state (id INTEGER PRIMARY KEY CHECK(id=1))')
        self.db.execute('CREATE TABLE IF NOT EXISTS armed_test_markers (marker TEXT PRIMARY KEY,expires REAL NOT NULL)')
        setup(self.db)
    def arm(self,ttl=900):
        if not self.db.execute('SELECT 1 FROM receiver_state WHERE id=1').fetchone():
            raise ValueError('Capture the initial baseline before creating a new test')
        if not 1<=ttl<=900:raise ValueError('Test expiry must be 1 to 900 seconds')
        marker='태산이 수신 테스트 '+secrets.token_hex(12)
        with self.db:
            self.db.execute('DELETE FROM armed_test_markers')
            self.db.execute('INSERT INTO armed_test_markers VALUES (?,?)',(marker,time.time()+ttl))
        return marker
    def observe(self,snapshot,room,requester,sender):
        if room!=ROOM:raise RoomBlocked('Only the exact planning room is authorized')
        items=markers(snapshot)
        if not self.db.execute('SELECT 1 FROM receiver_state WHERE id=1').fetchone():
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO seen_test_markers VALUES (?)',((s,) for s in items))
                self.db.execute('INSERT INTO receiver_state VALUES (1)')
            return 'baseline_saved'
        for text in sorted(items):
            arm=self.db.execute('SELECT expires FROM armed_test_markers WHERE marker=?',(text,)).fetchone()
            if not arm or arm[0]<time.time():continue
            with self.db:
                new=self.db.execute('INSERT OR IGNORE INTO seen_test_markers VALUES (?)',(text,)).rowcount==1
            if not new:continue
            nonce=MARKER.fullmatch(text)[1]
            # Persist before HTTP: ambiguous errors remain visible, never auto-retry.
            key='probe:'+nonce
            if not reserve(self.db,key+':request',ROOM,text):continue
            payload={'schema':'planning-probe-v1','room':ROOM,'text':text}
            try:
                response=requester(payload)
                expected=f'[태산이 연결 확인 {nonce}] 기획방 수신 확인 / 시트 변경 없음'
                if (response.get('schema')!='planning-probe-reply-v1' or response.get('room')!=ROOM
                        or response.get('nonce')!=nonce or response.get('reply')!=expected
                        or response.get('sheet_written') is not False):
                    raise ValueError('Server reply does not match this test')
                with self.db:self.db.execute('UPDATE receipts SET state=? WHERE id=?',('server_replied',key+':request'))
                if not reserve(self.db,key+':reply',ROOM,expected):continue
                confirmed=sender({'destination':ROOM,'body':expected})
                with self.db:
                    self.db.execute('UPDATE receipts SET state=? WHERE id=?',
                                    ('sent' if confirmed else 'uncertain',key+':reply'))
            except Exception:
                with self.db:
                    self.db.execute("UPDATE receipts SET state='uncertain' WHERE id=? AND state='sending'",(key+':request',))
                raise
        return 'checked'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def request_probe(payload,token):
    if not isinstance(token,str) or len(token)<32:raise ValueError('Dedicated PC test token is not configured')
    body=json.dumps(payload,ensure_ascii=False).encode()
    req=urllib.request.Request(ORIGIN+'/pc/planning-probe',data=body,
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=10) as response:
        raw=response.read(4097)
        if len(raw)>4096:raise ValueError('Unexpected response size')
        return json.loads(raw)


class WindowsChatCopyPort:
    def __init__(self,room=ROOM,config=None):
        from pywinauto import Desktop
        from room_access import resolve_window,validate_config
        if room!=ROOM:
            if config is None or room not in {e['window_title'] for e in validate_config(config)}:
                raise RoomBlocked('Customer room is not explicitly enabled')
        self.room=room
        self.window=resolve_window(Desktop(backend='win32'),{'window_title':room})
        lists=[c for c in self.window.descendants(class_name='EVA_VH_ListControl_Dblclk')
               if c.is_visible() and c.is_enabled()]
        if len(lists)!=1:raise RoomBlocked('Chat list is missing or ambiguous')
        self.chat=lists[0]
    def validate(self):
        import win32gui,win32process
        from room_access import check_window
        check_window(self.window,{'window_title':self.room})
        thread=win32process.GetWindowThreadProcessId(self.window.handle)[0]
        if (win32gui.GetForegroundWindow()!=self.window.handle or
            win32gui.GetGUIThreadInfo(thread)['hwndFocus']!=self.chat.handle or
            win32gui.GetAncestor(self.chat.handle,2)!=self.window.handle or
            win32gui.GetClassName(self.chat.handle)!='EVA_VH_ListControl_Dblclk'):
            raise RoomBlocked('Focus must remain on the planning-room chat list')
    def copy(self):
        import win32clipboard as cb
        from pywinauto.keyboard import send_keys
        import time
        self.validate()
        sequence=cb.GetClipboardSequenceNumber()
        send_keys('^a',pause=.05)
        self.validate()
        send_keys('^c',pause=.05)
        for _ in range(10):
            time.sleep(.1);self.validate()
            if cb.GetClipboardSequenceNumber()!=sequence:
                cb.OpenClipboard()
                try:
                    if not cb.IsClipboardFormatAvailable(13):raise RoomBlocked('Copied content is not text')
                    value=cb.GetClipboardData(13)
                finally:cb.CloseClipboard()
                self.validate()
                return value
        raise RoomBlocked('No fresh copy result; stale clipboard was not used')


def main():
    import argparse,os
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default='planning-receiver.sqlite3')
    parser.add_argument('--capture-only',action='store_true')
    parser.add_argument('--new-test',action='store_true')
    args=parser.parse_args()
    if args.new_test:
        receiver=Receiver(args.db)
        try:print(receiver.arm())
        finally:receiver.db.close()
        return
    snapshot=WindowsChatCopyPort().copy()
    if args.capture_only:
        print('테스트 문구 수:',len(markers(snapshot)))
        print('대화 원문 출력·서버 전송·시트 변경 없음')
        return
    # Explicitly require separate provisioning. Never repurpose the production
    # webhook secret, customer identity or invitation/payment service token.
    token=os.getenv('TAESAN_PC_PROBE_TOKEN','')
    if len(token)<32:raise ValueError('Dedicated PC test connection is not configured')
    from workflow import WindowsTextSender
    config={'allowed_rooms':[],'routing':{'mode':'test','test_room':ROOM,
        'sending_enabled':True,'input_method':'native_clipboard'}}
    receiver=Receiver(args.db)
    try:
        print(receiver.observe(snapshot,ROOM,lambda p:request_probe(p,token),WindowsTextSender(config)))
    finally:receiver.db.close()


if __name__=='__main__':main()
