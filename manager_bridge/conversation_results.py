"""Private, expiring read snapshots. No provider calls or write capabilities.

The caller derives scope only after current customer authorization. XLSX retrieval
requires a second current authorization callback, including after generation.
Differences describe observed result windows, never assumed source deletions.
"""
from copy import deepcopy
from io import BytesIO
import json
import math
import secrets
from .store import Conflict, NotFound, PermissionDenied, canonical_json, digest

FIELDS = ('id','date','status','state','stage','revision','stableUid','sourceRow','time',
          'recipient','sender','address','driver','amount','paid','title','location',
          'category','price','updated','productId','departureDate','travelerCount',
          'quote','assignee','due','version','roomName','departureAt')


def count_summary(result):
    if type(result) is not dict:raise ValueError('typed result required')
    items=result.get('items')
    if type(items) is not list or len(items)>100:raise ValueError('bounded result items required')
    count=len(items)
    # Upstream adapter totals describe fetched records. Only an explicit trusted
    # coverage receipt allows presentation as the total source matching count.
    coverage=result.get('coverage')
    total=result.get('total_matched')
    complete=(type(coverage) is dict and coverage.get('verified') is True and
              coverage.get('complete') is True and type(coverage.get('receipt')) is str and
              bool(coverage['receipt']) and type(total) is int and total>=count)
    return {'displayed':count,'total':total if complete else None,
            'total_known':complete,'truncated':result.get('truncated') is True,
            'fetched_matched':total if type(total) is int and total>=count else None}


def _scope(scope):
    if type(scope) is not tuple or not 5<=len(scope)<=8 or any(type(x) is not str or not x or len(x)>256 for x in scope):
        raise ValueError('authenticated bounded scope required')
    return digest(scope)


def _snapshot(result):
    summary=count_summary(result)
    if result.get('provider_state',result.get('state','ok'))not in {'ok','empty'}:raise ValueError('successful read required')
    query=result.get('query',{})
    if type(query) is not dict or set(query)-{'id','date','status','limit'}:raise ValueError('validated query required')
    if type(result.get('action')) is not str or len(result['action'])>80:raise ValueError('read action required')
    items=[]
    for row in result['items']:
        if type(row) is not dict:raise ValueError('typed row required')
        item={}
        for field in FIELDS:
            value=row.get(field)
            if value is None:continue
            if type(value) is str and len(value)<=2000 and '\x00' not in value:item[field]=value
            elif type(value) in (bool,int):item[field]=value
            elif type(value) is float and math.isfinite(value):item[field]=value
            elif field in row:raise ValueError('bounded scalar result required')
        if not item.get('id'):raise ValueError('source identifier required')
        items.append(item)
    source=result.get('source',{})
    if type(source) is not dict:raise ValueError('source receipt required')
    payload={'action':result['action'],'query':deepcopy(query),'items':items,
             'counts':summary,'source':{key:source[key] for key in ('kind','ref','branch','contract_commit','reference','observed_at') if key in source}}
    if len(canonical_json(payload).encode())>256000:raise ValueError('snapshot too large')
    return payload


def _identity(row):
    return ('stable', str(row['stableUid'])) if row.get('stableUid') else ('id', str(row['id']))


