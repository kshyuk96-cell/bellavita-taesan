"""Opt-in, single-instance scheduled snapshots with automatic restore drills."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

import manage_data


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_backup_runs (
        id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL,
        state TEXT NOT NULL, snapshot TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')''')


def files(root):
    return {k:v for k,v in manage_data.inventory(root).items()
            if k not in ('state.sqlite3','state.sqlite3-wal','state.sqlite3-shm','state.sqlite3-journal')}


def snapshot(root,destination):
    root,destination=Path(root).resolve(),Path(destination).resolve()
    # This reserved write lock prevents DB-backed shop mutations while files and
    # SQLite are copied. Non-cooperating invitation/file writers are detected by
    # before/after hashes; an unstable snapshot is never marked verified.
    lock=sqlite3.connect(root/'state.sqlite3',timeout=10)
    try:
        lock.execute('BEGIN IMMEDIATE')
        before=files(root)
        manage_data.transfer(root,destination)
        if before!=files(root) or before!=files(destination):
            raise ValueError('snapshot_files_changed')
    finally:lock.rollback();lock.close()
    with tempfile.TemporaryDirectory(prefix='.restore-drill-',dir=destination.parent) as temporary:
        restored=Path(temporary)/'restored'
        manage_data.transfer(destination,restored,restore=True)
        if manage_data.inspect(destination)['counts']!=manage_data.inspect(restored)['counts']:
            raise ValueError('restore_counts_changed')
        with sqlite3.connect(restored/'state.sqlite3') as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shop_artifacts'").fetchone():
                for order_id,version,png_hash,pdf_hash in c.execute('SELECT order_id,version,png_hash,pdf_hash FROM shop_artifacts'):
                    import production_files
                    for ext,expected in (('png',png_hash),('pdf',pdf_hash)):
                        if hashlib.sha256(production_files.artifact_path(restored,order_id,version,ext).read_bytes()).hexdigest()!=expected:
                            raise ValueError('restored_artifact_hash_changed')
        with sqlite3.connect(restored/'state.sqlite3') as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shop_delivery_photos'").fetchone():
                import delivery_photos
                for order_id,photo_id,expected in c.execute('SELECT order_id,id,sha256 FROM shop_delivery_photos'):
                    if hashlib.sha256(delivery_photos.photo_path(restored,order_id,photo_id).read_bytes()).hexdigest()!=expected:
                        raise ValueError('restored_delivery_photo_changed')
        with sqlite3.connect(restored/'state.sqlite3') as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shop_service_photos'").fetchone():
                import shop_service
                for case_id,photo_id,expected in c.execute('SELECT case_id,id,sha256 FROM shop_service_photos'):
                    if hashlib.sha256(shop_service.photo_path(restored,case_id,photo_id).read_bytes()).hexdigest()!=expected:raise ValueError('restored_service_photo_changed')
        def logical(path):
            with sqlite3.connect(path/'state.sqlite3') as c:
                digest=hashlib.sha256()
                for line in c.iterdump():digest.update(line.encode())
                return digest.hexdigest()
        if logical(destination)!=logical(restored):raise ValueError('restore_sql_changed')
    return manage_data.inspect(destination)


def process_one(db,root,clock=time.time):
    if os.getenv('SHOP_BACKUP_ENABLED')!='1':return None
    base=Path(os.getenv('SHOP_BACKUP_DIRECTORY','')).expanduser()
    if not base.is_absolute():raise ValueError('SHOP_BACKUP_DIRECTORY에는 원본 밖의 절대 경로가 필요합니다.')
    base=base.resolve();root=Path(root).resolve()
    if base==root or root in base.parents:raise ValueError('백업은 데이터 디렉터리 밖에 보관해야 합니다.')
    interval=int(os.getenv('SHOP_BACKUP_INTERVAL_SECONDS','86400'))
    if not 60<=interval<=604800:raise ValueError('백업 간격은 60초~7일입니다.')
    base.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (base/'.scheduler.lock').open('a') as guard:
        try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return None
        current=clock()
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE shop_backup_runs SET state='failed',note='interrupted_before_verification',finished=? WHERE state='running'",(current,))
            last=c.execute('SELECT * FROM shop_backup_runs ORDER BY id DESC LIMIT 1').fetchone()
            if last and current-last['started']<interval:return None
            target=base/('shop-'+str(time.time_ns()))
            cursor=c.execute("INSERT INTO shop_backup_runs(started,state,snapshot) VALUES (?,'running',?)",(current,str(target)))
            run_id=cursor.lastrowid
        try:
            staging=base/('.pending-'+target.name)
            report=snapshot(root,staging)
            staging.rename(target)
            note=json.dumps({'integrity':'ok','restore_drill':'passed','counts':report['counts']},ensure_ascii=False)
            state='verified'
        except Exception as error:
            # A failed directory is preserved for diagnosis, never offered as a
            # verified recovery point. The previous verified backup is untouched.
            if 'staging' in locals() and staging.exists():
                failed=base/('.failed-'+target.name);staging.rename(failed);target=failed
            note=type(error).__name__;state='failed'
        with db() as c:c.execute('UPDATE shop_backup_runs SET state=?,finished=?,note=?,snapshot=? WHERE id=?',(state,clock(),note,str(target),run_id))
        return {'id':run_id,'state':state,'snapshot':str(target),'note':note}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data',required=True);parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    root=Path(args.data).resolve()
    def db():
        c=sqlite3.connect(root/'state.sqlite3',timeout=10);c.row_factory=sqlite3.Row;setup(c);return c
    while True:
        result=process_one(db,root)
        if result:print(json.dumps(result,ensure_ascii=False),flush=True)
        if not args.watch:break
        time.sleep(10)
