"""Immutable business proposals and atomic owner batch decisions.

Technical acceptance and native delivery are separate decisions. No prose grants
approval. Source observations must already exist in the trusted source watcher.
"""
import json
import math
import re
import secrets
from .coordination import validate_paths
from .maintenance_registry import name, receipt
from .store import Conflict, NotFound, _manifest, _reject_credentials, canonical_json, digest


SOURCE_MAX_AGE = 300


def _source(c, project, config, now):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='manager_source_heads'").fetchone():
        raise Conflict('trusted source observation required')
    row=c.execute('SELECT * FROM manager_source_heads WHERE project=?',(project,)).fetchone()
    if not row or row['config_fingerprint']!=config['fingerprint']:
        raise Conflict('current source configuration observation required')
    observed=row['observed_at']
    if (type(now) not in (int,float) or not math.isfinite(now)
            or type(observed) not in (int,float) or not math.isfinite(observed)
            or not 0<=now-observed<=SOURCE_MAX_AGE):
        raise Conflict('fresh trusted source observation required')
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='manager_source_observation_status'").fetchone():
        status=c.execute('SELECT * FROM manager_source_observation_status WHERE project=?',(project,)).fetchone()
        if status:
            checked=status['checked_at']
            if (status['config_fingerprint']!=config['fingerprint']
                    or status['state'] not in ('baseline','unchanged','changed')
                    or type(checked) not in (int,float) or not math.isfinite(checked)
                    or not 0<=now-checked<=SOURCE_MAX_AGE):
                raise Conflict('current source observation unavailable')
    return {'sha':row['sha'],'revision':row['revision'],'sequence':row['sequence']}


def validate_execution(c, store, task):
    approval=json.loads(task['approval'] or '{}')
    if 'proposal_batch_id' not in approval:return
    if c is None:raise Conflict('proposal execution requires a transactional fence')
    proposal=c.execute('SELECT * FROM manager_proposals WHERE id=?',(approval.get('proposal_id'),)).fetchone()
    if not proposal or proposal['fingerprint']!=approval.get('proposal_digest'):
        raise Conflict('proposal approval binding changed')
    snapshot=json.loads(proposal['snapshot'])
    config=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(task['project'],)).fetchone()
    if not config or config['fingerprint']!=snapshot['config_fingerprint']:
        raise Conflict('approved proposal configuration changed')
    if _source(c,task['project'],config,store._now())!=snapshot['source']:
        raise Conflict('approved proposal source changed')
    from .governance_gate import validate_proposal_policy
    qa_policy_digest=validate_proposal_policy(c,task['project'],snapshot['spec'])
    if qa_policy_digest!=approval.get('qa_policy_digest'):
        raise Conflict('approved proposal QA policy changed')
    owner=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(approval['owner_hash'],)).fetchone()
    if (not owner or owner['revoked'] is not None or (owner['expires'] is not None and owner['expires']<=store._now()) or owner['role']!='owner' or not store._scope({'role':owner['role'],'projects':json.loads(owner['projects'])},task['project'])):
        raise Conflict('proposal owner authority no longer valid')
    session=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(approval['owner_hash'],)).fetchone()
    if session:
        parent=c.execute('SELECT revoked,expires FROM bridge_tokens WHERE token_hash=?',(session['parent_hash'],)).fetchone()
        if not parent or parent['revoked'] is not None or (parent['expires'] is not None and parent['expires']<=store._now()):
            raise Conflict('proposal owner session authority no longer valid')


