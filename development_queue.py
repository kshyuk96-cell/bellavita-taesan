"""Handoff for authorized Kakao development requests."""
import hashlib
import hmac
import re
import time


READER_DIGEST = '31bb0f39b2b47ca1643f6139436970e093275625b38e5c1907522fd002f72a19'
REQUEST = re.compile(r'(?:기능\s*(?:개발|개선)(?:\s*(?:요청|해\s*줘))?|개발\s*요청)(?:\s*[:：]\s*|\s+)(\S.*)', re.S)
DUPLICATE_ALERT_REQUEST = re.compile(
    r'(?=.*배송\s*일시)(?=.*배송\s*장소)(?=.*보내는\s*분\s*요약)'
    r'(?=.*중복\s*입력)(?=.*(?:경고|알림)).+', re.S)
APPROVE = re.compile(r'(?:(?:이거|이\s*요청|그거)\s*)?(?:승인(?:\s*(?:해|할게|합니다)(?:\s*줘)?)?|진행해\s*줘)[.!]?$')
INBOX = re.compile(r'(?:기능\s*)?개발\s*요청(?:\s*(?:보기|보여\s*(?:줘|주세요)|확인))?[.!?]?$')
EMPTY = '승인할 개발 요청이 없어요.'


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS development_work (
        job TEXT PRIMARY KEY, detail TEXT NOT NULL, approved REAL NOT NULL,
        completed REAL, result TEXT)''')
    c.execute('CREATE TABLE IF NOT EXISTS development_focus (owner TEXT PRIMARY KEY, job TEXT NOT NULL)')


def enqueue(c, job, detail):
    c.execute('INSERT OR IGNORE INTO development_work (job,detail,approved) VALUES (?,?,?)',
              (job, detail[:400], time.time()))


def authorized(authorization):
    secret = authorization.removeprefix('Bearer ')
    return (secret != authorization and len(secret) == 43
            and hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), READER_DIGEST))


def read(c):
    rows = c.execute('''SELECT job,detail FROM development_work WHERE completed IS NULL
                        ORDER BY approved,job LIMIT 20''').fetchall()
    return [dict(row) for row in rows]


def complete(c, job, result):
    if (not re.fullmatch(r'[a-f0-9]{12}', job)
            or not re.fullmatch(r'https://github\.com/kshyuk96-cell/bellavita-taesan/pull/[1-9][0-9]*', result)):
        return False
    return bool(c.execute('''UPDATE development_work SET completed=COALESCE(completed,?),result=?
                             WHERE job=? AND (completed IS NULL OR result=?)''',
                          (time.time(), result, job, result)).rowcount)
