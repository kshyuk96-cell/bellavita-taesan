"""Explicit room allowlist. No message text is read before window identity checks."""
import ctypes
from pathlib import PureWindowsPath

class RoomBlocked(RuntimeError):pass

def validate_config(config):
    entries=config.get('allowed_rooms')
    if not isinstance(entries,list):raise RoomBlocked('먼저 주문 수집 방을 등록하세요. 기존 단일 방 설정은 자동 허용하지 않습니다.')
    active=[];ids=set();titles=set()
    for entry in entries:
        if not isinstance(entry,dict) or not isinstance(entry.get('enabled'),bool):raise RoomBlocked('잘못된 방 설정')
        if not entry['enabled']:continue
        title=entry.get('window_title','');rid=entry.get('id','')
        if not isinstance(title,str) or not title.strip() or title!=title.strip() or title in {'카카오톡','KakaoTalk'}:raise RoomBlocked('고객 채팅창의 정확한 제목이 필요합니다.')
        if not isinstance(rid,str) or not rid or rid in ids or title in titles:raise RoomBlocked('방 ID 또는 창 제목이 중복되었습니다.')
        destinations=config.get('routing',{})
        if title in {'기획','기획방',destinations.get('order_room','주문방'),destinations.get('confirmation_room','확인방'),destinations.get('test_room','기획')}:raise RoomBlocked('주문방·확인방은 공유 대상입니다. 고객 수집 방으로 등록할 수 없습니다.')
        if config.get('mode')=='floral' and not entry.get('order_key','').strip():raise RoomBlocked('각 방의 주문 키가 필요합니다.')
        ids.add(rid);titles.add(title);active.append(entry)
    return active

def process_name(pid):
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:raise RoomBlocked('카카오톡 프로세스를 확인할 수 없습니다.')
    try:
        size=wintypes.DWORD(32768);buffer=ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):raise RoomBlocked('프로세스 경로 확인 실패')
        return PureWindowsPath(buffer.value).name
    finally:kernel.CloseHandle(handle)

def check_window(window,entry,name_provider=process_name):
    if window.window_text()!=entry['window_title']:raise RoomBlocked('방 제목 변경: 수집 차단')
    if name_provider(window.process_id()).casefold()!='kakaotalk.exe':raise RoomBlocked('카카오톡 프로세스가 아닙니다.')
    if window.is_minimized():raise RoomBlocked('주문방 창을 복원하세요.')

def resolve_window(desktop,entry,name_provider=process_name):
    candidates=desktop.windows(title=entry['window_title'])
    if len(candidates)!=1:raise RoomBlocked('방 창이 없거나 같은 이름의 창이 여러 개입니다.')
    check_window(candidates[0],entry,name_provider)
    return candidates[0]

def read_snapshot(window,entry,pattern,control_type='Text',name_provider=process_name):
    check_window(window,entry,name_provider)
    handle=window.handle
    matches=[]
    for control in window.descendants(control_type=control_type):
        # Check before each text read as well as after the complete snapshot.
        check_window(window,entry,name_provider)
        match=pattern.fullmatch(control.window_text())
        if match:matches.append(match.groupdict())
    check_window(window,entry,name_provider)
    if window.handle!=handle:raise RoomBlocked('창이 바뀌어 수집 결과를 버립니다.')
    return matches


def enabled_titles(path, require_sync=False):
    import json
    from pathlib import Path
    config=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    entries=validate_config(config)
    if require_sync and not config.get('sync'):return set()
    return {e['window_title'] for e in entries}
