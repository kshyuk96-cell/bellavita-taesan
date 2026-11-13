"""Offline backup/restore of TAESAN_DATA. Never overwrite an existing directory."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('심볼릭 링크가 있는 데이터는 먼저 실제 파일로 정리하세요.')
        if path.is_file() and path.name != 'backup-manifest.json':
            result[path.relative_to(root).as_posix()] = digest(path)
    return result


def check_database(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite 무결성 검사 실패')


def inspect(source):
    root = Path(source).resolve()
    check_database(root / 'state.sqlite3')
    with closing(sqlite3.connect((root / 'state.sqlite3').as_uri() + '?mode=ro', uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {name: connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                  for name in ('shop_products', 'shop_offers', 'invitation_orders', 'payment_events', 'payment_checks')
                  if name in tables}
    return {'data_path': str(root), 'under_var_data': root.is_relative_to(Path('/var/data').resolve()),
            'var_data_is_mount': os.path.ismount('/var/data'), 'integrity_check': 'ok', 'counts': counts}


def transfer(source, destination, *, restore=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination or source in destination.parents:
        raise ValueError('대상은 원본 밖의 새 디렉터리여야 합니다. 기존 데이터는 덮어쓰지 않습니다.')
    check_database(source / 'state.sqlite3')
    files = inventory(source)
    manifest = source / 'backup-manifest.json'
    if restore:
        if json.loads(manifest.read_text()) != {'version': 1, 'sha256': files}:
            raise ValueError('백업 파일 목록 또는 SHA-256이 일치하지 않습니다.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.taesan-transfer-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'data'
        shutil.copytree(source, stage)
        stage.chmod(0o700)
        if not restore:
            # sqlite.backup includes committed WAL data. The stopped service makes
            # the DB and invitation/artwork files one consistent offline snapshot.
            for suffix in ('', '-wal', '-shm', '-journal'):
                (stage / ('state.sqlite3' + suffix)).unlink(missing_ok=True)
            with closing(sqlite3.connect((source / 'state.sqlite3').as_uri() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(stage / 'state.sqlite3')) as target:
                    src.backup(target)
            (stage / 'backup-manifest.json').write_text(json.dumps(
                {'version': 1, 'sha256': inventory(stage)}, ensure_ascii=False, indent=2))
        check_database(stage / 'state.sqlite3')
        stage.rename(destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'backup', 'restore'))
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--service-stopped', action='store_true',
                        help='운영 서비스를 중지하고 모든 쓰기를 멈춘 뒤에만 지정')
    args = parser.parse_args()
    try:
        if args.action == 'inspect':
            print(json.dumps(inspect(args.source), ensure_ascii=False, indent=2))
        else:
            if not args.service_stopped or not args.destination:
                parser.error('backup/restore에는 --service-stopped 및 --destination이 필요합니다.')
            print(transfer(args.source, args.destination, restore=args.action == 'restore'))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error) + '\n')
