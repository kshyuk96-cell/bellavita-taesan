"""Restricted ERP gateway using this service's existing OpenAI key. Never returns credentials."""
import base64
import json
import os
import threading
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ACTIVE=threading.BoundedSemaphore(2)

def event_scene_upload(raw):
    data=json.loads(raw)
    if not isinstance(data,dict) or set(data)!={'reference','prompt'}:
        raise ValueError()
    reference,prompt=data['reference'],data['prompt']
    if not isinstance(reference,str) or not 0<len(reference)<=2500000 or not isinstance(prompt,str) or not 0<len(prompt)<=700:
        raise ValueError()
    jpeg=base64.b64decode(reference,validate=True)
    if not jpeg.startswith(b'\xff\xd8\xff') or not jpeg.endswith(b'\xff\xd9'):
        raise ValueError()
    boundary=uuid.uuid4().hex
    fields={'model':'gpt-image-1','size':'1536x1024','quality':'low','output_format':'jpeg','prompt':prompt}
    parts=[f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
           for name,value in fields.items()]
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="scene.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode(),
                  jpeg,f'\r\n--{boundary}--\r\n'.encode()])
    return b''.join(parts),'multipart/form-data; boundary='+boundary

def dispatch(handler,method,path):
    routes={'/erp/ai/responses':'responses','/erp/ai/audio/transcriptions':'audio/transcriptions',
            '/erp/ai/images/edits':'images/edits'}
    if path not in routes:return False
    if method!='POST':handler.send_json({'error':'Method not allowed'},405);return True
    key=os.getenv('OPENAI_API_KEY','').strip()
    if not key:handler.send_json({'error':'기존 AI 키 연결을 확인하세요.'},503);return True
    if not ACTIVE.acquire(blocking=False):handler.send_json({'error':'AI 요청이 겹쳤습니다. 잠시 후 다시 요청하세요.'},429);return True
    try:
        size=int(handler.headers.get('Content-Length','0'))
        if not 0<size<=16*1024*1024:raise ValueError()
        raw=handler.rfile.read(size);mime=handler.headers.get('Content-Type','')
        event_scene=path.endswith('/images/edits')
        if path.endswith('/responses'):
            data=json.loads(raw)
            if not isinstance(data,dict) or data.get('stream') or data.get('tools') or data.get('previous_response_id'):raise ValueError()
            if type(data.get('max_output_tokens')) is not int or not 1<=data['max_output_tokens']<=3000:raise ValueError()
            configured=os.getenv('TAESAN_MODEL','')
            if data.get('model')!=configured:raise ValueError()
            data['store']=False;raw=json.dumps(data,ensure_ascii=False).encode();mime='application/json'
        elif event_scene:
            if not mime.startswith('application/json') or size>3*1024*1024:raise ValueError()
            raw,mime=event_scene_upload(raw)
        elif not mime.startswith('multipart/form-data;') or size>11*1024*1024:raise ValueError()
        request=Request('https://api.openai.com/v1/'+routes[path],data=raw,headers={'Authorization':'Bearer '+key,'Content-Type':mime},method='POST')
        try:
            with urlopen(request,timeout=78 if event_scene else 50) as reply:
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