class ProposalBoard:
    def __init__(self,store,registry):
        if registry.store is not store:raise ValueError('shared registry required')
        self.store,self.registry=store,registry
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS manager_proposals (
              id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,project TEXT,snapshot TEXT,
              fingerprint TEXT,task TEXT,UNIQUE(actor,request_key));
            CREATE TABLE IF NOT EXISTS manager_proposal_batches (
              id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,snapshot TEXT,fingerprint TEXT,
              approved_by TEXT,UNIQUE(actor,request_key));
            ''')

    def _actor(self,c,token,roles=('owner','manager')):
        actor=self.store._authenticate(c,token);self.store._role(actor,roles);return actor

    def _proposal(self,c,token,key):
        name(key)
        row=c.execute('SELECT * FROM manager_proposals WHERE id=?',(key,)).fetchone()
        if not row:raise NotFound('proposal unavailable')
        self.registry._get(c,token,row['project'])
        return row

    def _current(self,c,token,row):
        snapshot=json.loads(row['snapshot']);config=self.registry._get(c,token,row['project'])
        if config['fingerprint']!=snapshot['config_fingerprint'] or _source(c,row['project'],config,self.store._now())!=snapshot['source']:
            raise Conflict('proposal source or configuration changed; create a new proposal')
        return snapshot

    def propose(self,token,request_key,spec,*,source='manager_proposal'):
        name(request_key)
        if source!='manager_proposal':raise ValueError('structured manager proposal required')
        extra={'allowed_paths','dependencies','qa_contract_ref','qa_contract_digest','priority','risk_rationale'}
        manifest_keys={'project','repository','base_sha','description','checks','kind'}
        if type(spec) is not dict or set(spec)!=extra|manifest_keys:raise ValueError('exact proposal scope required')
        manifest=_manifest({k:spec[k] for k in manifest_keys})
        allowed=validate_paths(spec['allowed_paths']);dependencies=spec['dependencies']
        if type(dependencies) is not list or len(dependencies)>100 or len(set(dependencies))!=len(dependencies):raise ValueError('bounded unique dependencies required')
        for key in dependencies:name(key)
        receipt(spec['qa_contract_ref'])
        if type(spec['qa_contract_digest']) is not str or not re.fullmatch('[a-f0-9]{64}',spec['qa_contract_digest']):raise ValueError('QA contract digest required')
        if spec['priority'] not in ('P0','P1','P2','P3') or type(spec['risk_rationale']) is not str or not spec['risk_rationale'].strip() or len(spec['risk_rationale'])>2000:raise ValueError('priority and bounded risk rationale required')
        spec={**spec,'allowed_paths':allowed};_reject_credentials(spec,token)
        with self.store._transaction() as c:
            actor=self._actor(c,token);config=self.registry._get(c,token,manifest['project'])
            if self.store._project_access(c,actor,manifest['project'])!=manifest['repository']:raise Conflict('repository mismatch')
            old=c.execute('SELECT * FROM manager_proposals WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if json.loads(old['snapshot'])['spec']!=spec:raise Conflict('proposal key scope is immutable')
                self._current(c,token,old);return self._public(old)
            observed=_source(c,manifest['project'],config,self.store._now())
            if observed['sha']!=manifest['base_sha']:raise Conflict('proposal base differs from observed source')
            snapshot={'spec':spec,'config_fingerprint':config['fingerprint'],'source':observed}
            key=secrets.token_hex(16)
            c.execute('INSERT INTO manager_proposals VALUES (?,?,?,?,?,?,NULL)',(key,actor['token_hash'],request_key,manifest['project'],canonical_json(snapshot),digest(snapshot)))
            return self._public(self._proposal(c,token,key))

    @staticmethod
    def _public(row):
        return {'id':row['id'],'project':row['project'],'digest':row['fingerprint'],'snapshot':json.loads(row['snapshot']),'task_id':row['task']}

    def create_batch(self,token,request_key,proposal_ids):
        name(request_key)
        if type(proposal_ids) is not list or not 1<=len(proposal_ids)<=10 or len(set(proposal_ids))!=len(proposal_ids):raise ValueError('one to ten unique proposals required')
        with self.store._transaction() as c:
            actor=self._actor(c,token)
            rows=[self._proposal(c,token,key) for key in proposal_ids]
            for row in rows:self._current(c,token,row)
            items=[{'id':r['id'],'digest':r['fingerprint']} for r in rows]
            old=c.execute('SELECT * FROM manager_proposal_batches WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if json.loads(old['snapshot'])!=items:raise Conflict('batch key scope is immutable')
                return self._batch(c,token,old)
            key=secrets.token_hex(16)
            c.execute('INSERT INTO manager_proposal_batches VALUES (?,?,?,?,?,NULL)',(key,actor['token_hash'],request_key,canonical_json(items),digest(items)))
            return self._batch(c,token,c.execute('SELECT * FROM manager_proposal_batches WHERE id=?',(key,)).fetchone())

    def _batch(self,c,token,row):
        items=json.loads(row['snapshot']);proposals=[self._proposal(c,token,x['id']) for x in items]
        current=True
        for proposal in proposals:
            try:
                self._current(c,token,proposal)
                if proposal['task']:
                    validate_execution(c,self.store,self.store._get(c,self._actor(c,token),proposal['task']))
            except Conflict:current=False
        return {'id':row['id'],'digest':row['fingerprint'],'items':items,'proposals':[self._public(p) for p in proposals],
                'state':('approved' if row['approved_by'] else 'pending_owner_approval') if current else 'invalidated',
                'required_decision':'manager_technical_acceptance' if current and row['approved_by'] else ('owner_business_approval' if current else 'new_proposal_snapshot'),
                'business_approval':bool(row['approved_by']) and current,'historical_business_approval':bool(row['approved_by']),
                'qa_contract_verified':False,
                'technical_acceptance':False,'native_delivery_authorized':False,
                'tasks':[self.store._public(self.store._get(c,self._actor(c,token),p['task'])) for p in proposals if p['task']]}

    def get_proposal(self,token,proposal_id):
        with self.store._transaction() as c:
            self._actor(c,token)
            return self._public(self._proposal(c,token,proposal_id))

    def list_proposals(self,token,project,*,limit=20):
        if type(limit) is not int or not 1<=limit<=50:raise ValueError('bounded proposal limit required')
        with self.store._transaction() as c:
            self.registry._get(c,token,project)
            return [self._public(row) for row in c.execute('SELECT * FROM manager_proposals WHERE project=? ORDER BY rowid DESC LIMIT ?',(project,limit))]

    def list_batches(self,token,project,*,limit=20):
        if type(limit) is not int or not 1<=limit<=50:raise ValueError('bounded batch limit required')
        with self.store._transaction() as c:
            self.registry._get(c,token,project)
            result=[]
            rows=c.execute("""SELECT b.* FROM manager_proposal_batches b WHERE EXISTS
                (SELECT 1 FROM json_each(b.snapshot) item JOIN manager_proposals p
                 ON p.id=json_extract(item.value,'$.id') WHERE p.project=?)
                ORDER BY b.rowid DESC LIMIT ?""",(project,limit)).fetchall()
            for row in rows:
                try:result.append(self._batch(c,token,row))
                except NotFound:continue
            return result

    def get_batch(self,token,batch_id):
        with self.store._transaction() as c:
            self._actor(c,token);row=c.execute('SELECT * FROM manager_proposal_batches WHERE id=?',(batch_id,)).fetchone()
            if not row:raise NotFound('batch unavailable')
            return self._batch(c,token,row)

    def approve_batch(self,token,batch_id,batch_digest):
        with self.store._transaction() as c:
            actor=self._actor(c,token,('owner',));batch=c.execute('SELECT * FROM manager_proposal_batches WHERE id=?',(batch_id,)).fetchone()
            if not batch:raise NotFound('batch unavailable')
            if batch['fingerprint']!=batch_digest:raise Conflict('exact batch snapshot digest required')
            rows=[self._proposal(c,token,item['id']) for item in json.loads(batch['snapshot'])]
            for row in rows:self._current(c,token,row)
            if batch['approved_by']:
                for proposal in rows:
                    validate_execution(c,self.store,self.store._get(c,actor,proposal['task']))
                return self._batch(c,token,batch)
            for row in rows:
                if row['task']:raise Conflict('proposal already approved in another batch')
                snapshot=json.loads(row['snapshot']);spec=snapshot['spec']
                from .governance_gate import validate_proposal_policy
                qa_policy_digest=validate_proposal_policy(c,row['project'],spec)
                manifest=_manifest({k:spec[k] for k in ('project','repository','base_sha','description','checks','kind')})
                bindings=[]
                for key in spec['dependencies']:
                    parent=self.store._get(c,actor,key)
                    if parent['project']!=row['project'] or not parent['approval'] or parent['state'] in ('cancelled','needs_verification'):raise Conflict('approved same-project dependency required')
                    bindings.append({'id':key,'version':parent['version'],'manifest_digest':parent['manifest_digest']})
                task=secrets.token_hex(16);now=self.store._now();md=digest(manifest)
                policy={'version':1,'manifest_digest':md,'allowed_paths':spec['allowed_paths'],'dependencies':bindings,'routing_required':True}
                approval={'actor':actor['subject'],'version':1,'manifest_digest':md,'approved_at':now,'policy_digest':digest(policy),
                          'proposal_batch_id':batch_id,'proposal_id':row['id'],'proposal_digest':row['fingerprint'],'owner_hash':actor['token_hash'],'qa_policy_digest':qa_policy_digest}
                c.execute("INSERT INTO bridge_tasks (id,project,request_key,manifest,manifest_digest,version,state,approval,created,updated) VALUES (?,?,?,?,?,1,'queued',?,?,?)",(task,row['project'],'proposal-'+row['id'],canonical_json(manifest),md,canonical_json(approval),now,now))
                c.execute('INSERT INTO bridge_task_policy VALUES (?,?,?)',(task,canonical_json(policy),digest(policy)))
                c.execute('UPDATE manager_proposals SET task=? WHERE id=?',(task,row['id']))
                self.store._event(c,task,actor['subject'],'submitted','pending_approval',manifest)
                self.store._event(c,task,actor['subject'],'policy_bound','pending_approval',policy)
                self.store._event(c,task,actor['subject'],'approved','queued',approval)
            # Authenticate again at the commit boundary; failures roll back the entire batch.
            self._actor(c,token,('owner',))
            for proposal in rows:
                current=self._proposal(c,token,proposal['id'])
                validate_execution(c,self.store,self.store._get(c,actor,current['task']))
            c.execute('UPDATE manager_proposal_batches SET approved_by=? WHERE id=?',(actor['token_hash'],batch_id))
            return self._batch(c,token,c.execute('SELECT * FROM manager_proposal_batches WHERE id=?',(batch_id,)).fetchone())
