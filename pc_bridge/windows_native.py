"""Use stable Win32 APIs directly, independent of optional pywin32 wrappers."""
import ctypes
from ctypes import wintypes


def focus_handle(window):
    class Rect(ctypes.Structure):
        _fields_=[('left',wintypes.LONG),('top',wintypes.LONG),('right',wintypes.LONG),('bottom',wintypes.LONG)]
    class Info(ctypes.Structure):
        _fields_=[('cbSize',wintypes.DWORD),('flags',wintypes.DWORD),
                  ('hwndActive',wintypes.HWND),('hwndFocus',wintypes.HWND),
                  ('hwndCapture',wintypes.HWND),('hwndMenuOwner',wintypes.HWND),
                  ('hwndMoveSize',wintypes.HWND),('hwndCaret',wintypes.HWND),('rcCaret',Rect)]
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype=wintypes.DWORD
    user.GetGUIThreadInfo.argtypes=[wintypes.DWORD,ctypes.POINTER(Info)]
    user.GetGUIThreadInfo.restype=wintypes.BOOL
    thread=user.GetWindowThreadProcessId(window,None)
    info=Info();info.cbSize=ctypes.sizeof(Info)
    if not thread or not user.GetGUIThreadInfo(thread,ctypes.byref(info)):
        raise OSError('Cannot verify target input focus')
    return int(info.hwndFocus or 0)


def root_handle(window):
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
    user.GetAncestor.restype=wintypes.HWND
    return int(user.GetAncestor(window,2) or 0)
