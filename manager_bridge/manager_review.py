"""Manager-owned verification, change observations and atomic owner decision packets.

This is an application API, not a connector to other open Codex windows. All
observers/execution hosts are trusted local registrations. No merge, deployment,
payment, messaging, arbitrary command or owner credential forwarding is provided.
"""
from dataclasses import dataclass
import json
import math
import secrets
from .coordination import failure_plan, validate_paths
from .maintenance_registry import name, receipt
from .store import (Conflict, NotFound, PermissionDenied, _sha, _task_id,
                    _reject_credentials, canonical_json, digest)
from .supervisor import verify_report


@dataclass(frozen=True)
class VerificationHost:
    runner: object
    check_commands: dict
    native_handoff: object = None

    def __post_init__(self):
        if type(self.check_commands) is not dict or not self.check_commands:
            raise ValueError('registered independent checks required')
        copied={}
        from pathlib import Path
        for key,argv in self.check_commands.items():
            if type(key) is not str or not key.strip() or len(key)>200 or any(ord(x)<32 for x in key):raise ValueError('registered check label required')
            if type(argv) not in (list,tuple) or not argv or not Path(argv[0]).is_absolute() or any(
                    type(arg) is not str or not arg or '\x00' in arg for arg in argv):
                raise ValueError('fixed registered argv required')
            copied[key]=tuple(argv)
        object.__setattr__(self,'check_commands',copied)


@dataclass(frozen=True)
class ChangeObservation:
    project: str
    repository: str
    base_sha: str
    head_sha: str
    paths: tuple
    conflict: bool | None
    reference: str
    observed_at: float

    def __post_init__(self):
        name(self.project);receipt(self.repository);_sha(self.base_sha);_sha(self.head_sha)
        if type(self.paths) is not tuple or len(self.paths)>100:
            raise ValueError('bounded changed paths required')
        if self.paths:validate_paths(list(self.paths))
        if self.conflict is not None and type(self.conflict) is not bool:
            raise ValueError('strict observed conflict state required')
        receipt(self.reference)
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('observation time required')


@dataclass(frozen=True)
class ChangeProbe:
    observe: object
    read_only: bool = True

    def __post_init__(self):
        if not callable(self.observe) or self.read_only is not True:
            raise ValueError('registered read-only change observer required')


