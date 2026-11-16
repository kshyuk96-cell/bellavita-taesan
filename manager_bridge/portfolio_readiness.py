"""Owner batch enrollment, exceptions and current observed requirement coverage.

Coverage counts DB-bound prerequisites, not uptime, product quality or execution
permission. No caller-supplied success flags or percentage are accepted.
"""
import json
import math
import re
from .agent_directory import _reject_known_tokens
from .control_onboarding import ControlOnboarding
from .maintenance_registry import configuration,name
from .operating_budget import OperatingBudget
from .operations_reporting import OperationsReporting
from .project_connection_preflight import ProjectConnectionPreflight
from .store import Conflict,NotFound,PermissionDenied,_REPOSITORY,canonical_json,digest
from .three_tier import ThreeTier


class PortfolioReadiness:
    def __init__(self,core,*,connections=None,budget=None,operations_reporting=None,host_admission=None):
        if not isinstance(core,ThreeTier):raise ValueError('registered hierarchy core required')
        if budget is not None and (not isinstance(budget,OperatingBudget) or budget.core is not core):raise ValueError('same registered admission budget required')
        if operations_reporting is not None and (not isinstance(operations_reporting,OperationsReporting) or operations_reporting.core is not core):raise ValueError('same registered operations reporting required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.control=ControlOnboarding(self.store,self.registry,connections=connections)
        self.preflight=ProjectConnectionPreflight(self.store,self.registry,self.control)
        self.budget,self.operations_reporting=budget,operations_reporting
        from .host_admission import HostAdmission
        self.host_admission=host_admission or getattr(budget,'host_admission',None)
        if self.host_admission is not None and (not isinstance(self.host_admission,HostAdmission) or self.host_admission.core is not core):raise ValueError('same registered host admission required')
        with self.store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS portfolio_registration_batches (
                id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,request TEXT,result TEXT,
                UNIQUE(actor,request_key))''')

    @staticmethod
    def _projects(projects):
        if type(projects) is not list or not 1<=len(projects)<=20:raise ValueError('one to twenty exact portfolio projects required')
        for project in projects:name(project)
        if len(set(projects))!=len(projects):raise ValueError('duplicate portfolio project')
        return sorted(projects)

    def _scope(self,c,token,projects):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        configs={p:self.registry._get(c,token,p) for p in projects}
        return actor,configs

    def _recheck(self,c,token,projects,configs):
        actor,current=self._scope(c,token,projects)
        if {p:(v['revision'],v['fingerprint']) for p,v in current.items()}!={p:(v['revision'],v['fingerprint']) for p,v in configs.items()}:raise Conflict('portfolio registry binding changed')
        return actor

    def register_batch(self,owner,request_key,registrations):
        """Atomically register new inactive review-only metadata; never overwrite scope.

        Every item has exactly project/config/expected_revision/expected_fingerprint.
        Existing unchanged metadata requires its current revision and fingerprint.
        The same successful request key replays only its unchanged registered scope.
        """
        name(request_key)
        if type(registrations) is not list or not 1<=len(registrations)<=20:raise ValueError('bounded exact owner registration batch required')
        prepared=[]
        for item in registrations:
            if type(item) is not dict or set(item)!={'project','config','expected_revision','expected_fingerprint'}:raise ValueError('exact project configuration and registry comparison required')
            project=name(item['project']);config=configuration(item['config']);revision=item['expected_revision'];fingerprint=item['expected_fingerprint']
            if (type(revision) is not int or revision<0 or (revision==0 and fingerprint is not None)
                    or (revision>0 and (type(fingerprint) is not str or not re.fullmatch('[a-f0-9]{64}',fingerprint)))):raise ValueError('exact prior registry revision/fingerprint required')
            if config['maintenance_enabled'] or any(s['purpose']!='review' for s in config['services']):raise ValueError('new portfolio registration is inactive and review-only')
            if len(canonical_json(config).encode())>20000:raise ValueError('bounded project configuration required')
            prepared.append({'project':project,'config':config,'expected_revision':revision,'expected_fingerprint':fingerprint})
        projects=self._projects([p['project'] for p in prepared]);prepared.sort(key=lambda p:p['project'])
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,owner);self.store._role(actor,('owner',))
            if any(not self.store._scope(actor,p) for p in projects):raise PermissionDenied('registration project outside owner scope')
            _reject_known_tokens(c,{'request_key':request_key,'registrations':prepared})
            old=c.execute('SELECT * FROM portfolio_registration_batches WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if json.loads(old['request'])!=prepared:raise Conflict('registration request key scope is immutable')
                result=json.loads(old['result'])
                for item in result['registrations']:
                    current=self.registry._get(c,owner,item['project'],('owner',))
                    if (current['revision'],current['fingerprint'])!=(item['revision'],item['fingerprint']):raise Conflict('registered batch scope changed')
                    enrolled=c.execute('SELECT 1 FROM governance_enrolled_projects WHERE project=?',(item['project'],)).fetchone()
                    if not enrolled:raise Conflict('registered governance enrollment changed')
                _reject_known_tokens(c,result);return result
            writes=[]
            for item in prepared:
                project=item['project'];config=item['config']
                repository=config['source_ref'] if config['source_kind']=='github' else 'sites/'+config['source_ref']
                if not _REPOSITORY.fullmatch(repository):raise ValueError('exact registered repository required')
                bound=c.execute('SELECT repository FROM bridge_projects WHERE project=?',(project,)).fetchone()
                row=c.execute('SELECT * FROM maintenance_projects WHERE project=?',(project,)).fetchone()
                if bound and bound['repository']!=repository:raise Conflict('immutable project repository differs')
                if row:
                    if (row['revision'],row['fingerprint'])!=(item['expected_revision'],item['expected_fingerprint']):raise Conflict('prior registry binding changed')
                    if json.loads(row['config'])!=config:raise Conflict('registration never overwrites existing project configuration')
                    revision=row['revision'];fingerprint=row['fingerprint']
                else:
                    if item['expected_revision']!=0:raise Conflict('new project requires absent prior binding')
                    revision=1;fingerprint=digest({'project':project,'config':config,'revision':revision})
                writes.append((project,config,repository,row is None,revision,fingerprint))
            c.execute('CREATE TABLE IF NOT EXISTS governance_enrolled_projects (project TEXT PRIMARY KEY,enrolled_at REAL NOT NULL)')
            result={'batch_id':digest({'actor':actor['token_hash'],'request_key':request_key}),'registrations':[],
                    'maintenance_enabled':False,'execution_authorized':False,'operational_authorized':False,'credentials_created':False}
            for project,config,repository,new,revision,fingerprint in writes:
                now=self.store._now();c.execute('INSERT OR IGNORE INTO bridge_projects VALUES (?,?,?)',(project,repository,now))
                if new:c.execute('INSERT INTO maintenance_projects VALUES (?,?,?,?,?)',(project,canonical_json(config),revision,fingerprint,now))
                c.execute('INSERT OR IGNORE INTO governance_enrolled_projects VALUES (?,?)',(project,now))
                result['registrations'].append({'project':project,'revision':revision,'fingerprint':fingerprint,'metadata_state':'registered' if new else 'unchanged',
                                                'governance_enrolled':True,'connection_observation':'not_performed','role_assignment_performed':False,'budget_installation_performed':False})
            current=self.store._authenticate(c,owner);self.store._role(current,('owner',))
            if any(not self.store._scope(current,p) for p in projects):raise PermissionDenied('registration owner scope changed')
            for project,config,_,_,revision,fingerprint in writes:
                row=self.registry._get(c,owner,project,('owner',))
                if (row['revision'],row['fingerprint'],row['config'])!=(revision,fingerprint,config):raise Conflict('registration changed before commit')
            _reject_known_tokens(c,result)
            c.execute('INSERT INTO portfolio_registration_batches VALUES (?,?,?,?,?)',(result['batch_id'],actor['token_hash'],request_key,canonical_json(prepared),canonical_json(result)))
            return result

    def _leader(self,c,projects,now):
        if not self.preflight._table(c,'continuous_leader'):return False
        leader=c.execute('SELECT * FROM continuous_leader WHERE singleton=1').fetchone()
        if not leader or leader['projects_digest']!=digest(projects):return False
        expires=leader['expires']
        if type(expires) not in (int,float) or not math.isfinite(expires) or not now<expires<=now+300:return False
        for project in projects:
            try:
                live=self.core._live(c,leader['actor_hash'],project)
                if live['role']!='manager':return False
                member=c.execute("SELECT member_id FROM hierarchy_members WHERE project=? AND credential_hash=? AND role='taesan'",(project,leader['actor_hash'])).fetchone()
                if not member:return False
                binding=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(project,)).fetchone()
                self.core._member(c,project,member['member_id'],'taesan',{'fingerprint':binding['fingerprint']})
            except (PermissionDenied,Conflict):return False
        return True

    def _assess(self,c,token,projects):
        _,configs=self._scope(c,token,projects);inventory=self.control._inventory(c,token,projects,300)
        now=self.store._now()
        if type(now) not in (int,float) or not math.isfinite(now) or now<=0:raise Conflict('finite actual readiness clock required')
        leader=self._leader(c,projects,now);items=[]
        quarantined=OperatingBudget._quarantined(c)
        for inv in inventory['projects']:
            project=inv['project'];config=configs[project];source=self.preflight._source(c,project,config,300)
            report=self.control.connections._latest(c,token,project,300);pin=report.get('source_pin')
            pin_matches=(type(pin) is dict and source.get('source_pin') is not None
                         and all(pin.get(k)==source['source_pin'][k] for k in ('project','source_kind','source_ref','branch','sha','revision')))
            needed=('receiver','sandbox','host')
            connection_states={f['capability']:f['classification'] for f in inv['connections']}
            connections=pin_matches and all(connection_states.get(k)=='observed_verified' for k in needed)
            role_sets={r['role']:{m['subject'] for m in r['members'] if m['classification']=='registered'} for r in inv['roles']}
            roles=(all(r['classification']=='registered' and not r['inventory_truncated'] for r in inv['roles'])
                   and all(role_sets.get(r) for r in ('taesan','developer','reviewer'))
                   and not any(role_sets[a]&role_sets[b] for a,b in (('taesan','developer'),('taesan','reviewer'),('developer','reviewer'))))
            try:self.core._capacity(c,project);capacity=True
            except Conflict:capacity=False
            policy=c.execute('SELECT * FROM governance_qa_policies WHERE project=?',(project,)).fetchone() if self.preflight._table(c,'governance_qa_policies') else None
            qa_policy=False
            if policy:
                try:
                    body=json.loads(policy['policy'])
                    qa_policy=body['config_fingerprint']==config['fingerprint'] and body['source_config']==config['config'] and digest(body)==policy['policy_digest']
                except (ValueError,KeyError,TypeError):pass
            admission=self.budget._check(c,token,project) if self.budget else {'configured':False,'allowed':False,'reason':'budget_service_not_configured'}
            budget=admission.get('configured') is True and admission.get('allowed') is True
            budget_reason=('authentication_'+admission['auth_state'] if admission.get('configured') and admission.get('auth_state') not in (None,'ready') else admission.get('reason') or 'budget_unverified')
            host=self.host_admission._check(c,token,project) if self.host_admission else {'allowed':False,'reason':'host_admission_not_configured'}
            gate=getattr(self.core,'governance_gate',None);providers=getattr(gate,'providers',{})
            checks=[('registry_binding',True,None),('owner_new_work_enabled',config['config']['maintenance_enabled'],'new_work_disabled'),
                    ('fresh_source',source['classification']=='observed_current_source',source.get('reason')),
                    ('source_bound_connections',connections,'connection_evidence_missing_stale_or_source_mismatched'),
                    ('independent_live_role_registrations',bool(roles),'role_registration_missing_unverified_or_denied'),
                    ('capacity_limits_registered',capacity,'capacity_limits_missing'),('qa_policy_registered',qa_policy,'qa_policy_missing_or_changed'),
                    ('qa_provider_registered',callable(providers.get(project)),'qa_provider_unavailable'),
                    ('admission_budget',budget,budget_reason),
                    ('host_admission',host.get('allowed') is True,host.get('reason') or 'host_admission_unverified'),
                    ('current_continuous_leader_lease',leader,'continuous_leader_lease_unverified'),('restore_reconciled',not quarantined,'restore_reconciliation_required')]
            evidence=[{'requirement':key,'state':'observed_met' if met else 'unmet_or_unknown','reason':None if met else reason} for key,met,reason in checks]
            met=sum(x['state']=='observed_met' for x in evidence)
            items.append({'project':project,'binding':inv['binding'],'state':'observed_prerequisites_met' if met==len(evidence) else 'blocked',
                          'requirements':evidence,'observed_requirement_coverage':{'met':met,'total':len(evidence)},
                          'source':source,'roles':inv['roles'],'budget':admission,'host_admission':host,'execution_authorized':False})
        self._recheck(c,token,projects,configs)
        result={'projects':items,'observed_requirement_coverage':{'met':sum(p['observed_requirement_coverage']['met'] for p in items),
                                                               'total':sum(p['observed_requirement_coverage']['total'] for p in items)},
                'percentage':None,'always_on_verified':False,'uptime_measured':False,'readiness_scope':'current_db_bound_prerequisites_only',
                'execution_authorized':False,'operational_authorized':False,'observed_at':now}
        _reject_known_tokens(c,result);return result

    def assess(self,token,projects):
        """Count observed current prerequisites; unknowns never count as complete."""
        projects=self._projects(projects)
        with self.store._transaction() as c:return self._assess(c,token,projects)

    def exceptions(self,token,projects,request_key,after=None,limit=20):
        """Reuse grouped operations observations, then add fresh readiness exceptions."""
        projects=self._projects(projects);name(request_key)
        if after is not None:name(after)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded exception group window required')
        with self.store._transaction() as c:
            _,configs=self._scope(c,token,projects);_reject_known_tokens(c,{'request_key':request_key,'after':after})
        operation=self.operations_reporting.report(token,projects,request_key,after=after,limit=limit) if self.operations_reporting else None
        if operation is None and after is not None:raise NotFound('operations cursor unavailable without reporting service')
        with self.store._transaction() as c:
            self._recheck(c,token,projects,configs);readiness=self._assess(c,token,projects)
            grouped={}
            for project in readiness['projects']:
                for check in project['requirements']:
                    if check['state']=='observed_met':continue
                    reason=check['reason'] or 'not_observed';urgency='urgent' if reason in ('authentication_expired','restore_reconciliation_required','restore_quarantine') else 'normal'
                    key=(project['project'],reason,urgency)
                    grouped[key]={'project':key[0],'category':reason,'urgency':urgency,'count':grouped.get(key,{}).get('count',0)+1}
            groups=sorted(grouped.values(),key=lambda x:(x['urgency']!='urgent',x['project'],x['category']))
            result={'readiness_exceptions':groups[:limit],'readiness_window':{'limit':limit,'total':len(groups),'displayed':min(limit,len(groups)),'partial':len(groups)>limit},'operations_exceptions':{'urgent':operation['urgent'],'normal':operation['normal']} if operation else None,
                    'operations_window':operation['group_window'] if operation else {'configured':False,'partial':False,'next_cursor':None},
                    'operations_snapshot_state':operation['snapshot_state'] if operation else 'not_configured',
                    'operations_observed_at':operation.get('observed_at') if operation else None,'readiness_observed_at':readiness['observed_at'],
                    'summary_complete':bool(operation and operation.get('summary_complete',False) and operation.get('snapshot_state')=='recorded' and len(groups)<=limit),
                    'observed_requirement_coverage':readiness['observed_requirement_coverage'],'percentage':None,
                    'always_on_verified':False,'execution_authorized':False,'messages_sent':False,'per_task_messages_sent':0}
            _reject_known_tokens(c,result);return result
