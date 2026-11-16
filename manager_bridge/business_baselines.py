"""Owner-approved business requirements, immutable proposal/task baseline pins.

This is a requirements handoff boundary, not a test engine or a quality score.
Reference metadata never attests that a worker has viewed an image or document.
"""
import json
import re
import secrets
from .maintenance_registry import name
from .store import Conflict,NotFound,PermissionDenied,canonical_json,digest
from .three_tier import ThreeTier

CATEGORIES=frozenset({'design','copy','price_behavior','completion','integration'})
PIN_FIELDS=('version','digest','revision','config_fingerprint','qa_policy_digest')


def _table(c,name):return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


def _text(value,limit=2000):
    if type(value) is not str or not value.strip() or len(value)>limit:raise ValueError('bounded explicit business requirement required')
    return value.strip()


def _contract(value,scenarios):
    if type(value) is not dict or set(value)!={'version','requirements','references','preserve'}:raise ValueError('exact business baseline contract required')
    name(value['version'])
    requirements=value['requirements'];references=value['references'];preserve=value['preserve']
    if type(requirements) is not list or not 1<=len(requirements)<=40:raise ValueError('one to forty explicit requirements required')
    ids=set();rows=[]
    for row in requirements:
        if type(row) is not dict or set(row)!={'id','category','priority','criterion','scenario_ids'}:raise ValueError('exact requirement and QA mapping required')
        name(row['id'])
        if row['id'] in ids or row['category'] not in CATEGORIES or row['priority'] not in ('P0','P1','P2','P3'):raise ValueError('unique categorized prioritized requirements required')
        ids.add(row['id']);_text(row['criterion'])
        mapped=row['scenario_ids']
        if type(mapped) is not list or not 1<=len(mapped)<=20 or any(type(x) is not str or x not in scenarios for x in mapped) or len(set(mapped))!=len(mapped):raise ValueError('exact independently stored QA scenario mapping required')
        rows.append({**row,'criterion':row['criterion'].strip(),'scenario_ids':sorted(mapped)})
    if type(references) is not list or len(references)>10:raise ValueError('bounded approved reference metadata required')
    refs=[];seen=set()
    for row in references:
        if type(row) is not dict or set(row)!={'reference','sha256','description'}:raise ValueError('exact retained reference identity required')
        name(row['reference']);_text(row['description'],500)
        if row['reference'] in seen or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',row['sha256']):raise ValueError('unique retained reference hashes required')
        seen.add(row['reference']);refs.append(dict(row))
    if type(preserve) is not list or len(preserve)>20:raise ValueError('bounded preserve constraints required')
    for item in preserve:_text(item,1000)
    result={'version':value['version'],'requirements':sorted(rows,key=lambda r:(r['priority'],r['id'])),
            'references':sorted(refs,key=lambda r:r['reference']),'preserve':list(preserve)}
    if len(canonical_json(result).encode())>12000:raise ValueError('business baseline exceeds native handoff requirement budget')
    return result


def qa_projection(contract,policy):
    full=policy['expected_contract'];selected=sorted({sid for row in contract['requirements'] for sid in row['scenario_ids']})
    rows={sid:full['scenarios'][sid] for sid in selected}
    adapters={r['adapter'] for r in rows.values() if 'adapter' in r}
    return {'schema_version':full['schema_version'],'adapter_versions':{a:full['adapter_versions'][a] for a in sorted(adapters)},'scenarios':rows}


def _prompt_budget(contract,policy):
    if len(canonical_json({'contract':contract,'expected_contract':qa_projection(contract,policy),'required_coverage':policy['required_coverage']}).encode())>16000:
        raise ValueError('business baseline exceeds native handoff context budget')


def _qa(c,project,config):
    row=c.execute('SELECT * FROM governance_qa_policies WHERE project=?',(project,)).fetchone() if _table(c,'governance_qa_policies') else None
    if row is None:raise Conflict('registered external QA contract required for business baseline')
    body=json.loads(row['policy'])
    if body.get('config_fingerprint')!=config or digest(body)!=row['policy_digest']:raise Conflict('baseline QA configuration changed')
    return dict(row),body


def _references(c,store,project,config,contract):
    if any(r['category']=='design' for r in contract['requirements']) and not contract['references']:
        raise Conflict('design requirements need retained reference identities')
    for ref in contract['references']:
        row=c.execute('SELECT * FROM artifact_content_records WHERE reference=?',(ref['reference'],)).fetchone() if _table(c,'artifact_content_records') else None
        if (row is None or row['project']!=project or row['config']!=config or row['sha256']!=ref['sha256']
                or not 0<row['observed_at']<=store._now()<row['expires']):
            raise Conflict('business reference missing, expired or configuration/hash changed')


