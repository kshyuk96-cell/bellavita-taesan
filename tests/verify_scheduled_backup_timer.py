"""Real 60-second schedule in a fresh synthetic environment, no service credentials."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import seed_shop_demo
import scheduled_backups


def main():
    with tempfile.TemporaryDirectory(prefix='bellavita-backup-timer-') as temporary:
        root=Path(temporary)/'data';base=Path(temporary)/'backups';seed_shop_demo.seed(root)
        os.environ.update(SHOP_BACKUP_ENABLED='1',SHOP_BACKUP_DIRECTORY=str(base),SHOP_BACKUP_INTERVAL_SECONDS='60')
        def db():
            c=sqlite3.connect(root/'state.sqlite3',timeout=10);c.row_factory=sqlite3.Row;return c
        first=scheduled_backups.process_one(db,root);assert first['state']=='verified',first
        print('First scheduled snapshot and restore drill passed.',flush=True)
        started=time.monotonic();second=None
        while time.monotonic()-started<75:
            # Concurrent normal service work is represented by an independent
            # committed SQLite writer between snapshots.
            with db() as c:c.execute("UPDATE shop_offers SET active=active WHERE rowid=1")
            second=scheduled_backups.process_one(db,root)
            if second:break
            time.sleep(2)
        assert second and second['state']=='verified',second
        with db() as c:rows=c.execute('SELECT state FROM shop_backup_runs ORDER BY id').fetchall()
        assert [r[0] for r in rows]==['verified','verified']
        result={'real_interval_seconds':60,'elapsed_seconds':round(time.monotonic()-started,1),'scheduled_snapshots':2,
                'automatic_restore_drills':2,'service_writes_between_backups':True,'sample_products':5,'real_payments':False}
        path=Path(__file__).resolve().parents[1]/'test-artifacts'/'scheduled-backup-timer.json';path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)


if __name__=='__main__':main()
