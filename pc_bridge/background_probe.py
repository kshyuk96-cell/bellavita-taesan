"""Read-only compatibility check. Never activates windows or uses keyboard/clipboard."""
import argparse
import ctypes
import json
from ctypes import wintypes
from pathlib import Path
from room_access import validate_config,resolve_window,check_window,RoomBlocked


def native_text(handle):
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.SendMessageTimeoutW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,
        wintypes.LPARAM,wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t)]
    user.SendMessageTimeoutW.restype=wintypes.LPARAM
    result=ctypes.c_size_t();buffer=ctypes.create_unicode_buffer(64001)
    ok=user.SendMessageTimeoutW(handle,13,len(buffer),ctypes.cast(buffer,ctypes.c_void_p).value,2,1000,ctypes.byref(result))
    if not ok:raise RuntimeError('native_read_unavailable')
    return buffer.value


def summarize(text,marker):
    import re
    return {'characters':len(text),'known_test_marker':bool(marker and marker in text),
            'transcript_boundaries':len(re.findall(r'(?m)^\[[^\]\r\n]+\] \[[^\]\r\n]+\] ',text))}


def probe(config,room,marker='TAESAN_BG_TEST_01'):
    from pywinauto import Desktop
    entries=[e for e in validate_config(config) if e['window_title']==room]
    if len(entries)!=1:raise RoomBlocked('정확히 등록한 고객방만 검사합니다.')
    entry=entries[0];window=resolve_window(Desktop(backend='win32'),entry)
    controls=window.descendants(class_name='EVA_VH_ListControl_Dblclk')
    if len(controls)!=1:raise RoomBlocked('대화 목록이 없거나 여러 개입니다.')
    check_window(window,entry);handle=controls[0].handle
    report={'room_matches':1,'focus_keys_clipboard_used':False,'methods':{}}
    def read(name,fn):
        try:
            check_window(window,entry)
            text=fn()
            check_window(window,entry)
            report['methods'][name]=summarize(text or '',marker)
        except Exception as e:report['methods'][name]={'available':False,'error_type':type(e).__name__}
    read('native_window_text',lambda:native_text(handle))
    uia=Desktop(backend='uia').window(handle=handle).wrapper_object()
    read('accessibility_name',lambda:uia.element_info.name)
    read('accessibility_text',lambda:uia.iface_text.DocumentRange.GetText(64000))
    read('legacy_accessibility_name',lambda:uia.iface_legacy_iaccessible.CurrentName)
    read('legacy_accessibility_value',lambda:uia.iface_legacy_iaccessible.CurrentValue)
    check_window(window,entry)
    report['background_body_confirmed']=any(v.get('known_test_marker') for v in report['methods'].values())
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--room',required=True)
    args=parser.parse_args()
    try:print(json.dumps(probe(json.loads(Path(args.config).read_text(encoding='utf-8-sig')),args.room),ensure_ascii=False))
    except Exception as e:print(json.dumps({'probe_failed':True,'error_type':type(e).__name__}))