class ManagerReview:
    def __init__(self,store,registry,*,verification_hosts=None,change_observers=None,rehearsals=None):
        if registry.store is not store:raise ValueError('shared manager store required')
        self.store,self.registry=store,registry
        self.verification_hosts=dict(verification_hosts or {})
        self.change_observers=dict(change_observers or {})
        self.rehearsals=dict(rehearsals or {})
        for project,host in self.verification_hosts.items():
            name(project)
            if not isinstance(host,VerificationHost):raise ValueError('registered verification host required')
        for project,probe in self.change_observers.items():
            name(project)
            if not isinstance(probe,ChangeProbe):raise ValueError('registered change probe required')
        from .merge_rehearsal import MergeRehearsal
        for project,rehearsal in self.rehearsals.items():
            name(project)
            if not isinstance(rehearsal,MergeRehearsal):raise ValueError('registered local merge rehearsal required')

    def verify(self,token,task_id):
        task=self.store.get_task(token,task_id)
        config=self.registry.get(token,task['manifest']['project'])
        host=self.verification_hosts.get(task['manifest']['project'])
        if host is None:
            return {'task_id':task_id,'state':'not_measured','independently_verified':False,'operational_change':False}
        # supervisor reauthenticates and reads exact artifacts/HEAD; model/worker reports
        # cannot be promoted by merely calling this wrapper.
        verified=verify_report(self.store,token,task_id,host.runner,host.check_commands,native_handoff=host.native_handoff)
        current=self.registry.get(token,task['manifest']['project'])
        if current['fingerprint']!=config['fingerprint']:
            raise Conflict('project configuration changed during independent review')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            row=self.store._get(c,actor,task_id)
            proof=c.execute('SELECT * FROM bridge_verified_candidates WHERE task=?',(task_id,)).fetchone()
            if row['state']!='reviewed' or proof is None or proof['report_digest']!=digest(json.loads(row['report'])):
                raise Conflict('independent candidate evidence missing or changed')
        return {'task_id':task_id,'state':verified['state'],'head_sha':proof['head_sha'],
                'artifact_digest':proof['artifact_digest'],'candidate_clean':not bool(proof['dirty']),'independently_verified':True,
                'config_fingerprint':config['fingerprint'],'operational_change':False,'deployment_approved':False}

    def changes(self,token,projects,*,max_age=300):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or len(set(projects))!=len(projects):
            raise ValueError('explicit project review scope required')
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('observation age required')
        configs={project:self.registry.get(token,project) for project in projects}
        observations=[]
        for project in projects:
            current=self.registry.get(token,project)
            if current['fingerprint']!=configs[project]['fingerprint']:raise Conflict('project changed during collection')
            config=current['config'];probe=self.change_observers.get(project)
            result={'project':project,'state':'not_measured','conflict':None}
            if probe:
                try:
                    observed=probe.observe(project)
                    expected=config['source_ref'] if config['source_kind']=='github' else 'sites/'+config['source_ref']
                    if not isinstance(observed,ChangeObservation) or (observed.project,observed.repository)!=(project,expected):
                        raise ValueError('source scope mismatch')
                    if not 0<=self.store._now()-observed.observed_at<=max_age:raise ValueError('stale source')
                    _reject_credentials(observed.__dict__,token)
                    result.update(repository=observed.repository,base_sha=observed.base_sha,
                                  head_sha=observed.head_sha,paths=list(observed.paths),reference=observed.reference)
                    if observed.conflict is not None:
                        result.update(state='conflict' if observed.conflict else 'observed',conflict=observed.conflict)
                    local=self.rehearsals.get(project)
                    if local is not None:
                        rehearsal=local.run(project,expected,observed.base_sha,observed.head_sha)
                        if (rehearsal.get('base_sha'),rehearsal.get('candidate_sha'))!=(observed.base_sha,observed.head_sha) or rehearsal.get('worktree_index_refs_unchanged') is not True:
                            raise ValueError('local rehearsal provenance mismatch')
                        local_conflict=rehearsal.get('status')=='conflicts'
                        if rehearsal.get('status') not in ('clean_rehearsal','conflicts'):
                            raise ValueError('unknown local rehearsal')
                        if observed.conflict is not None and observed.conflict!=local_conflict:
                            result.update(state='contradiction',conflict=None)
                        else:result.update(state='conflict' if local_conflict else 'observed',conflict=local_conflict)
                        result['local_rehearsal']=True
                except Exception:
                    result={'project':project,'state':'not_measured','conflict':None}
            current=self.registry.get(token,project)
            if current['fingerprint']!=configs[project]['fingerprint']:raise Conflict('project changed during collection')
            observations.append(result)
        # Recheck the whole set; an observer may have revoked access to an earlier project.
        for project in projects:
            if self.registry.get(token,project)['fingerprint']!=configs[project]['fingerprint']:
                raise Conflict('project changed during review')
        return {'projects':observations,'all_observed':all(x['state']=='observed' for x in observations),
                'production_merge_performed':False,'deployment_approved':False,'operational_change':False,
                'cross_project_integration_verified':False}

    def blocker(self,token,task_id,code):
        task=self.store.get_task(token,task_id)
        self.registry.get(token,task['manifest']['project'])
        started=bool(task.get('worker')) or task['state'] in ('running','reported','reviewed','needs_verification')
        plan=failure_plan(code,started=started)
        return {'task_id':task_id,'task_state':task['state'],**plan,
                'requires_owner_decision':plan['category'] in ('usage_limit','authentication') or not plan['manager_can_check'],
                'reassignment_allowed':False,'operational_change':False}