class ResultHistory:
    def __init__(self,store,ttl=900):
        if type(ttl) is not int or not 1<=ttl<=1800:raise ValueError('bounded ttl required')
        self.store,self.ttl=store,ttl
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS manager_conversation_snapshots
                (id TEXT PRIMARY KEY, scope TEXT, payload TEXT, created REAL, expires REAL)''')

    def remember(self,scope,result):
        key=_scope(scope);payload=_snapshot(result);now=self.store.clock();identifier=secrets.token_hex(16)
        with self.store._transaction() as c:
            c.execute('DELETE FROM manager_conversation_snapshots WHERE expires<=?',(now,))
            c.execute('INSERT INTO manager_conversation_snapshots VALUES (?,?,?,?,?)',
                      (identifier,key,canonical_json(payload),now,now+self.ttl))
            # Keep two observations for comparison; insertion order breaks equal clocks.
            c.execute('DELETE FROM manager_conversation_snapshots WHERE scope=? AND id NOT IN '
                      '(SELECT id FROM manager_conversation_snapshots WHERE scope=? ORDER BY rowid DESC LIMIT 2)',(key,key))
        return identifier

    def _load(self,scope,identifier=None):
        key=_scope(scope)
        with self.store._transaction() as c:
            c.execute('DELETE FROM manager_conversation_snapshots WHERE expires<=?',(self.store.clock(),))
            if identifier is None:
                rows=c.execute('SELECT id,payload FROM manager_conversation_snapshots WHERE scope=? ORDER BY rowid DESC LIMIT 2',(key,)).fetchall()
            else:
                if type(identifier) is not str or len(identifier)!=32:raise NotFound('read snapshot unavailable')
                rows=c.execute('SELECT id,payload FROM manager_conversation_snapshots WHERE scope=? AND id=?',(key,identifier)).fetchall()
        if not rows:raise NotFound('read snapshot expired or unavailable')
        return [dict(id=row['id'],**json.loads(row['payload'])) for row in rows]

    def current(self,scope):return self._load(scope)[0]

    def clear(self,scope):
        with self.store._transaction() as c:c.execute('DELETE FROM manager_conversation_snapshots WHERE scope=?',(_scope(scope),))

    def compare(self,scope):
        rows=self._load(scope)
        if len(rows)<2:raise NotFound('two fresh read observations required')
        current,previous=rows
        if (current['action'],current['query'])!=(previous['action'],previous['query']):
            raise Conflict('same read conditions required for comparison')
        def indexed(snapshot):
            pairs=[(_identity(item),item) for item in snapshot['items']]
            if len(set(k for k,_ in pairs))!=len(pairs):raise Conflict('ambiguous source identities')
            return dict(pairs)
        old,new=indexed(previous),indexed(current)
        changed=[]
        for key in sorted(old.keys()&new.keys()):
            fields=[field for field in FIELDS if old[key].get(field)!=new[key].get(field)]
            if fields:changed.append({'id':new[key]['id'],'fields':fields})
        return {'previous_snapshot':previous['id'],'current_snapshot':current['id'],
                'appeared':[new[k]['id'] for k in sorted(new.keys()-old.keys())],
                'absent_from_current_display':[old[k]['id'] for k in sorted(old.keys()-new.keys())],
                'changed':changed,'scope':'observed_result_windows','source_deletions_inferred':False,
                'operational_change':False}

    def xlsx(self,scope,identifier,authorize):
        if not callable(authorize) or authorize() is not True:raise PermissionDenied('current read access required')
        snapshot=self._load(scope,identifier)[0]
        from openpyxl import Workbook
        workbook=Workbook();sheet=workbook.active;sheet.title='조회 결과'
        fields=[field for field in FIELDS if any(field in row for row in snapshot['items'])]
        if not fields:fields=['id']
        sheet.append(fields)
        for row in snapshot['items']:
            values=[]
            for field in fields:
                value=row.get(field,'')
                # Spreadsheet formula triggers can follow whitespace/control chars.
                if type(value) is str and value.lstrip(' \t\r\n').startswith(('=','+','-','@')):value="'"+value
                values.append(value)
            sheet.append(values)
        sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
        note=workbook.create_sheet('조회 범위')
        note.append(['표시 건수',snapshot['counts']['displayed']])
        note.append(['전체 건수',snapshot['counts']['total'] if snapshot['counts']['total_known'] else '확인되지 않음'])
        note.append(['범위','현재 조회 결과에 포함된 행만 내보냄'])
        output=BytesIO();workbook.save(output)
        if authorize() is not True:raise PermissionDenied('read access changed during export')
        # Expiry is checked after generation as well as before it.
        self._load(scope,identifier)
        return output.getvalue()
