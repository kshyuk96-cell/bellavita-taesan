"""Bounded central-control registration plans and cached connection inventory.

This layer reuses Onboarding's catalog and Connections' stored observations. It
never invokes probes, registers members, reads credentials or grants authority.
"""
import json
import re
from .agent_directory import _reject_known_tokens
from .connection_readiness import CAPABILITIES, Connections
from .maintenance_registry import name
from .project_onboarding import Onboarding
from .store import Conflict, NotFound, canonical_json, digest


class ControlOnboarding:
    def __init__(self,store,registry,connections=None,onboarding=None):
        self.store,self.registry=store,registry
        self.connections=connections or Connections(store,registry)
        self.onboarding=onboarding or Onboarding(store,registry)
        if (registry.store is not store or self.connections.store is not store
                or self.connections.registry is not registry or self.onboarding.store is not store
                or self.onboarding.registry is not registry):raise ValueError('shared registry and connection inventory required')
        self.catalog=self.onboarding.catalog()
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS control_onboarding_plans (
                id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,request TEXT,body TEXT,
                fingerprint TEXT,UNIQUE(actor,request_key))''')

    @staticmethod
    def _selection(projects,max_age):
        if type(projects) is not list or not 1<=len(projects)<=20:raise ValueError('one to twenty exact projects required')
        for project in projects:name(project)
        if len(set(projects))!=len(projects):raise ValueError('duplicate project selection')
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('bounded cached evidence age required')
        return sorted(projects)

    def _actor(self,c,token,projects):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        if any(not self.store._scope(actor,p) for p in projects):raise NotFound('selected project unavailable')
        return actor

    def _roles(self,c,project,config):
        rows=[]
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='hierarchy_members'").fetchone():
            rows=c.execute('SELECT * FROM hierarchy_members WHERE project=? ORDER BY member_id LIMIT 101',(project,)).fetchall()
        truncated=len(rows)>100;rows=rows[:100]
        result=[]
        for role in ('taesan','developer','reviewer'):
            mappings=[]
            for row in rows:
                if row['role']!=role:continue
                credential=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(row['credential_hash'],)).fetchone()
                state='registered';reason=None
                if row['config_fingerprint']!=config['fingerprint']:state='unverified';reason='configuration_changed'
                elif (not credential or credential['revoked'] is not None
                      or (credential['expires'] is not None and credential['expires']<=self.store._now())):
                    state='permission_denied';reason='credential_missing_revoked_or_expired'
                elif (credential['role']!=('manager' if role=='taesan' else 'worker')
                      or credential['subject']!=row['subject']
                      or project not in json.loads(credential['projects'])
                      or (role=='developer' and 'development' not in json.loads(credential['capabilities']))
                      or (role=='reviewer' and 'test' not in json.loads(credential['capabilities']))):
                    state='permission_denied';reason='role_scope_or_capability_changed'
                else:
                    parent=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(row['credential_hash'],)).fetchone()
                    if parent:
                        live=c.execute('SELECT revoked,expires FROM bridge_tokens WHERE token_hash=?',(parent['parent_hash'],)).fetchone()
                        if not live or live['revoked'] is not None or (live['expires'] is not None and live['expires']<=self.store._now()):
                            state='permission_denied';reason='parent_credential_unavailable'
                mappings.append({'member_id':row['member_id'],'subject':row['subject'],'agent_id':row['agent_id'],
                                 'classification':state,'reason':reason,'connection_verified':False})
            result.append({'role':role,'classification':('unverified' if truncated else 'missing' if not mappings else
                           'permission_denied' if any(m['classification']=='permission_denied' for m in mappings) else
                           'unverified' if any(m['classification']=='unverified' for m in mappings) else 'registered'),
                           'members':mappings,'inventory_truncated':truncated})
        return result

    def _inventory(self,c,token,projects,max_age):
        self._actor(c,token,projects);items=[]
        for project in projects:
            exists=c.execute('SELECT 1 FROM maintenance_projects WHERE project=?',(project,)).fetchone()
            if not exists:
                item={'project':project,'binding':{'revision':0,'fingerprint':None},'registration':'missing',
                      'catalog_available':project in self.catalog,'roles':[],'connections':[],
                      'required_action':'owner_register_catalog' if project in self.catalog else 'owner_review_project_configuration'}
                items.append(item);continue
            config=self.registry._get(c,token,project)
            report=self.connections._latest(c,token,project,max_age)
            facts={f['capability']:f for f in report.get('facts',[])};connections=[]
            for capability in CAPABILITIES:
                fact=facts.get(capability,{});state=fact.get('state','not_checked')
                classification=('permission_denied' if state=='permission_denied' else
                                'missing' if state in ('not_configured','not_checked') else
                                'observed_verified' if state=='verified' and not report.get('stale') else
                                'observed_not_applicable' if state=='not_applicable' and not report.get('stale') else 'unverified')
                connections.append({'capability':capability,'classification':classification,'reported_state':state,
                                    'reference':fact.get('reference'),'observed_at':fact.get('observed_at')})
            items.append({'project':project,'binding':{'revision':config['revision'],'fingerprint':config['fingerprint']},
                          'registration':'registered','catalog_available':project in self.catalog,
                          'company':config['config']['company'],'name':config['config']['name'],
                          'roles':self._roles(c,project,config),'connections':connections,
                          'required_action':'review_missing_permissions_and_connection_evidence',
                          'evidence_scope':'cached_report_only'})
        result={'projects':items,'execution_ready':False,'approval_granted':False,'registration_performed':False,
                'connection_probe_performed':False,'maintenance_changed':False,'operational_authorized':False}
        self._actor(c,token,projects)
        for item in items:
            row=c.execute('SELECT revision,fingerprint FROM maintenance_projects WHERE project=?',(item['project'],)).fetchone()
            binding={'revision':row['revision'],'fingerprint':row['fingerprint']} if row else {'revision':0,'fingerprint':None}
            if binding!=item['binding']:raise Conflict('registry changed during control inventory')
        _reject_known_tokens(c,result)
        return result

    def inventory(self,token,projects,*,max_age=300):
        """Read exact bindings, role registrations and cached facts without provider calls."""
        projects=self._selection(projects,max_age)
        with self.store._transaction() as c:return self._inventory(c,token,projects,max_age)

    def plan(self,token,request_key,projects,expected_bindings,*,max_age=300):
        """Persist an immutable exact-snapshot checklist, never perform registration."""
        name(request_key);projects=self._selection(projects,max_age)
        if type(expected_bindings) is not dict or set(expected_bindings)!=set(projects):raise ValueError('exact project binding map required')
        for binding in expected_bindings.values():
            if (type(binding) is not dict or set(binding)!={'revision','fingerprint'}
                    or type(binding['revision']) is not int or binding['revision']<0
                    or (binding['revision']==0 and binding['fingerprint'] is not None)
                    or (binding['revision']>0 and (type(binding['fingerprint']) is not str or not re.fullmatch('[a-f0-9]{64}',binding['fingerprint'])))):
                raise ValueError('exact registry revision/fingerprint required')
        request={'projects':projects,'expected_bindings':expected_bindings,'max_age':max_age}
        with self.store._transaction() as c:
            actor=self._actor(c,token,projects);_reject_known_tokens(c,{'request_key':request_key,'request':request})
            body=self._inventory(c,token,projects,max_age)
            if {p['project']:p['binding'] for p in body['projects']}!=expected_bindings:raise Conflict('registry binding changed before onboarding plan')
            missing=[p['project'] for p in body['projects'] if p['registration']=='missing' and p['catalog_available']]
            body['registration_action']={'method':'Onboarding.register_catalog','owner_required':True,
                                         'projects':missing,'expected_revisions':{p:0 for p in missing},'performed':False}
            fingerprint=digest(body)
            old=c.execute('SELECT * FROM control_onboarding_plans WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if json.loads(old['request'])!=request or old['fingerprint']!=fingerprint:raise Conflict('onboarding plan changed; use a new request key')
                return self._public(old)
            key=digest({'actor':actor['token_hash'],'request_key':request_key})
            c.execute('INSERT INTO control_onboarding_plans VALUES (?,?,?,?,?,?)',(key,actor['token_hash'],request_key,canonical_json(request),canonical_json(body),fingerprint))
            return self._public(c.execute('SELECT * FROM control_onboarding_plans WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _public(row):return {'id':row['id'],'fingerprint':row['fingerprint'],'plan':json.loads(row['body']),'state':'current','approval_granted':False}

    def get_plan(self,token,plan_id):
        name(plan_id)
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM control_onboarding_plans WHERE id=?',(plan_id,)).fetchone()
            if not row:
                self._actor(c,token,[]);raise NotFound('onboarding plan unavailable')
            request=json.loads(row['request']);actor=self._actor(c,token,request['projects'])
            if actor['token_hash']!=row['actor']:raise NotFound('onboarding plan private to its origin')
            current=self._inventory(c,token,request['projects'],request['max_age'])
            missing=[p['project'] for p in current['projects'] if p['registration']=='missing' and p['catalog_available']]
            current['registration_action']={'method':'Onboarding.register_catalog','owner_required':True,'projects':missing,
                                           'expected_revisions':{p:0 for p in missing},'performed':False}
            result=self._public(row)
            if digest(current)!=row['fingerprint']:result['state']='invalidated'
            _reject_known_tokens(c,result)
            return result
