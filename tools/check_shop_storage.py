"""Inspect an explicitly selected existing store; never import or start the app."""
import argparse
import json
import os
from pathlib import Path
import sqlite3

TABLES=('invitation_orders','shop_products','shop_offers','shop_messages','shop_cash_receipts','shop_backup_runs')


def inspect(data):
    if not data:raise ValueError('missing_data_path')
    root=Path(data).expanduser().resolve(strict=True)
    database=root/'state.sqlite3'
    if not database.is_file():raise ValueError('existing_database_required')
    # mode=ro prevents changes to existing data and cannot create a database.
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as c:
        c.execute('BEGIN')
        integrity=c.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts={name:c.execute('SELECT count(*) FROM '+name).fetchone()[0] if name in tables else None for name in TABLES}
        products=None
        if 'shop_products' in tables:
            public=[json.loads(row[0]) for row in c.execute('SELECT details FROM shop_products') if json.loads(row[0]).get('public')]
            products={'public_count':len(public),'tax_type_missing_count':sum(p.get('cash_tax_type') not in ('TG01','TG02') for p in public)}
        c.rollback()
    return {'inspection_scope':'existing_storage_only','data_path':str(root),'database_exists':True,'database_bytes':database.stat().st_size,'integrity':'ok' if integrity else 'failed','under_var_data':root.is_relative_to(Path('/var/data')),'var_data_is_mount':os.path.ismount('/var/data'),'counts':counts,'public_catalog':products,'application_started':False,'database_writes':False,'external_requests':0,'provider_validation':'not_performed','release_approval':'not_evaluated'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data',default=os.getenv('TAESAN_DATA'));args=parser.parse_args()
    try:report=inspect(args.data)
    except (ValueError,OSError,sqlite3.Error):
        print(json.dumps({'inspection':'failed','reason':'explicit_existing_data_path_and_readable_database_required','database_writes':False,'external_requests':0}));return 2
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['integrity']=='ok' else 2


if __name__=='__main__':raise SystemExit(main())
