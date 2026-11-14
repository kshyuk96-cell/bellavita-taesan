"""Non-interfering polling of registered OPEN chat windows. OCR changes need review.

No foreground activation, keyboard, clipboard, URL fetch, Sheet write or chat send.
Closed/minimized/unreadable rooms are reported as errors rather than silently read.
"""
import argparse,asyncio,difflib,hashlib,json,os,re,sqlite3,subprocess,sys,time
from pathlib import Path
from room_access import validate_config
from order_preview import preview


def body_lines(lines,height):
    return [' '.join(line['text'].split()) for line in lines
            if line['text'].strip() and line['top']>=height*.10 and line['bottom']<=height*.85]


class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS ocr_frames (scope TEXT PRIMARY KEY,lines TEXT NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS ocr_reviews (
            id TEXT PRIMARY KEY,scope TEXT NOT NULL,changed_text TEXT NOT NULL,
            result TEXT NOT NULL,sightings INTEGER NOT NULL DEFAULT 1)''');self.db.commit()
    def observe(self,scope,lines):
        old=self.db.execute('SELECT lines FROM ocr_frames WHERE scope=?',(scope,)).fetchone()
        before=json.loads(old[0]) if old else []
        if old and before==lines:return {'state':'unchanged','new_orders':0}
        # Large erasure may mean occlusion, scrolling or OCR failure: keep baseline.
        if not lines:return {'state':'unreadable','new_orders':0}
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO ocr_frames VALUES (?,?)',(scope,json.dumps(lines,ensure_ascii=False)))
            if old is None:return {'state':'baseline_saved','new_orders':0}
            changed=[]
            for operation,_,__,start,end in difflib.SequenceMatcher(a=before,b=lines,autojunk=False).get_opcodes():
                if operation in {'insert','replace'}:changed.extend(lines[start:end])
            text='\n'.join(changed)
            if not text or not re.search(r'주문|화환|근조|부고|청첩|결혼|신랑|신부|보내는\s*분|행사|개업|취임|준공|https?|www\.',text,re.I):
                return {'state':'checked','new_orders':0}
            key=hashlib.sha256((scope+'\n'+text).encode()).hexdigest()
            try:result=preview(text)
            except ValueError as error:result={'text':str(error),'sheet_written':False}
            result['ocr_review_required']=True
            result['warning']='OCR 화면 변화 후보입니다. 새 메시지 경계·누락·오인식·잘린 링크를 확인해야 하며 자동 확정하지 않습니다.'
            added=self.db.execute('INSERT OR IGNORE INTO ocr_reviews(id,scope,changed_text,result) VALUES (?,?,?,?)',
                (key,scope,text,json.dumps(result,ensure_ascii=False))).rowcount
            if not added:self.db.execute('UPDATE ocr_reviews SET sightings=sightings+1 WHERE id=?',(key,))
            return {'state':'review_required','new_orders':added}


def config(path):
    result=json.loads(path.read_text(encoding='utf-8-sig'));validate_config(result);return result


def once(config_path,room,state):
    from background_capture import capture
    from background_ocr import recognize
    cfg=config(config_path);entries=[e for e in validate_config(cfg) if e['window_title']==room]
    if len(entries)!=1:raise ValueError('Room not enabled')
    entry=entries[0];scope=json.dumps([entry['id'],room],ensure_ascii=False)
    image=state/(hashlib.sha256(scope.encode()).hexdigest()+'.bmp')
    rendered=capture(cfg,room,image)
    if not rendered['nonblank_candidate']:raise ValueError('Blank render')
    recognized=asyncio.run(recognize(image,include_lines=True,save_text=False))
    if not recognized['ocr_available']:raise RuntimeError('Korean OCR unavailable')
    if entry not in validate_config(config(config_path)):raise ValueError('Room disabled during capture')
    store=Store(state/'background-orders.sqlite3')
    try:return store.observe(scope,body_lines(recognized['lines'],rendered['height']))
    finally:store.db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--room')
    parser.add_argument('--state',required=True);parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=int,default=5);args=parser.parse_args()
    path=Path(args.config);state=Path(args.state);state.mkdir(parents=True,exist_ok=True)
    if args.once:
        try:print(json.dumps(once(path,args.room,state)))
        except Exception as error:
            import traceback
            frames=traceback.extract_tb(error.__traceback__)
            location=(Path(frames[-1].filename).name+':'+str(frames[-1].lineno)) if frames else ''
            print(json.dumps({'state':'error','error_type':type(error).__name__,'error_location':location,'new_orders':0}))
        return
    while True:
        reports=[]
        try:
            entries=validate_config(config(path))
            for entry in entries:
                try:
                    run=subprocess.run([sys.executable,__file__,'--config',str(path),'--state',str(state),
                        '--once','--room',entry['window_title']],capture_output=True,text=True,encoding='utf-8',timeout=20,
                        env=dict(os.environ,PYTHONIOENCODING='utf-8'))
                    result=json.loads(run.stdout)
                except Exception as error:result={'state':'error','error_type':type(error).__name__,'new_orders':0}
                reports.append(dict(result,room=entry['window_title']))
            report={'rooms':reports,'timestamp':time.time(),'sheet_written':False,'focus_keys_clipboard_used':False}
        except Exception as error:report={'error_type':type(error).__name__,'rooms':[],'timestamp':time.time()}
        temp=state/'background-status.tmp';temp.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
        os.replace(temp,state/'background-status.json')
        time.sleep(max(5,min(args.interval,60)))


if __name__=='__main__':main()
