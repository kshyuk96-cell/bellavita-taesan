"""Test-only native Kakao clipboard transport. No WM_SETTEXT or cached HWNDs."""
import time
from room_access import RoomBlocked

ROOM = '기획'
PLACEHOLDERS = {'', '메시지 입력', '메시지를 입력하세요', '메시지를 입력하세요.',
                '메시지를 입력해주세요.', '메시지를 입력해 주세요.'}


def paste_checked(job, port, send=False):
    """Port makes focus loss, old drafts and ambiguous delivery testable offline."""
    if job.get('destination') != ROOM:
        raise RoomBlocked('이번 테스트 대상은 정확히 기획입니다.')
    body = job.get('body')
    if not isinstance(body, str) or not body.strip() or len(body) > 12000 or '\x00' in body:
        raise RoomBlocked('잘못된 메시지')
    port.validate()
    if port.read_input().strip() not in PLACEHOLDERS:
        raise RoomBlocked('기존 초안이 있어 중단했습니다.')
    port.focus_input()
    port.validate(focused=True)
    # The caller durably reserves this job before any PC interaction.
    with port.clipboard(body):
        port.validate(focused=True)
        if port.read_input().strip() not in PLACEHOLDERS:
            raise RoomBlocked('입력칸이 변경되었습니다.')
        port.paste()
        for _ in range(10):
            port.validate(focused=True)
            if port.read_input() == body:
                break
            time.sleep(.1)
        else:
            raise RoomBlocked('붙여넣기 결과 불명확: 재시도 전에 화면 확인 필요')
    if not send:
        return False  # A draft is never a sent receipt.
    before = port.receipt_ids(body)
    port.validate(focused=True)
    if port.read_input() != body:
        raise RoomBlocked('전송 직전 입력칸이 변경되었습니다.')
    port.enter()
    for _ in range(10):
        port.validate()
        if port.read_input().strip() in PLACEHOLDERS and port.receipt_ids(body) - before:
            return True
        time.sleep(.3)
    return False  # Empty input or pressing Enter alone does not prove delivery.


class WindowsPastePort:
    def __init__(self):
        from pywinauto import Desktop
        from room_access import resolve_window
        self.window = resolve_window(Desktop(backend='win32'), {'window_title': ROOM})
        self.inputs = self.window.descendants(class_name='RICHEDIT50W')
        self.inputs = [box for box in self.inputs if box.is_visible() and box.is_enabled()]
        if len(self.inputs) != 1:
            raise RoomBlocked('입력칸이 없거나 여러 개입니다.')
        self.box = self.inputs[0]

    def validate(self, focused=False):
        import win32gui
        from windows_native import focus_handle,root_handle
        from room_access import check_window
        check_window(self.window, {'window_title': ROOM})
        if not win32gui.IsWindow(self.box.handle) or root_handle(self.box.handle) != self.window.handle:
            raise RoomBlocked('입력칸이 변경되었습니다.')
        if win32gui.GetClassName(self.box.handle) != 'RICHEDIT50W':
            raise RoomBlocked('입력칸 종류가 변경되었습니다.')
        if focused:
            if (win32gui.GetForegroundWindow() != self.window.handle or
                    focus_handle(self.window.handle) != self.box.handle):
                raise RoomBlocked('대상 입력칸에서 포커스가 벗어났습니다.')

    def read_input(self):
        self.validate()
        return self.box.window_text()

    def focus_input(self):
        self.window.set_focus()
        self.box.set_focus()

    def paste(self):
        from pywinauto.keyboard import send_keys
        send_keys('^v', pause=.05)

    def enter(self):
        from pywinauto.keyboard import send_keys
        send_keys('{ENTER}', pause=.05)

    def receipt_ids(self, body):
        self.validate()
        # This PC exposes no calibrated outgoing-message direction/receipt ID.
        # Even a new identical Text node could be an incoming message or a draft.
        # Never promote such a candidate to an automatic delivery confirmation.
        return set()

    def clipboard(self, body):
        from contextlib import contextmanager
        import win32clipboard as cb
        @contextmanager
        def scope():
            cb.OpenClipboard()
            try:
                formats = []
                fmt = cb.EnumClipboardFormats(0)
                while fmt:
                    formats.append(fmt)
                    fmt = cb.EnumClipboardFormats(fmt)
                # Preserve text exactly; refuse to destroy images/files/rich formats.
                if set(formats) - {1, 7, 13, 16}:
                    raise RoomBlocked('클립보드에 이미지·파일 등이 있습니다. 먼저 일반 텍스트를 복사하세요.')
                old = cb.GetClipboardData(13) if cb.IsClipboardFormatAvailable(13) else None
                if formats and old is None:
                    raise RoomBlocked('기존 클립보드를 안전하게 보존할 수 없습니다.')
                cb.EmptyClipboard()
                cb.SetClipboardText(body, 13)
                sequence = cb.GetClipboardSequenceNumber()
            finally:
                cb.CloseClipboard()
            try:
                yield
            finally:
                cb.OpenClipboard()
                try:
                    # Do not overwrite a new clipboard value copied by the user.
                    if cb.GetClipboardSequenceNumber() == sequence:
                        cb.EmptyClipboard()
                        if old is not None:
                            cb.SetClipboardText(old, 13)
                finally:
                    cb.CloseClipboard()
        return scope()