class DecisionPackets:
    """One owner decision over exact development exceptions, never operational approval."""
    def __init__(self,store,registry):
        if registry.store is not store:raise ValueError('shared manager store required')
        self.store,self.registry=store,registry
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS manager_decision_packets (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL,
                body TEXT NOT NULL, fingerprint TEXT NOT NULL, expires REAL NOT NULL,
                state TEXT NOT NULL,result TEXT,UNIQUE(actor,request_key))''')

    def _snapshot(self,c,token,task_ids,roles=('owner','manager')):
        actor=self.store._authenticate(c,token);self.store._role(actor,roles)
        snapshots=[]
        for task_id in task_ids:
            row=self.store._get(c,actor,task_id)
            config=self.registry._get(c,token,row['project'],roles)
            policy=c.execute('SELECT policy_digest,policy FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
            approval=json.loads(row['approval']) if row['approval'] else None
            semantic_approval=None
            if approval:
                semantic_approval={key:approval[key] for key in ('actor','version','manifest_digest','policy_digest','delegation_id','delegation_digest','manager_hash','decision_packet') if key in approval}
                if isinstance(approval.get('source_proof'),dict):
                    semantic_approval['source_proof']={key:approval['source_proof'].get(key) for key in ('project','source_ref','branch','sha')}
            manifest=json.loads(row['manifest'])
            task_policy=json.loads(policy['policy']) if policy else None
            delegation=None
            if approval and approval.get('delegation_id'):
                grant=None
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='management_delegations'").fetchone():
                    grant=c.execute('SELECT state,fingerprint,policy FROM management_delegations WHERE id=?',(approval['delegation_id'],)).fetchone()
                delegation={'id':approval['delegation_id'],'state':'missing','valid':False}
                if grant is not None:
                    policy_value=json.loads(grant['policy'])
                    delegation.update(state=grant['state'],fingerprint=grant['fingerprint'],expires_at=policy_value.get('expires_at'),config_fingerprint=policy_value.get('config_fingerprint'))
                    try:
                        from .delegation import validate_execution
                        validate_execution(c,self.store,row,check_source_age=row['state']=='queued')
                        delegation['valid']=True
                    except (Conflict,PermissionDenied,NotFound):pass
            snapshots.append({'id':task_id,'project':row['project'],'version':row['version'],
                'manifest_digest':row['manifest_digest'],'policy_digest':policy['policy_digest'] if policy else None,
                'summary':manifest['description'][:1000],'kind':manifest['kind'],'repository':manifest['repository'],
                'base_sha':manifest['base_sha'],'checks':manifest['checks'],
                'allowed_paths':task_policy['allowed_paths'] if task_policy else None,
                'config_fingerprint':config['fingerprint'],'state':row['state'],
                'approval_digest':digest(semantic_approval) if semantic_approval else None,'delegation':delegation})
        return snapshots

    def prepare(self,token,request_key,task_ids,*,ttl=3600):
        name(request_key)
        if type(task_ids) not in (list,tuple) or not 1<=len(task_ids)<=100 or len(set(task_ids))!=len(task_ids):
            raise ValueError('one to 100 unique exact tasks required')
        if type(ttl) is not int or not 60<=ttl<=86400:raise ValueError('bounded decision lifetime required')
        task_ids=sorted(_task_id(x) for x in task_ids)
        _reject_credentials({'request_key':request_key,'tasks':task_ids},token)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            snapshots=self._snapshot(c,token,task_ids)
            body={'tasks':snapshots,'scope':'development_test_only'}
            _reject_credentials(body,token)
            fingerprint=digest(body)
            old=c.execute('SELECT * FROM manager_decision_packets WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise Conflict('packet request changed')
                return self._public(old)
            key=secrets.token_hex(16)
            c.execute('INSERT INTO manager_decision_packets VALUES (?,?,?,?,?,?,?,NULL)',
                      (key,actor['token_hash'],request_key,canonical_json(body),fingerprint,self.store._now()+ttl,'pending'))
            return self._public(c.execute('SELECT * FROM manager_decision_packets WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _public(row):
        body=json.loads(row['body'])
        return {'id':row['id'],'fingerprint':row['fingerprint'],'state':row['state'],
                'tasks':body['tasks'],'approval_required_tasks':[x['id'] for x in body['tasks'] if x['state']=='pending_approval'],
                'informational_tasks':[x['id'] for x in body['tasks'] if x['state']!='pending_approval'],
                'operational_approval':False,'result':json.loads(row['result']) if row['result'] else None}

    def get(self,token,packet_id):
        _task_id(packet_id)
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM manager_decision_packets WHERE id=?',(packet_id,)).fetchone()
            if row is None:raise NotFound('decision packet missing')
            body=json.loads(row['body'])
            current=self._snapshot(c,token,[x['id'] for x in body['tasks']])
            return {**self._public(row),'stale':current!=body['tasks'],'expired':row['expires']<=self.store._now()}

    def decide(self,token,packet_id,fingerprint,*,decision='approve'):
        _task_id(packet_id)
        if type(fingerprint) is not str or len(fingerprint)!=64 or any(x not in '0123456789abcdef' for x in fingerprint):
            raise ValueError('exact decision fingerprint required')
        if decision not in ('approve','reject'):raise ValueError('development approval or rejection only')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner',))
            packet=c.execute('SELECT * FROM manager_decision_packets WHERE id=?',(packet_id,)).fetchone()
            if packet is None:raise NotFound('decision packet missing')
            body=json.loads(packet['body']);ids=[x['id'] for x in body['tasks']]
            current=self._snapshot(c,token,ids,('owner',))
            if packet['fingerprint']!=fingerprint:raise Conflict('exact reviewed packet required')
            if packet['result']:
                result=json.loads(packet['result'])
                if result['decision']!=decision:raise Conflict('packet already decided differently')
                return result
            if packet['state']!='pending' or packet['expires']<=self.store._now():raise Conflict('decision packet expired')
            if current!=body['tasks']:raise Conflict('task, configuration or approval changed after packet review')
            # All source rows and scopes are validated under this transaction before
            # any update. A later error rolls back every approval in the packet.
            changed=[]
            for snapshot in current:
                if snapshot['state']!='pending_approval':continue
                row=self.store._get(c,actor,snapshot['id'])
                if decision=='approve':
                    approval={'actor':actor['subject'],'version':row['version'],'manifest_digest':row['manifest_digest'],
                              'approved_at':self.store._now(),'decision_packet':packet_id}
                    if snapshot['policy_digest'] is not None:approval['policy_digest']=snapshot['policy_digest']
                    c.execute("UPDATE bridge_tasks SET state='queued',approval=?,updated=? WHERE id=? AND state='pending_approval'",
                              (canonical_json(approval),self.store._now(),row['id']))
                    self.store._event(c,row['id'],actor['subject'],'packet_approved','queued',approval)
                else:
                    c.execute("UPDATE bridge_tasks SET state='cancelled',cancel_reason=?,updated=? WHERE id=? AND state='pending_approval'",
                              ('Representative rejected exact development packet',self.store._now(),row['id']))
                    self.store._event(c,row['id'],actor['subject'],'packet_rejected','cancelled')
                changed.append(row['id'])
            result={'packet_id':packet_id,'fingerprint':fingerprint,'decision':decision,'changed_tasks':changed,
                    'operational_approval':False,'deployment_approved':False}
            c.execute('UPDATE manager_decision_packets SET state=?,result=? WHERE id=?',
                      ('decided',canonical_json(result),packet_id))
            return result