def _owner_live(c,store,owner,project):
    row=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(owner,)).fetchone()
    if not row or row['role']!='owner' or row['revoked'] is not None or (row['expires'] is not None and row['expires']<=store._now()) or not store._scope({'role':row['role'],'projects':json.loads(row['projects'])},project):
        raise Conflict('business baseline owner authority unavailable')
    session=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(owner,)).fetchone()
    if session:
        parent=c.execute('SELECT revoked,expires FROM bridge_tokens WHERE token_hash=?',(session['parent_hash'],)).fetchone()
        if parent is None or parent['revoked'] is not None or (parent['expires'] is not None and parent['expires']<=store._now()):raise Conflict('business baseline owner session unavailable')


def snapshot_for_proposal(c,store,project):
    if not _table(c,'business_baseline_active'):return None
    row=c.execute('SELECT * FROM business_baseline_active WHERE project=?',(project,)).fetchone()
    if row is None:return None
    config=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(project,)).fetchone()
    if config is None or config['fingerprint']!=row['config_fingerprint']:raise Conflict('business baseline configuration changed')
    qa,policy=_qa(c,project,row['config_fingerprint']);_owner_live(c,store,row['owner_hash'],project)
    contract=json.loads(row['contract'])
    if _contract(contract,policy['expected_contract']['scenarios'])!=contract:raise Conflict('canonical business baseline required')
    _prompt_budget(contract,policy)
    if row['qa_policy_digest']!=qa['policy_digest']:raise Conflict('business baseline QA policy changed')
    _references(c,store,project,row['config_fingerprint'],contract)
    expected=digest({'project':project,'revision':row['revision'],'config_fingerprint':row['config_fingerprint'],
        'qa_policy_digest':row['qa_policy_digest'],'contract':contract})
    if expected!=row['digest']:raise Conflict('business baseline integrity mismatch')
    return {'project':project,'version':contract['version'],'digest':row['digest'],'revision':row['revision'],
        'config_fingerprint':row['config_fingerprint'],'qa_policy_digest':row['qa_policy_digest'],'contract':contract,
        'state':'approved','reference_materialized':False,'business_acceptance_verified':False,
        'execution_authorized':False,'operational_authorized':False,'quality_equivalence_guaranteed':False}


def pin(snapshot):return {k:snapshot[k] for k in PIN_FIELDS} if snapshot is not None else None


def validate_task(c,store,task):
    manifest=json.loads(task['manifest']) if isinstance(task['manifest'],str) else task['manifest']
    if manifest['kind']!='development':return None
    if c is None:raise Conflict('development execution requires a transactional fence')
    snapshot=snapshot_for_proposal(c,store,task['project'])
    approval=json.loads(task['approval'] or '{}') if isinstance(task['approval'],str) else (task['approval'] or {})
    if approval.get('business_baseline')!=pin(snapshot):raise Conflict('exact owner-approved business baseline task pin required')
    return snapshot


