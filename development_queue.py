"""Handoff for authorized Kakao development requests."""
import hashlib
import re
import sqlite3
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


PUBLIC_KEY = '4e2298ef0c0eaca4e4e6cbd2d84b6f22d910fab3ffdf898a85bc2603067680e1'
REQUEST_LIFETIME = 90
REQUEST = re.compile(r'(?:기능\s*(?:개발|개선)(?:\s*(?:요청|해\s*줘))?|개발\s*요청)(?:\s*[:：]\s*|\s+)(\S.*)', re.S)
NATURAL_REQUEST = re.compile(r'(?P<detail>.+?)(?:[.。]|\s)\s*(?:개발|구현)\s*해\s*(?:줘|주세요|주라)?[.!]?$', re.S)
DUPLICATE_ALERT_REQUEST = re.compile(
    r'(?=.*배송\s*일시)(?=.*배송\s*장소)(?=.*보내는\s*분\s*요약)'
    r'(?=.*중복\s*입력)(?=.*(?:경고|알림)).+', re.S)
APPROVE = re.compile(r'(?:(?:이거|이\s*요청|그거)\s*)?(?:승인(?:\s*(?:해|할게|합니다)(?:\s*줘)?)?|진행해\s*줘)[.!]?$')
INBOX = re.compile(r'(?:기능\s*)?개발\s*요청(?:\s*(?:보기|보여\s*(?:줘|주세요)|확인))?[.!?]?$')
STATUS = re.compile(r'(?:내\s*)?(?:최근\s*)?개발\s*요청\s*(?:상태|어떻게\s*됐(?:어|나요))(?:\s*(?:알려|보여)\s*(?:줘|주세요))?[.!?]?')
EMPTY = '승인할 개발 요청이 없어요.'


def request_detail(message):
    if match := REQUEST.fullmatch(message):
        return match[1]
    if DUPLICATE_ALERT_REQUEST.fullmatch(message):
        return message
    if match := NATURAL_REQUEST.fullmatch(message):
        detail = match['detail'].strip()
        if len(detail) >= 8 and re.search(r'기능|태산|코드|프로그램|메뉴|조회|알림', detail):
            return detail
    return None


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS development_work (
        job TEXT PRIMARY KEY, detail TEXT NOT NULL, approved REAL NOT NULL,
        completed REAL, result TEXT)''')
    c.execute('CREATE TABLE IF NOT EXISTS development_focus (owner TEXT PRIMARY KEY, job TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS development_signatures (nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL)')


def enqueue(c, job, detail):
    c.execute('INSERT OR IGNORE INTO development_work (job,detail,approved) VALUES (?,?,?)',
              (job, detail[:400], time.time()))


def authorized(c, authorization, method, body=b''):
    match = re.fullmatch(r'Taesan-Signature ([0-9]{10})\.([a-f0-9]{32})\.([a-f0-9]{128})', authorization)
    if not match or abs(time.time() - int(match[1])) > REQUEST_LIFETIME:
        return False
    timestamp, nonce, signature = match.groups()
    payload = f'{method}\n/development-queue\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY)).verify(bytes.fromhex(signature), payload.encode())
    except InvalidSignature:
        return False
    c.execute('DELETE FROM development_signatures WHERE expires<?', (int(time.time()),))
    try:
        c.execute('INSERT INTO development_signatures VALUES (?,?)', (nonce, int(timestamp) + REQUEST_LIFETIME + 1))
    except sqlite3.IntegrityError:
        return False
    return True


def read(c):
    rows = c.execute('''SELECT job,detail FROM development_work WHERE completed IS NULL
                        ORDER BY approved,job LIMIT 20''').fetchall()
    return [dict(row) for row in rows]


def latest_status(c, owner):
    row = c.execute('''SELECT w.job, w.completed FROM jobs j
                       LEFT JOIN development_work w ON w.job=j.id
                       WHERE j.owner=? AND j.state='done' AND json_extract(j.result,'$.development')=1
                       ORDER BY j.created DESC LIMIT 1''', (owner,)).fetchone()
    if not row:
        return '최근에 접수한 개발 요청이 없어요.'
    if row['completed'] is not None:
        return '최근 개발 요청은 검증을 마쳤어요. 운영 반영 상태는 별도로 확인해 주세요.'
    if row['job']:
        return '최근 개발 요청은 작업 대기 중이에요.'
    return '최근 개발 요청은 대표님 승인 대기 중이에요.'


def complete(c, job, result):
    if (not re.fullmatch(r'[a-f0-9]{12}', job)
            or not re.fullmatch(r'https://github\.com/kshyuk96-cell/bellavita-taesan/pull/[1-9][0-9]*', result)):
        return False
    return bool(c.execute('''UPDATE development_work SET completed=COALESCE(completed,?),result=?
                             WHERE job=? AND (completed IS NULL OR result=?)''',
                          (time.time(), result, job, result)).rowcount)
