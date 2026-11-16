"""Provision only a dedicated, empty nonproduction exchange for reviewed executors."""
from pathlib import Path
import json
import os
import sqlite3
from .project_runtime import _directory
from .store import Conflict,digest

SCHEMA='''
CREATE TABLE inventory_meta(id INTEGER PRIMARY KEY CHECK(id=1),contract_digest TEXT NOT NULL);
CREATE TABLE inventory_slots(slot TEXT PRIMARY KEY,capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 100),available INTEGER NOT NULL CHECK(available BETWEEN 0 AND capacity));
CREATE TABLE inventory_leases(key TEXT PRIMARY KEY,slot TEXT NOT NULL REFERENCES inventory_slots(slot),owner TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>=1),fingerprint TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100),released INTEGER NOT NULL DEFAULT 0 CHECK(released BETWEEN 0 AND quantity),status TEXT NOT NULL CHECK(status IN ('held','confirmed','uncertain','released')),booking TEXT,expires INTEGER NOT NULL);
CREATE TABLE inventory_releases(lease TEXT NOT NULL REFERENCES inventory_leases(key),unit INTEGER NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(lease,unit));
CREATE TABLE inventory_events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,lease TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE inventory_barrier(role TEXT PRIMARY KEY,ready INTEGER NOT NULL CHECK(ready=1));
CREATE TABLE inventory_gate(id INTEGER PRIMARY KEY CHECK(id=1),released INTEGER NOT NULL CHECK(released IN (0,1)));
INSERT INTO inventory_gate VALUES(1,0);
'''


def validate_contract(value,source):
    if type(value) is not dict or value.get('schema')!='hope-shared-fixture-inventory/v1' or value.get('source_revision')!=source:
        raise Conflict('inventory source/schema mismatch')
    if value.get('environment')!='isolated_nonproduction' or value.get('project')!='hope-tour' or value.get('operational_tables') is not False or value.get('supplier_connected') is not False:
        raise Conflict('dedicated nonproduction inventory required')
    if not value.get('owner') or value.get('adapter_version')!='shared-fixture-v1' or type(value.get('resource')) is not dict:
        raise Conflict('inventory ownership/resource contract missing')
    slots=value.get('slots')
    if type(slots) is not dict or not slots or len(slots)>30 or any(type(n) is not int or not 1<=n<=100 for n in slots.values()):raise Conflict('bounded stock contract required')
    return digest(value)


def provision_exchange(root,contract,source):
    root=_directory(root);pin=validate_contract(contract,source)
    if any(root.iterdir()):raise Conflict('fresh dedicated exchange required')
    path=root/'inventory.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA);db.execute('INSERT INTO inventory_meta VALUES(1,?)',(pin,))
        db.executemany('INSERT INTO inventory_slots VALUES(?,?,?)',[(k,v,v) for k,v in contract['slots'].items()])
    # Test-only data, two UID65534 containers. No credentials or business DB here.
    root.chmod(0o777);path.chmod(0o666)
    return pin


def check_exchange(root,pin):
    root=_directory(root);path=root/'inventory.sqlite'
    if not path.is_file() or path.is_symlink():raise Conflict('dedicated inventory database required')
    if any(p.name not in ('inventory.sqlite','inventory.sqlite-journal','inventory.sqlite-wal','inventory.sqlite-shm') or p.is_symlink() for p in root.iterdir()):raise Conflict('exchange contains unapproved files')
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT contract_digest FROM inventory_meta WHERE id=1').fetchone()
    if row!=(pin,):raise Conflict('inventory contract changed')
    return root


def independent_stock_check(snapshot,capacities=None):
    """Recompute free+active allocation=capacity, not a worker PASS/count claim."""
    issues=[];slots={r['slot']:r for r in snapshot['slots']};leases=snapshot['leases']
    if len(slots)!=len(snapshot['slots']):issues.append('duplicate_slot')
    if any(type(r[k]) is not int for r in snapshot['slots'] for k in ('capacity','available')):issues.append('invalid_stock_type')
    if capacities is not None and ({k:r['capacity'] for k,r in slots.items()}!=capacities):issues.append('trusted_capacity_mismatch')
    seen=set()
    for row in leases:
        if row['key'] in seen:issues.append('duplicate_lease')
        seen.add(row['key'])
        if type(row['quantity']) is not int or type(row['released']) is not int or not 0<=row['released']<=row['quantity']:issues.append('invalid_release_quantity')
        if row['status']=='released' and row['released']!=row['quantity']:issues.append('released_without_restoration')
        if row['slot'] not in slots:issues.append('orphan_slot')
    for key,slot in slots.items():
        used=sum(r['quantity']-r['released'] for r in leases if r['slot']==key and r['status']!='released')
        if type(slot['available']) is not int or not 0<=slot['available']<=slot['capacity'] or slot['available']+used!=slot['capacity']:issues.append('inventory_conservation:'+key)
    return sorted(set(issues))


def independent_domain_check(state):
    issues=[];keys=set()
    for journal in state['ledger']:
        if journal['sourceKey'] in keys:issues.append('duplicate_journal_source')
        keys.add(journal['sourceKey'])
        for book in ('DP','BP','SP','CP'):
            if sum(e['delta'] for e in journal['entries'] if e['book']==book)!=0:issues.append('unbalanced_journal')
    for booking in state['bookings']:
        refunds=[r for r in state['refunds'] if r['bookingId']==booking['id'] and r['status']=='refunded']
        if sum(r['quote']['refundTotal']+r['quote']['fee']-r['quote'].get('additionalDue',0) for r in refunds)>booking['total']:issues.append('excess_refund_total')
        for book in ('DP','BP','SP','CP','card'):
            if sum(r['quote']['refund'][book] for r in refunds)>booking['plan'][book]:issues.append('excess_refund_source')
            if booking.get('status')=='cancelled' and booking.get('pointsCommitted') is True and refunds and all(r['quote']['fee']==0 for r in refunds) and sum(r['quote']['refund'][book] for r in refunds)!=booking['plan'][book]:issues.append('full_refund_source_mismatch')
        if booking.get('status') in ('confirmed','cancelled','departed','completed'):
            paid=sum(i['amount'] for i in state['invoices'] if i.get('bookingId')==booking['id'] and i['kind']=='travel' and i['status']=='paid')
            if paid!=booking['plan']['card']:issues.append('paid_invoice_card_mismatch')
        for refund in refunds:
            if refund['quote']['refund']['card']>0:
                events=[e for e in state['travelEvents'] if e.get('refundId')==refund['id'] and e.get('type')=='refunded']
                if len(events)!=1 or events[0]['amount']!=refund['quote']['refund']['card']:issues.append('refund_card_event_mismatch')
            journal=next((j for j in state['ledger'] if j['sourceKey']=='refund:'+refund['id']),None)
            for book in ('DP','BP','SP','CP'):
                actual=sum(e['delta'] for e in (journal or {}).get('entries',[]) if e['account']==booking['contractId'] and e['book']==book)
                if actual!=refund['quote']['refund'][book]:issues.append('refund_journal_mismatch')
    return sorted(set(issues))
