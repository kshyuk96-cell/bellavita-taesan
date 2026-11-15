"""Owner-approved, bounded delegation of isolated development/test approval.

No owner credential is copied to a manager/worker. Delegated approval is explicit
provenance in the existing task record, never an operational release permission.
Source observations come only from registered host callbacks, not request text.
"""
from dataclasses import asdict, dataclass
import json
import math
import re
import secrets
from .coordination import scope_violations, validate_paths
from .maintenance_registry import name, receipt
from .store import (Conflict, NotFound, PermissionDenied, _project, _sha, _task_id,
                    _reject_credentials, canonical_json, digest)

REQUIRED_CHECKS = frozenset({'authorization_regression', 'approval_regression'})
MAX_GRANT_SECONDS = 7 * 86400
SOURCE_MAX_AGE = 300


def _hash(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):
        raise ValueError('exact fingerprint required')
    return value


def _live_identity(c,store,hashed,role,project):
    row=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(hashed,)).fetchone()
    now=store._now()
    if (not row or row['role']!=role or row['revoked'] is not None or
            (row['expires'] is not None and row['expires']<=now)):
        raise PermissionDenied('delegation identity expired or revoked')
    identity={'role':role,'projects':json.loads(row['projects'])}
    if not store._scope(identity,project):raise PermissionDenied('delegation identity outside project')
    web=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(hashed,)).fetchone()
    if web:
        parent=c.execute('SELECT revoked,expires FROM bridge_tokens WHERE token_hash=?',(web['parent_hash'],)).fetchone()
        if not parent or parent['revoked'] is not None or (parent['expires'] is not None and parent['expires']<=now):
            raise PermissionDenied('delegation session parent expired or revoked')
    return row


@dataclass(frozen=True)
class SourceProof:
    project:str
    source_ref:str
    branch:str
    sha:str
    reference:str
    observed_at:float

    def __post_init__(self):
        _project(self.project);_sha(self.sha);receipt(self.source_ref);receipt(self.branch);receipt(self.reference)
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('source observation time required')


def _grant(c,store,key):
    _task_id(key)
    row=c.execute('SELECT * FROM management_delegations WHERE id=?',(key,)).fetchone()
    if row is None:raise NotFound('delegation not found')
    policy=json.loads(row['policy'])
    if digest(policy)!=row['fingerprint']:raise Conflict('delegation policy changed')
    return row,policy


def _current_grant(c,store,row,policy):
    if policy.get('project')!=row['project'] or policy.get('manager_hash')!=row['manager_hash']:
        raise Conflict('delegation identity policy changed')
    if row['state']!='active' or row['approval']!=row['fingerprint']:
        raise PermissionDenied('delegation not active')
    if policy['expires_at']<=store._now():raise PermissionDenied('delegation expired')
    _live_identity(c,store,row['owner_hash'],'owner',row['project'])
    _live_identity(c,store,row['manager_hash'],'manager',row['project'])
    config=c.execute('SELECT config,fingerprint FROM maintenance_projects WHERE project=?',(row['project'],)).fetchone()
    if not config or config['fingerprint']!=policy['config_fingerprint']:
        raise Conflict('delegated project configuration changed')
    if not json.loads(config['config'])['maintenance_enabled']:
        raise PermissionDenied('project execution remains disabled')
    return config


