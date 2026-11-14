"""Allow employees to turn on supported, reversible shortcuts for their own menu."""
import re


REPORT = r'(?:최근|마지막|지난)\s*배송\s*(?:루트\s*)?보고서'
REPORT_REQUEST = re.compile(
    REPORT + r'(?:\s*(?:다운로드|엑셀|이미지)?\s*(?:링크|주소))?(?:를|을)?'
    r'(?:\s*다시)?(?:\s*(?:보여|보내|열어)\s*(?:줘|주세요|줘요)|\s*재발급(?:해\s*(?:줘|주세요|줘요))?)?[.!?]?'
)
MUTATION = re.compile(
    rf'(?:내\s*(?:업무\s*)?메뉴(?:에(?:서)?|에서)?\s+{REPORT}|{REPORT}(?:를|을)?\s+내\s*(?:업무\s*)?메뉴(?:에(?:서)?|에서)?)'
    r'\s*(?:버튼(?:을|도)?\s*)?(?P<verb>추가|넣어|달아|띄워|빼|제거|삭제|숨겨)'
    r'(?:해\s*)?(?:\s*줘|\s*주세요|\s*줘요)?[.!]?'
)


def setup(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS staff_report_shortcuts (owner TEXT PRIMARY KEY)')


def requested_change(message):
    match = MUTATION.fullmatch(message)
    if not match:
        return None
    return match['verb'] in ('추가', '넣어', '달아', '띄워')


def update(connection, owner, enabled):
    if enabled:
        connection.execute('INSERT OR IGNORE INTO staff_report_shortcuts VALUES (?)', (owner,))
    else:
        connection.execute('DELETE FROM staff_report_shortcuts WHERE owner=?', (owner,))


def enabled(connection, owner):
    return connection.execute('SELECT 1 FROM staff_report_shortcuts WHERE owner=?', (owner,)).fetchone() is not None
