"""Owner-pinned project/role/broker/host candidates, never a live reconfiguration.

Templates and service objects are trusted Python configuration. Public planning
arguments select exact registered identifiers; no credential file is read here.
"""
import json
import math
from pathlib import Path
import re
from .agent_directory import _reject_known_tokens
from .continuous_host import ContinuousHost
from .control_onboarding import ControlOnboarding
from .customer_worker_profiles import _path,_overlap
from .project_connection_preflight import ProjectConnectionPreflight
from .subscription_broker import SubscriptionBroker
from .maintenance_registry import name
from .store import Conflict,NotFound,PermissionDenied,canonical_json,digest,_REPOSITORY
from .three_tier import ThreeTier


class ProjectLinkPlan:
    def __init__(self,core,host=None,*,brokers=(),templates=None):
        if not isinstance(core,ThreeTier) or (host is not None and (not isinstance(host,ContinuousHost) or host.core is not core)):raise ValueError('same registered core and host required')
        if type(brokers) not in (list,tuple) or len(brokers)>20 or any(type(b) is not SubscriptionBroker or b.core is not core for b in brokers):raise ValueError('bounded same-core registered brokers required')
        if len({b.ref for b in brokers})!=len(brokers):raise ValueError('duplicate subscription broker reference')
        self.core,self.store,self.registry,self.host=core,core.store,core.registry,host
        self.brokers={b.ref:b for b in brokers};self.control=ControlOnboarding(self.store,self.registry)
        self.preflight=ProjectConnectionPreflight(self.store,self.registry,self.control)
        if templates is None:templates={}
        if type(templates) is not dict or len(templates)>20:raise ValueError('bounded trusted host templates required')
        self.templates={}
        for project,entry in templates.items():self.templates[name(project)]=self._template(project,entry)
        with self.store._transaction() as c:
            _reject_known_tokens(c,self.templates)
            c.execute('''CREATE TABLE IF NOT EXISTS project_link_plans (
                id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,request TEXT,body TEXT,fingerprint TEXT,
                UNIQUE(actor,request_key))''')

    @staticmethod
    def _template(project,entry):
        needed={'project','repository','source','candidates','runtime','developer_id','reviewer_id','developer_token_file','reviewer_token_file','checks','developer','reviewer'}
        optional={'session_id','fleet_template','fleet_host_ref','worker_mode'}
        if type(entry) is not dict or not needed<=set(entry) or set(entry)-needed-optional or entry['project']!=project:raise ValueError('exact trusted load_host project template required')
        if type(entry['repository']) is not str or not _REPOSITORY.fullmatch(entry['repository']):raise ValueError('exact template repository required')
        for field in ('developer_id','reviewer_id'):name(entry[field])
        paths={k:_path(entry[k]) for k in ('source','candidates','runtime','developer_token_file','reviewer_token_file')}
        if type(entry['checks']) is not dict or not 1<=len(entry['checks'])<=30:raise ValueError('fixed bounded template checks required')
        for check,argv in entry['checks'].items():
            name(check)
            if type(argv) not in (list,tuple) or not 1<=len(argv)<=50 or any(type(x) is not str or not x or len(x)>4000 for x in argv):raise ValueError('fixed bounded check argv required')
        homes=[]
        for role in ('developer','reviewer'):
            value=entry[role]
            if (type(value) is not dict or not {'binary','codex_home'}<=set(value)
                    or set(value)-{'binary','codex_home','require_origin','execution_timeout'}):raise ValueError('credential-free native profile settings required')
            _path(value['binary']);homes.append(_path(value['codex_home']))
            if 'require_origin' in value and type(value['require_origin']) is not bool:raise ValueError('explicit template origin policy required')
            if 'execution_timeout' in value and (type(value['execution_timeout']) is not int or not 1<=value['execution_timeout']<=3600):raise ValueError('bounded execution timeout required')
        roots=[paths['source'],paths['candidates'],paths['runtime'],*homes]
        if any(_overlap(a,b) for i,a in enumerate(roots) for b in roots[i+1:]):raise ValueError('isolated template work/profile roots required')
        if paths['developer_token_file']==paths['reviewer_token_file'] or any(root==p or root in p.parents for root in roots for p in (paths['developer_token_file'],paths['reviewer_token_file'])):raise ValueError('independent external token-file references required')
        for key in ('fleet_template','fleet_host_ref'):
            if key in entry:name(entry[key])
        if 'session_id' in entry:
            from .fleet_codex import _uuid
            _uuid(entry['session_id'])
        if entry.get('worker_mode','metadata') not in ('metadata','deferred_native'):raise ValueError('fixed worker mode required')
        if len(canonical_json(entry).encode())>64000:raise ValueError('bounded trusted template required')
        return json.loads(canonical_json(entry))

    def _scope(self,c,owner,projects):
        actor=self.store._authenticate(c,owner);self.store._role(actor,('owner',))
        return actor,{p:self.registry._get(c,owner,p,('owner',)) for p in projects}

    def _host_map(self):
        result={}
        for project,host in sorted(self.host.projects.items()) if self.host else []:
            result[project]={'repository':host.repository,'developer_id':host.developer_id,'reviewer_id':host.reviewer_id,
                             'resource_digest':digest({key:str(getattr(host,key)) for key in ('source','candidates','runtime','developer_token_file','reviewer_token_file')}),
                             'checks_digest':digest(host.checks),'session_id':host.session_id,'fleet_template':host.fleet_template,
                             'fleet_host_ref':host.fleet_host_ref,'worker_mode':host.worker_mode}
        return result

    def _busy(self,c,project):
        now=self.store._now()
        if type(now) not in (int,float) or not math.isfinite(now):raise Conflict('finite link-plan clock required')
        blocks=[]
        if c.execute("SELECT 1 FROM bridge_tasks WHERE project=? AND (state IN ('pending_approval','queued','running','reported','needs_verification') OR lease_expires>?) LIMIT 1",(project,now)).fetchone():blocks.append('current_task_or_live_lease')
        if c.execute("SELECT 1 FROM hierarchy_tasks WHERE project=? AND state NOT IN ('accepted','cancelled') LIMIT 1",(project,)).fetchone():blocks.append('managed_task_in_progress')
        if self.preflight._table(c,'continuous_actions') and c.execute("SELECT 1 FROM continuous_actions WHERE project=? AND state IN ('running','uncertain') LIMIT 1",(project,)).fetchone():blocks.append('continuous_execution_running_or_uncertain')
        if self.preflight._table(c,'continuous_host_assignments') and c.execute("SELECT 1 FROM continuous_host_assignments a JOIN bridge_tasks t ON t.id=a.task_id WHERE t.project=? AND a.phase IN ('executing','freezing','uncertain') LIMIT 1",(project,)).fetchone():blocks.append('native_host_execution_running_or_uncertain')
        if self.preflight._table(c,'fleet_creates') and c.execute("SELECT 1 FROM fleet_creates WHERE project=? AND state IN ('creating','reserved','executing','uncertain') LIMIT 1",(project,)).fetchone():blocks.append('fleet_creation_or_execution_in_progress')
        if self.preflight._table(c,'operating_budget_reservations') and c.execute("SELECT 1 FROM operating_budget_reservations WHERE project=? AND state IN ('reserved','started','uncertain') LIMIT 1",(project,)).fetchone():blocks.append('budget_reservation_active_or_uncertain')
        if SubscriptionBroker._quarantined(c):blocks.append('restore_reconciliation_required')
        return blocks

    def _snapshot(self,c,owner,projects):
        actor,configs=self._scope(c,owner,projects);host_map=self._host_map();items=[];broker_refs={}
        inventory=self.control._inventory(c,owner,projects,300)
        by_project={p['project']:p for p in inventory['projects']}
        for project in projects:
            references=[]
            for ref,broker in sorted(self.brokers.items()):
                targets=[t for t in broker.targets if t.project==project]
                if not targets:continue
                if any(not self.store._scope(actor,t.project) for t in broker.targets):raise PermissionDenied('owner scope must cover referenced shared broker')
                try:
                    row=broker._state(c)
                    if row['binding']!=broker.binding:raise Conflict('broker binding changed')
                    state=row['state'];generation=row['generation'];binding=row['binding']
                except (Conflict,ValueError):state='unverified';generation=None;binding=broker.binding
                references.append({'broker_ref':ref,'generation':generation,'binding':binding,'state':state,
                                   'profiles':[{'member_id':t.member_id,'role':t.role,'profile_ref':digest(str(t.home))} for t in targets]})
            broker_refs[project]=[{k:r[k] for k in ('broker_ref','generation','binding')} for r in references]
            items.append({'project':project,'binding':{'revision':configs[project]['revision'],'fingerprint':configs[project]['fingerprint']},
                          'roles':by_project[project]['roles'],'brokers':references,'source':self.preflight._source(c,project,configs[project],300),
                          'current_host':host_map.get(project),'template_digest':digest(self.templates[project]) if project in self.templates else None,
                          'blocks':self._busy(c,project)})
        result={'projects':items,'bindings':{p['project']:p['binding'] for p in items},'broker_refs':broker_refs,
                'host_map_digest':digest(host_map),'activation_performed':False,'credentials_expanded':False}
        _,current=self._scope(c,owner,projects)
        if any((current[p]['revision'],current[p]['fingerprint'])!=(configs[p]['revision'],configs[p]['fingerprint']) for p in projects):raise Conflict('registry changed during link inventory')
        if digest(self._host_map())!=result['host_map_digest']:raise Conflict('host map changed during link inventory')
        _reject_known_tokens(c,result);return actor,result

    def inventory(self,owner,projects):
        projects=self.control._selection(projects,300)
        with self.store._transaction() as c:return self._snapshot(c,owner,projects)[1]

    def _resource_blocks(self,project,template):
        work=[_path(template[k]) for k in ('source','candidates','runtime')];blocks=[]
        for other,entry in self.templates.items():
            if other==project:continue
            roots=[_path(entry[k]) for k in ('source','candidates','runtime')]
            roots.extend(_path(entry[role]['codex_home']) for role in ('developer','reviewer'))
            if any(_overlap(a,b) for a in work for b in roots):blocks.append('candidate_overlaps_other_trusted_template')
        private=[self.store.path]+([self.host.manager_token_file] if self.host else [])
        for host in self.host.projects.values() if self.host else []:
            private.extend((host.developer_token_file,host.reviewer_token_file))
        for broker in self.brokers.values():private.extend([broker.home,*[t.home for t in broker.targets]])
        if any(_overlap(a,Path(b).absolute()) for a in work for b in private):blocks.append('candidate_overlaps_private_state_or_subscription_profile')
        return blocks

    def _build(self,c,owner,projects,role_mappings):
        actor,snapshot=self._snapshot(c,owner,projects);candidates=[]
        for project in projects:
            item=next(p for p in snapshot['projects'] if p['project']==project);roles=role_mappings[project];blocks=list(item['blocks']);config=self.registry._get(c,owner,project,('owner',));authority=[]
            subjects=[]
            for role,member_id in roles.items():
                try:
                    member=self.core._member(c,project,member_id,role,config);subjects.append(member['subject'])
                    authority.append((role,member_id,member['credential_hash'],member['subject'],member['config_fingerprint']))
                except (PermissionDenied,Conflict):blocks.append('role_'+role+'_missing_or_changed')
            if len(subjects)!=len(set(subjects)):blocks.append('independent_role_subjects_required')
            template=self.templates.get(project)
            if template is None:blocks.append('trusted_host_template_missing')
            else:
                if (template['repository']!=self.store._project_access(c,actor,project)
                        or template['developer_id']!=roles['developer'] or template['reviewer_id']!=roles['reviewer']):blocks.append('template_repository_or_role_binding_differs')
                if 'session_id' in template:
                    registered=self.host.projects.get(project) if self.host else None
                    if registered is None or registered.session_id!=template['session_id']:blocks.append('existing_host_session_reference_required')
                elif 'fleet_template' in template and 'fleet_host_ref' in template:
                    approved=c.execute('SELECT * FROM fleet_templates WHERE project=? AND template_ref=?',(project,template['fleet_template'])).fetchone() if self.preflight._table(c,'fleet_templates') else None
                    if not approved or approved['host_ref']!=template['fleet_host_ref'] or approved['config_fingerprint']!=config['fingerprint'] or approved['expires']<=self.store._now():blocks.append('approved_current_fleet_template_required')
                    elif self.core._live(c,approved['owner_hash'],project)['role']!='owner':blocks.append('fleet_template_owner_unavailable')
                else:blocks.append('session_or_approved_fleet_template_required')
                blocks.extend(self._resource_blocks(project,template))
                forbidden=[self.store.path]+([self.host.manager_token_file] if self.host else [])
                if any(_overlap(_path(template[k]),Path(p).absolute()) for k in ('source','candidates','runtime') for p in forbidden):blocks.append('private_host_state_overlaps_candidate_work_root')
                for other,host in (self.host.projects.items() if self.host else []):
                    if other!=project and any(_overlap(_path(template[k]),Path(getattr(host,j))) for k in ('source','candidates','runtime') for j in ('source','candidates','runtime')):blocks.append('candidate_overlaps_other_project_host')
            choices=[]
            for broker in item['brokers']:
                if broker['state']!='ready':continue
                if all(any(p['member_id']==roles[role] and p['role']==role for p in broker['profiles']) for role in ('developer','reviewer')):choices.append(broker)
            if len(choices)!=1:blocks.append('one_ready_registered_role_broker_required')
            if template and len(choices)==1:
                broker=self.brokers[choices[0]['broker_ref']]
                for role in ('developer','reviewer'):
                    target=next(t for t in broker.targets if t.project==project and t.role==role and t.member_id==roles[role])
                    if _path(template[role]['codex_home'])!=target.home:blocks.append('template_profile_differs_from_registered_broker_target')
            if item['source']['classification']!='observed_current_source':blocks.append('fresh_registered_source_required')
            candidates.append({'project':project,'binding':item['binding'],'role_mappings':roles,'authority_digest':digest(authority),
                               'state':'blocked' if blocks else 'candidate','blocks':sorted(set(blocks)),
                               'generated_host_settings':({'project':project,'configuration_ref':project,'template_digest':item['template_digest'],
                                  'repository':template['repository'] if template else None,'role_mappings':roles,
                                  'checks_digest':digest(template['checks']) if template else None,
                                  'resource_digest':digest({key:template[key] for key in ('source','candidates','runtime','developer_token_file','reviewer_token_file')}) if template else None,
                                  'broker_refs':snapshot['broker_refs'][project],'source_pin':item['source']['source_pin'],
                                  'requires_trusted_private_materialization':True,'requires_source_origin_and_loader_validation':True} if not blocks else None),
                               'connection_verified':False,'application_performed':False,'scope_expanded':False})
        _,latest=self._scope(c,owner,projects)
        if {p:{'revision':latest[p]['revision'],'fingerprint':latest[p]['fingerprint']} for p in projects}!=snapshot['bindings'] or digest(self._host_map())!=snapshot['host_map_digest']:raise Conflict('link configuration changed during candidate generation')
        return actor,{'snapshot':snapshot,'candidates':candidates,'candidate_only':True,'execution_authorized':False,
                      'credentials_read':False,'credentials_expanded':False,'activation_performed':False}

    def plan(self,owner,request_key,projects,expected_bindings,role_mappings,expected_host_map_digest,broker_refs):
        name(request_key);projects=self.control._selection(projects,300)
        if type(expected_bindings) is not dict or set(expected_bindings)!=set(projects):raise ValueError('exact registered binding map required')
        for binding in expected_bindings.values():
            if type(binding) is not dict or set(binding)!={'revision','fingerprint'} or type(binding['revision']) is not int or binding['revision']<1 or type(binding['fingerprint']) is not str or not re.fullmatch('[a-f0-9]{64}',binding['fingerprint']):raise ValueError('exact registry revision/fingerprint required')
        if type(expected_host_map_digest) is not str or not re.fullmatch('[a-f0-9]{64}',expected_host_map_digest):raise ValueError('exact current host-map digest required')
        if type(broker_refs) is not dict or set(broker_refs)!=set(projects):raise ValueError('exact project broker references required')
        for references in broker_refs.values():
            if type(references) is not list or len(references)>20:raise ValueError('bounded registered broker references required')
            seen=set()
            for reference in references:
                if type(reference) is not dict or set(reference)!={'broker_ref','generation','binding'}:raise ValueError('exact broker generation/binding reference required')
                name(reference['broker_ref'])
                if reference['broker_ref'] in seen:raise ValueError('duplicate broker reference')
                seen.add(reference['broker_ref'])
                if (reference['generation'] is not None and (type(reference['generation']) is not int or reference['generation']<0)) or type(reference['binding']) is not str or not re.fullmatch('[a-f0-9]{64}',reference['binding']):raise ValueError('exact broker generation/binding required')
        if type(role_mappings) is not dict or set(role_mappings)!=set(projects):raise ValueError('exact project role mapping required')
        for roles in role_mappings.values():
            if type(roles) is not dict or set(roles)!={'taesan','developer','reviewer'}:raise ValueError('exact registered hierarchy roles required')
            for member in roles.values():name(member)
        request={'projects':projects,'expected_bindings':expected_bindings,'role_mappings':role_mappings,
                 'expected_host_map_digest':expected_host_map_digest,'broker_refs':broker_refs}
        with self.store._transaction() as c:
            self._scope(c,owner,projects);_reject_known_tokens(c,{'request_key':request_key,'request':request})
            actor,body=self._build(c,owner,projects,role_mappings);current=body['snapshot']
            if current['bindings']!=expected_bindings or current['host_map_digest']!=expected_host_map_digest or current['broker_refs']!=broker_refs:raise Conflict('exact registry/host/broker plan comparison changed')
            fingerprint=digest(body)
            old=c.execute('SELECT * FROM project_link_plans WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if json.loads(old['request'])!=request or old['fingerprint']!=fingerprint:raise Conflict('project link plan scope changed; use a new request key')
                return self._public(old)
            _reject_known_tokens(c,body);key=digest({'actor':actor['token_hash'],'request_key':request_key})
            c.execute('INSERT INTO project_link_plans VALUES (?,?,?,?,?,?)',(key,actor['token_hash'],request_key,canonical_json(request),canonical_json(body),fingerprint))
            return self._public(c.execute('SELECT * FROM project_link_plans WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _public(row):return {'id':row['id'],'fingerprint':row['fingerprint'],'plan':json.loads(row['body']),'state':'current','application_performed':False}

    def get_plan(self,owner,plan_id):
        name(plan_id)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,owner);self.store._role(actor,('owner',))
            row=c.execute('SELECT * FROM project_link_plans WHERE id=?',(plan_id,)).fetchone()
            if not row or row['actor']!=actor['token_hash']:raise NotFound('project link plan unavailable')
            request=json.loads(row['request']);_,body=self._build(c,owner,request['projects'],request['role_mappings'])
            result=self._public(row)
            if digest(body)!=row['fingerprint']:result['state']='invalidated'
            result['current_candidate_generation_allowed']=all(item['state']=='candidate' for item in body['candidates'])
            result['current_blocks']={item['project']:item['blocks'] for item in body['candidates'] if item['blocks']}
            _reject_known_tokens(c,result);return result
