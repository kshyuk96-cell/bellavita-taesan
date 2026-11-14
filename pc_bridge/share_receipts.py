"""Durable single-attempt reservations, including separately labelled human evidence."""
import hashlib
import sqlite3


def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY,state TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS receipt_evidence (
        id TEXT PRIMARY KEY, destination TEXT NOT NULL, body_digest TEXT NOT NULL,
        source TEXT NOT NULL, evidence TEXT NOT NULL)''')
    db.commit()


def digest(body):
    return hashlib.sha256(body.replace('\r\n','\n').encode('utf-8')).hexdigest()


def reserve(db, key, destination, body):
    """Concurrent workers cannot both reserve; legacy or changed evidence stays blocked."""
    with db:
        result=db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)',(key,'sending'))
        if result.rowcount != 1:
            return False
        db.execute('INSERT INTO receipt_evidence VALUES (?,?,?,?,?)',
                   (key,destination,digest(body),'automatic_attempt','No receipt yet'))
    return True


def record_human_confirmation(db, key, destination, body, evidence):
    if destination != '기획' or not evidence.strip():
        raise ValueError('Exact test room and explicit human evidence required')
    with db:
        old=db.execute('SELECT destination,body_digest FROM receipt_evidence WHERE id=?',(key,)).fetchone()
        if old and tuple(old)!=(destination,digest(body)):
            raise ValueError('Existing confirmation refers to different content')
        db.execute('INSERT OR REPLACE INTO receipt_evidence VALUES (?,?,?,?,?)',
                   (key,destination,digest(body),'user_confirmed',evidence))
        db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?)',(key,'user_confirmed'))


def import_confirmations(db, manifest):
    for item in manifest.get('human_confirmed_messages',[]):
        if item.get('order_id')!=manifest['order_id'] or item.get('destination')!=manifest['destination']:
            raise ValueError('Human confirmation target mismatch')
        record_human_confirmation(db,manifest['order_id']+':'+item['kind'],
                                  item['destination'],item['body'],item['evidence'])