class BusinessBaselines:
    def __init__(self,core):
        if not isinstance(core,ThreeTier):raise ValueError('registered core required')
        self.core,self.store,self.registry=core,core.store,core.registry
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS business_baseline_drafts (id TEXT PRIMARY KEY,project TEXT,actor TEXT,request_key TEXT,body TEXT,digest TEXT,UNIQUE(actor,request_key))')
            c.execute('CREATE TABLE IF NOT EXISTS business_baseline_active (project TEXT PRIMARY KEY,revision INTEGER,config_fingerprint TEXT,qa_policy_digest TEXT,contract TEXT,digest TEXT,owner_hash TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS business_baseline_history (project TEXT,version TEXT,snapshot TEXT,PRIMARY KEY(project,version))')

    def _scope(self,c,token,project,owner=False):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner',) if owner else ('owner','manager'))
        config=self.registry._get(c,token,project)
        return actor,config

    def draft(self,token,project,request_key,contract):
        name(request_key)
        with self.store._transaction() as c:
            actor,config=self._scope(c,token,project);qa,policy=_qa(c,project,config['fingerprint'])
            contract=_contract(contract,policy['expected_contract']['scenarios']);_prompt_budget(contract,policy)
            body={'project':project,'config_fingerprint':config['fingerprint'],'qa_policy_digest':qa['policy_digest'],'contract':contract}
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,{'request_key':request_key,'body':body})
            from .artifact_content import ArtifactContent
            def protect(value):
                if type(value) is str:ArtifactContent._scan(c,token,value.encode())
                elif type(value) is dict:
                    for key,item in value.items():protect(key);protect(item)
                elif type(value) is list:
                    for item in value:protect(item)
            protect(body)
            old=c.execute('SELECT * FROM business_baseline_drafts WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if json.loads(old['body'])!=body:raise Conflict('business baseline draft request changed')
                return self._draft(old)
            key=secrets.token_hex(16)
            c.execute('INSERT INTO business_baseline_drafts VALUES (?,?,?,?,?,?)',(key,project,actor['token_hash'],request_key,canonical_json(body),digest(body)))
            return self._draft(c.execute('SELECT * FROM business_baseline_drafts WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _draft(row):return {'id':row['id'],'project':row['project'],'digest':row['digest'],'body':json.loads(row['body']),
        'state':'draft','owner_approved':False,'execution_authorized':False}

    def approve(self,token,draft_id,draft_digest,*,expected_revision=0):
        name(draft_id)
        if type(expected_revision) is not int or expected_revision<0:raise ValueError('exact prior baseline revision required')
        with self.store._transaction() as c:
            draft=c.execute('SELECT * FROM business_baseline_drafts WHERE id=?',(draft_id,)).fetchone()
            if draft is None:raise NotFound('business baseline draft unavailable')
            actor,config=self._scope(c,token,draft['project'],True)
            if draft['digest']!=draft_digest or digest(json.loads(draft['body']))!=draft_digest:raise Conflict('exact business baseline draft digest required')
            body=json.loads(draft['body']);qa,policy=_qa(c,draft['project'],config['fingerprint'])
            if body['config_fingerprint']!=config['fingerprint'] or body['qa_policy_digest']!=qa['policy_digest']:raise Conflict('business baseline draft binding changed')
            contract=_contract(body['contract'],policy['expected_contract']['scenarios']);_prompt_budget(contract,policy);_references(c,self.store,draft['project'],config['fingerprint'],contract)
            old=c.execute('SELECT * FROM business_baseline_active WHERE project=?',(draft['project'],)).fetchone()
            if expected_revision!=(old['revision'] if old else 0):raise Conflict('business baseline revision changed')
            if c.execute("SELECT 1 FROM bridge_tasks WHERE project=? AND state IN ('pending_approval','queued','running','reported','needs_verification') LIMIT 1",(draft['project'],)).fetchone():raise Conflict('active work prevents changing approved business baseline')
            if c.execute('SELECT 1 FROM business_baseline_history WHERE project=? AND version=?',(draft['project'],contract['version'])).fetchone():raise Conflict('business baseline version is immutable')
            revision=expected_revision+1
            stamp=digest({'project':draft['project'],'revision':revision,'config_fingerprint':config['fingerprint'],'qa_policy_digest':qa['policy_digest'],'contract':contract})
            c.execute('INSERT OR REPLACE INTO business_baseline_active VALUES (?,?,?,?,?,?,?)',(draft['project'],revision,config['fingerprint'],qa['policy_digest'],canonical_json(contract),stamp,actor['token_hash']))
            snapshot=snapshot_for_proposal(c,self.store,draft['project'])
            c.execute('INSERT INTO business_baseline_history VALUES (?,?,?)',(draft['project'],contract['version'],canonical_json(snapshot)))
            return snapshot

    def _current(self,c,token,project):
        self._scope(c,token,project)
        try:value=snapshot_for_proposal(c,self.store,project)
        except Conflict:
            row=c.execute('SELECT * FROM business_baseline_active WHERE project=?',(project,)).fetchone()
            return {'project':project,'state':'stale','version':json.loads(row['contract'])['version'] if row else None,
                'digest':row['digest'] if row else None,'revision':row['revision'] if row else 0,'contract':None,
                'execution_authorized':False,'business_acceptance_verified':False}
        return value or {'project':project,'state':'not_configured','version':None,'digest':None,'revision':0,'contract':None,
            'execution_authorized':False,'business_acceptance_verified':False}

    def current(self,token,project):
        with self.store._transaction() as c:return self._current(c,token,project)


def validate_baseline_qa(c,store,task,report):
    snapshot=validate_task(c,store,task)
    if snapshot is None:return None
    rows={r.get('id'):r for r in report['scenarios'] if type(r) is dict}
    results=[]
    for requirement in snapshot['contract']['requirements']:
        passed=all(sid in rows and rows[sid].get('status')=='pass' and rows[sid].get('executed') is True
                   and rows[sid].get('asserted') is True and bool(rows[sid].get('evidence')) for sid in requirement['scenario_ids'])
        if requirement['priority'] in ('P0','P1') and not passed:raise Conflict('critical business baseline requirement lacks mapped QA evidence')
        results.append({'id':requirement['id'],'priority':requirement['priority'],'mapped_scenarios_observed_pass':passed,
                        'business_acceptance_verified':False})
    return {'baseline_digest':snapshot['digest'],'requirements':results,'reference_materialized':False,
            'actual_project_verified':False,'business_acceptance_verified':False}
