"""Capture only an allowlisted Kakao window without activating it.

This checks rendering only. A nonblank bitmap does not prove OCR correctness,
complete links, hidden rooms, or reliable automatic order collection.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from room_access import validate_config,resolve_window,check_window,RoomBlocked


def capture(config,room,destination):
    from pywinauto import Desktop
    import win32gui,win32ui
    entries=[e for e in validate_config(config) if e['window_title']==room]
    if len(entries)!=1:raise RoomBlocked('고객방 등록을 확인해야 합니다.')
    entry=entries[0];window=resolve_window(Desktop(backend='win32'),entry)
    check_window(window,entry)
    left,top,right,bottom=win32gui.GetWindowRect(window.handle)
    width,height=right-left,bottom-top
    if not 0<width<=4096 or not 0<height<=4096:raise ValueError('창 크기를 확인해야 합니다.')
    dc_handle=win32gui.GetWindowDC(window.handle)
    if not dc_handle:raise RuntimeError('window_dc_unavailable')
    source=None;memory=None;bitmap=None;old=None
    try:
        source=win32ui.CreateDCFromHandle(dc_handle)
        memory=source.CreateCompatibleDC()
        bitmap=win32ui.CreateBitmap();bitmap.CreateCompatibleBitmap(source,width,height)
        old=memory.SelectObject(bitmap)
        user=ctypes.WinDLL('user32',use_last_error=True)
        from ctypes import wintypes
        user.PrintWindow.argtypes=[wintypes.HWND,wintypes.HDC,wintypes.UINT]
        user.PrintWindow.restype=wintypes.BOOL
        ok=user.PrintWindow(window.handle,memory.GetSafeHdc(),2)
        check_window(window,entry)
        if not ok:raise RuntimeError('background_render_unavailable')
        pixels=bitmap.GetBitmapBits(True)
        info=bitmap.GetInfo();step=max(4,(len(pixels)//4096)//4*4)
        colors={pixels[i:i+3] for i in range(0,len(pixels)-3,step)} if info['bmBitsPixel']==32 else set()
        bitmap.SaveBitmapFile(memory,str(destination))
        return {'capture_returned':True,'width':width,'height':height,
                'sampled_colors':len(colors),'nonblank_candidate':len(colors)>8,
                'focus_keys_clipboard_used':False,'ocr_verified':False,
                'image_path':str(destination),'image_uploaded':False}
    finally:
        if memory and old:memory.SelectObject(old)
        if bitmap:win32gui.DeleteObject(bitmap.GetHandle())
        if memory:memory.DeleteDC()
        if source:source.DeleteDC()
        win32gui.ReleaseDC(window.handle,dc_handle)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--room',required=True)
    parser.add_argument('--worker',action='store_true');args=parser.parse_args()
    if not args.worker:
        try:
            completed=subprocess.run([sys.executable,__file__,'--config',args.config,'--room',args.room,'--worker'],
                capture_output=True,text=True,encoding='utf-8',timeout=10,
                env=dict(os.environ,PYTHONIOENCODING='utf-8'))
            print(completed.stdout.strip() or json.dumps({'capture_failed':True,'error_type':'worker_failed'}))
        except subprocess.TimeoutExpired:print(json.dumps({'capture_failed':True,'error_type':'render_timeout'}))
        return
    try:
        config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        folder=Path(os.environ['LOCALAPPDATA'])/'TaesanPlanningTest';folder.mkdir(exist_ok=True)
        print(json.dumps(capture(config,args.room,folder/'background-render-test.bmp'),ensure_ascii=False))
    except Exception as e:print(json.dumps({'capture_failed':True,'error_type':type(e).__name__}))


if __name__=='__main__':main()
