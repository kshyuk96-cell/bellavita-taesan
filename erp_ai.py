"""Restricted ERP gateway using this service's existing OpenAI key. Never returns credentials."""
import json
import os
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ACTIVE=threading.BoundedSemaphore(2)

def dispatch(handler,method,path):
    routes={'/erp/ai/responses':'responses','/erp/ai/audio/transcriptions':'audio/transcriptions'}
    if path not in routes:return False
    if method!='POST':handler.send_json({'error':'Method not allowed'},405);return True
    key=os.getenv('OPENAI_API_KEY','').strip()
    if not key:handler.send_json({'error':'기존 AI 키 연결을 확인하세요.'},503);return True
    if not ACTIVE.acquire(blocking=False):handler.send_json({'error':'AI 요청이 겹쳤습니다. 잠시 후 다시 요청하세요.'},429);return True
    try:
        size=int(handler.headers.get('Content-Length','0'))
        if not 0<size<=16*1024*1024:raise ValueError()
        raw=handler.rfile.read(size);mime=handler.headers.get('Content-Type','')
        if path.endswith('/responses'):
            data=json.loads(raw)
            if not isinstance(data,dict) or data.get('stream') or data.get('tools') or data.get('previous_response_id'):raise ValueError()
            if type(data.get('max_output_tokens')) is not int or not 1<=data['max_output_tokens']<=3000:raise ValueError()
            configured=os.getenv('TAESAN_MODEL','')
            if data.get('model')!=configured:raise ValueError()
            data['store']=False;raw=json.dumps(data,ensure_ascii=False).encode();mime='application/json'
        elif not mime.startswith('multipart/form-data;') or size>11*1024*1024:raise ValueError()
        request=Request('https://api.openai.com/v1/'+routes[path],data=raw,headers={'Authorization':'Bearer '+key,'Content-Type':mime},method='POST')
        try:
            with urlopen(request,timeout=50) as reply:
                payload=reply.read(8*1024*1024+1)
                if len(payload)>8*1024*1024:raise ValueError()
                value=json.loads(payload)
            handler.send_json(value)
        except HTTPError as error:
            status=error.code if error.code in (400,401,403,404,408,429) else 502
            handler.send_json({'error':'AI 공급자 연결 또는 모델 설정을 확인하세요.'},status)
        except (OSError,URLError):handler.send_json({'error':'AI 응답을 확인하지 못했습니다.'},502)
    except (ValueError,TypeError,UnicodeError):handler.send_json({'error':'지원하는 AI 요청 형식과 모델을 확인하세요.'},400)
    finally:ACTIVE.release()
    return True
