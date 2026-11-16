"""Explicit dedicated business TEST database and stopped-executor certificates."""
import json
from pathlib import Path
import sqlite3
import subprocess
from .hope_service_runtime import _docker_env
from .project_runtime import _directory
from .store import Conflict,digest

REVIEW_SCHEMA='''
CREATE TABLE review_limits(id TEXT PRIMARY KEY,count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE review_sessions(token_hash TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,account TEXT NOT NULL,created TEXT NOT NULL,expires TEXT NOT NULL);
CREATE TABLE review_workspaces(id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL,created TEXT NOT NULL,expires TEXT NOT NULL);
CREATE TABLE qa_fixture_runs(run_id TEXT PRIMARY KEY,workspace TEXT UNIQUE,generation INTEGER,dataset TEXT,source TEXT);
CREATE TABLE qa_fixture_events(id INTEGER PRIMARY KEY,run_id TEXT,generation INTEGER,kind TEXT,receipt TEXT);
CREATE TABLE qa_fixture_cas(ok INTEGER NOT NULL CHECK(ok=1));
CREATE TABLE qa_fixture_snapshots(run_id TEXT,generation INTEGER,payload TEXT,PRIMARY KEY(run_id,generation));
CREATE TABLE qa_review_runtime(id INTEGER PRIMARY KEY CHECK(id=1),contract_digest TEXT,source TEXT,task TEXT,key_check TEXT);
'''
RECOVERY_SCHEMA='''
CREATE TABLE recovery_bindings(workspace TEXT PRIMARY KEY,label TEXT UNIQUE NOT NULL,task TEXT NOT NULL,source TEXT NOT NULL,contract_digest TEXT NOT NULL,generation INTEGER NOT NULL,run_id TEXT NOT NULL,slot TEXT NOT NULL,book_request TEXT NOT NULL,aux TEXT NOT NULL DEFAULT '{}');
CREATE TABLE recovery_executors(id TEXT PRIMARY KEY,task TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('running','stopped')),stop_receipt TEXT);
CREATE TABLE recovery_operations(key TEXT PRIMARY KEY,workspace TEXT NOT NULL,task TEXT NOT NULL,source TEXT NOT NULL,generation INTEGER NOT NULL,request_id TEXT NOT NULL,command TEXT NOT NULL,fingerprint TEXT NOT NULL,executor TEXT NOT NULL,phase TEXT NOT NULL,revision_before INTEGER NOT NULL,result TEXT);
CREATE TABLE recovery_barrier(role TEXT PRIMARY KEY,request_id TEXT NOT NULL,revision INTEGER NOT NULL,changes INTEGER);
CREATE TABLE recovery_gate(id INTEGER PRIMARY KEY CHECK(id=1),released INTEGER NOT NULL CHECK(released IN (0,1)));
INSERT INTO recovery_gate VALUES(1,0);
'''


def business_pin(contract,source):
    if (type(contract) is not dict or contract.get('schema')!='hope-persistent-review/v1' or contract.get('source_revision')!=source or
        contract.get('environment')!='isolated_nonproduction' or contract.get('project')!='hope-tour' or
        contract.get('task')!='hope-batch05-source-candidate' or contract.get('database')!='review.sqlite' or
        (type(contract.get('schema_version')) is not int or contract.get('schema_version')!=1) or contract.get('operational_tables') is not False or contract.get('real_pg') is not False):
        raise Conflict('pinned nonproduction business contract required')
    return digest(contract)


def provision_business(root,contract,source,exchange):
    root=_directory(root);pin=business_pin(contract,source)
    if any(root.iterdir()):raise Conflict('fresh business TEST directory required')
    path=root/'review.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(REVIEW_SCHEMA);db.execute('INSERT INTO qa_review_runtime(id,contract_digest,source,task) VALUES(1,?,?,?)',(pin,source,contract['task']))
    with sqlite3.connect(Path(exchange)/'inventory.sqlite') as db:db.executescript(RECOVERY_SCHEMA)
    root.chmod(0o777);path.chmod(0o666);return pin


def check_business(root,contract,source):
    root=_directory(root);pin=business_pin(contract,source);path=root/'review.sqlite'
    if not path.is_file() or path.is_symlink() or any(p.is_symlink() or p.name not in ('review.sqlite','review.sqlite-journal','review.sqlite-wal','review.sqlite-shm') for p in root.iterdir()):raise Conflict('unapproved business files')
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:row=db.execute('SELECT contract_digest,source,task FROM qa_review_runtime WHERE id=1').fetchone()
    if row!=(pin,source,contract['task']):raise Conflict('business source/contract/task changed')
    return root


def certify_stopped(exchange,receipt,contract):
    runtime=receipt['runtime_provenance'];name=runtime['container_name']
    if not runtime.get('process_exited') or receipt['source_revision']!=contract['source_revision']:raise Conflict('no terminal executor observation')
    listing=subprocess.run(['docker','ps','-a','--format','{{.Names}}'],env=_docker_env(),text=True,capture_output=True,timeout=10)
    if listing.returncode or name in listing.stdout.splitlines():raise Conflict('executor stop not verified')
    proof=dict(container=name,process_exited=True,container_absent=True,source_revision=contract['source_revision'],task=contract['task'])
    with sqlite3.connect(Path(exchange)/'inventory.sqlite') as db:
        result=db.execute("UPDATE recovery_executors SET status='stopped',stop_receipt=? WHERE id=? AND task=? AND source=?",(json.dumps(proof,sort_keys=True),name,contract['task'],contract['source_revision']))
        if result.rowcount!=1:raise Conflict('executor identity not registered')
    return proof