def validate_execution(c,store,row,*,check_source_age=True):
    """DB-only execution fence. Owner-only approvals retain their existing behavior.

    Invoke for each claim and before/after privileged worker work. Already running
    work may omit source age, but never grant expiry/revocation/configuration.
    """
    approval=json.loads(row['approval']) if row['approval'] else {}
    key=approval.get('delegation_id')
    if key is None:return True
    grant,policy=_grant(c,store,key)
    _current_grant(c,store,grant,policy)
    if (approval.get('delegation_digest')!=grant['fingerprint'] or
            approval.get('manager_hash')!=grant['manager_hash'] or row['project']!=grant['project'] or
            approval.get('version')!=row['version'] or approval.get('manifest_digest')!=row['manifest_digest']):
        raise Conflict('delegated task approval binding changed')
    usage=c.execute('SELECT * FROM management_delegation_usage WHERE task=?',(row['id'],)).fetchone()
    if not usage or usage['grant_id']!=key or usage['manifest_digest']!=row['manifest_digest']:
        raise Conflict('delegation receipt missing')
    count=c.execute('SELECT COUNT(*) FROM management_delegation_usage WHERE grant_id=?',(key,)).fetchone()[0]
    if not 1<=grant['used']<=policy['max_tasks'] or count!=grant['used']:
        raise Conflict('delegation budget receipt inconsistent')
    manifest=json.loads(row['manifest'])
    if manifest['kind'] not in policy['kinds'] or manifest['repository']!=policy['repository']:
        raise Conflict('delegated task scope changed')
    if not REQUIRED_CHECKS<=set(manifest['checks']) or not set(manifest['checks'])<=set(policy['allowed_checks']):
        raise Conflict('delegated checks changed')
    bound=c.execute('SELECT policy,policy_digest FROM bridge_task_policy WHERE task=?',(row['id'],)).fetchone()
    if not bound or approval.get('policy_digest')!=bound['policy_digest']:
        raise Conflict('delegated path policy missing')
    paths=json.loads(bound['policy'])
    if (digest(paths)!=bound['policy_digest'] or scope_violations(paths['allowed_paths'],policy['allowed_paths']) or
            paths.get('version')!=row['version'] or paths.get('manifest_digest')!=row['manifest_digest'] or
            paths.get('routing_required') is not True):
        raise Conflict('delegated file policy changed')
    proof=approval.get('source_proof')
    if type(proof) is not dict or proof.get('project')!=row['project'] or proof.get('source_ref')!=policy['source_ref'] or proof.get('branch')!=policy['branch'] or proof.get('sha')!=manifest['base_sha']:
        raise Conflict('delegated source proof missing')
    observed=proof.get('observed_at')
    if type(observed) not in (int,float) or not math.isfinite(observed) or not 0<=store._now()-observed:
        raise Conflict('delegated source clock invalid')
    if check_source_age and store._now()-observed>SOURCE_MAX_AGE:
        raise Conflict('delegated source observation stale')
    return True


class Delegation:
    def __init__(self,store,registry,*,source_observers=None):
        if registry.store is not store:raise ValueError('shared store required')
        self.store,self.registry=store,registry
        self.source_observers=dict(source_observers or {})
        for project,callback in self.source_observers.items():
            _project(project)
            if not callable(callback):raise ValueError('trusted source observer required')
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS management_delegations (
              id TEXT PRIMARY KEY,project TEXT,owner_hash TEXT,manager_hash TEXT,
              policy TEXT,fingerprint TEXT,state TEXT,approval TEXT,used INTEGER,created REAL);
            CREATE TABLE IF NOT EXISTS management_delegation_usage (
              task TEXT PRIMARY KEY,grant_id TEXT,manifest_digest TEXT,approved REAL);
            ''')

    def _public(self,row):
        return {'id':row['id'],'project':row['project'],'policy':json.loads(row['policy']),
                'fingerprint':row['fingerprint'],'state':row['state'],'used':row['used'],
                'operational_approval':False}

    def get(self,token,key):
        with self.store._transaction() as c:
            row,policy=_grant(c,self.store,key)
            self.registry._get(c,token,row['project'])
            return self._public(row)

    def propose(self,token,project,manager_hash,*,allowed_paths,allowed_checks,kinds,max_tasks,expires_at):
        _project(project);_hash(manager_hash)
        paths=validate_paths(allowed_paths)
        if type(allowed_checks) not in (list,tuple) or not 2<=len(allowed_checks)<=30 or len(set(allowed_checks))!=len(allowed_checks):
            raise ValueError('bounded explicit checks required')
        for check in allowed_checks:name(check)
        if not REQUIRED_CHECKS<=set(allowed_checks):raise ValueError('authorization and approval regressions required')
        if type(kinds) not in (list,tuple) or not kinds or len(set(kinds))!=len(kinds) or not set(kinds)<={'development','test'}:
            raise ValueError('development/test delegation only')
        if type(max_tasks) is not int or not 1<=max_tasks<=100:raise ValueError('bounded task budget required')
        now=self.store._now()
        if type(expires_at) not in (int,float) or not math.isfinite(expires_at) or not now<expires_at<=now+MAX_GRANT_SECONDS:
            raise ValueError('bounded future delegation expiry required')
        with self.store._transaction() as c:
            config=self.registry._get(c,token,project,('owner',))
            owner=self.store._authenticate(c,token)
            manager=_live_identity(c,self.store,manager_hash,'manager',project)
            repository=self.store._project_access(c,owner,project)
            policy={'project':project,'company':config['config']['company'],'repository':repository,
                    'source_ref':config['config']['source_ref'],'branch':config['config']['branch'],
                    'config_fingerprint':config['fingerprint'],'manager_hash':manager_hash,
                    'allowed_paths':paths,'allowed_checks':sorted(allowed_checks),'kinds':sorted(kinds),
                    'max_tasks':max_tasks,'expires_at':expires_at,'operational_actions_allowed':False}
            _reject_credentials(policy,token)
            key=secrets.token_hex(16)
            c.execute('INSERT INTO management_delegations VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (key,project,owner['token_hash'],manager_hash,canonical_json(policy),digest(policy),'pending_approval',None,0,now))
            return self._public(c.execute('SELECT * FROM management_delegations WHERE id=?',(key,)).fetchone())

    def activate(self,token,key,fingerprint):
        _hash(fingerprint)
        with self.store._transaction() as c:
            row,policy=_grant(c,self.store,key)
            config=self.registry._get(c,token,row['project'],('owner',))
            actor=self.store._authenticate(c,token)
            if actor['token_hash']!=row['owner_hash']:raise PermissionDenied('original granting owner required')
            if row['fingerprint']!=fingerprint:raise Conflict('exact delegation approval required')
            if row['state'] not in ('pending_approval','active'):raise Conflict('revoked delegation cannot reactivate')
            if config['fingerprint']!=policy['config_fingerprint']:raise Conflict('project configuration changed')
            if policy['expires_at']<=self.store._now():raise Conflict('delegation expired before activation')
            _live_identity(c,self.store,row['manager_hash'],'manager',row['project'])
            c.execute("UPDATE management_delegations SET state='active',approval=? WHERE id=?",(fingerprint,key))
            return self._public(c.execute('SELECT * FROM management_delegations WHERE id=?',(key,)).fetchone())

    def authorize(self,token,grant_id,task_id,version,manifest_digest,*,requested_paths,dependencies=()):
        _task_id(task_id);_hash(manifest_digest)
        paths=validate_paths(requested_paths)
        if type(dependencies) not in (list,tuple) or len(dependencies)>100 or len(set(dependencies))!=len(dependencies):
            raise ValueError('bounded dependency list required')
        if type(version) is not int:raise ValueError('exact task version required')
        with self.store._transaction() as c:
            grant,policy=_grant(c,self.store,grant_id)
            actor=self.store._authenticate(c,token);self.store._role(actor,('manager',))
            self.store._project_access(c,actor,grant['project'])
            if actor['token_hash']!=grant['manager_hash']:raise PermissionDenied('delegation belongs to another manager')
            _current_grant(c,self.store,grant,policy)
            task=self.store._get(c,actor,task_id)
            self._task_scope(task,grant,policy,version,manifest_digest,paths)
            config_fingerprint=policy['config_fingerprint']
        callback=self.source_observers.get(grant['project'])
        if callback is None:raise Conflict('trusted source observation not connected')
        try:observed=callback(grant['project'])
        except Exception:raise Conflict('trusted source observation unavailable') from None
        if not isinstance(observed,SourceProof):raise Conflict('invalid source observation')
        if (observed.project,observed.source_ref,observed.branch,observed.sha)!=(grant['project'],policy['source_ref'],policy['branch'],json.loads(task['manifest'])['base_sha']):
            raise Conflict('current branch source does not match task')
        if not 0<=self.store._now()-observed.observed_at<=SOURCE_MAX_AGE:raise Conflict('source observation stale')
        _reject_credentials(asdict(observed),token)
        with self.store._transaction() as c:
            grant,policy=_grant(c,self.store,grant_id)
            actor=self.store._authenticate(c,token);self.store._role(actor,('manager',))
            if actor['token_hash']!=grant['manager_hash']:raise PermissionDenied('delegation manager changed')
            self.store._project_access(c,actor,grant['project']);_current_grant(c,self.store,grant,policy)
            if policy['config_fingerprint']!=config_fingerprint:raise Conflict('delegation changed during observation')
            task=self.store._get(c,actor,task_id)
            self._task_scope(task,grant,policy,version,manifest_digest,paths)
            prior=c.execute('SELECT * FROM management_delegation_usage WHERE task=?',(task_id,)).fetchone()
            bindings=[]
            for dependency in dependencies:
                parent=self.store._get(c,actor,dependency)
                if parent['project']!=task['project'] or parent['id']==task_id or not parent['approval'] or parent['state'] in ('cancelled','needs_verification'):
                    raise Conflict('valid same-project approved dependency required')
                bindings.append({'id':dependency,'version':parent['version'],'manifest_digest':parent['manifest_digest']})
            task_policy={'version':version,'manifest_digest':manifest_digest,'allowed_paths':paths,'dependencies':bindings,'routing_required':True}
            policy_digest=digest(task_policy)
            if prior:
                if prior['grant_id']!=grant_id or prior['manifest_digest']!=manifest_digest:raise Conflict('task belongs to another delegation')
                current=c.execute('SELECT policy_digest FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
                if not current or current['policy_digest']!=policy_digest:raise Conflict('cannot alter approved delegated paths/dependencies')
                if task['state'] not in ('queued',):
                    validate_execution(c,self.store,task,check_source_age=False)
                    return self.store._public(task)
            else:
                if task['state']!='pending_approval':raise Conflict('only pending tasks can receive delegated approval')
                if grant['used']>=policy['max_tasks']:raise PermissionDenied('delegation task budget exhausted')
                existing=c.execute('SELECT policy,policy_digest FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
                if existing and (existing['policy_digest']!=policy_digest or digest(json.loads(existing['policy']))!=policy_digest):
                    raise Conflict('existing owner path/dependency policy cannot be replaced')
                c.execute('INSERT INTO management_delegation_usage VALUES (?,?,?,?)',(task_id,grant_id,manifest_digest,self.store._now()))
                c.execute('UPDATE management_delegations SET used=used+1 WHERE id=?',(grant_id,))
                c.execute('INSERT OR IGNORE INTO bridge_task_policy VALUES (?,?,?)',(task_id,canonical_json(task_policy),policy_digest))
            approval={'actor':actor['subject'],'version':version,'manifest_digest':manifest_digest,
                      'policy_digest':policy_digest,'approved_at':self.store._now(),
                      'delegation_id':grant_id,'delegation_digest':grant['fingerprint'],
                      'manager_hash':actor['token_hash'],'source_proof':asdict(observed)}
            c.execute("UPDATE bridge_tasks SET state='queued',approval=?,updated=? WHERE id=?",(canonical_json(approval),self.store._now(),task_id))
            self.store._event(c,task_id,actor['subject'],'delegated_development_approved','queued',approval)
            return self.store._public(self.store._get(c,actor,task_id))

    @staticmethod
    def _task_scope(task,grant,policy,version,manifest_digest,paths):
        if task['project']!=grant['project'] or task['version']!=version or task['manifest_digest']!=manifest_digest:
            raise Conflict('exact task binding required')
        manifest=json.loads(task['manifest'])
        if manifest['repository']!=policy['repository'] or manifest['kind'] not in policy['kinds']:
            raise PermissionDenied('task outside delegated repository or kind')
        if not REQUIRED_CHECKS<=set(manifest['checks']) or not set(manifest['checks'])<=set(policy['allowed_checks']):
            raise PermissionDenied('task checks outside delegated policy')
        if scope_violations(paths,policy['allowed_paths']):raise PermissionDenied('task files outside delegated paths')

    def revoke(self,token,key):
        with self.store._transaction() as c:
            grant,policy=_grant(c,self.store,key)
            self.registry._get(c,token,grant['project'],('owner',))
            c.execute("UPDATE management_delegations SET state='revoked' WHERE id=?",(key,))
            rows=c.execute('SELECT t.* FROM bridge_tasks t JOIN management_delegation_usage u ON u.task=t.id WHERE u.grant_id=?',(key,)).fetchall()
            stopped=uncertain=0
            for task in rows:
                if task['state']=='queued':
                    c.execute("UPDATE bridge_tasks SET state='cancelled',cancel_reason='Delegation revoked',updated=? WHERE id=?",(self.store._now(),task['id']));stopped+=1
                elif task['state'] in ('running','reported'):
                    c.execute("UPDATE bridge_tasks SET state='needs_verification',updated=? WHERE id=?",(self.store._now(),task['id']));uncertain+=1
            return {'id':key,'state':'revoked','queued_cancelled':stopped,'needs_verification':uncertain,
                    'execution_stop_confirmed':False,'operational_change':False}
