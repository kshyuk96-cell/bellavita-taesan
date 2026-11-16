"""Four-customer read-only connection preflight and one immutable owner checklist.

Cached source/provider metadata and local fixtures never attest live worker,
customer acceptance, deployment, or model quality. This module runs no probes.
"""
from dataclasses import dataclass
import json
import math
import re
from .agent_directory import _reject_known_tokens
from .connection_readiness import SourcePin
from .control_onboarding import ControlOnboarding
from .maintenance_registry import name, receipt
from .store import Conflict, NotFound, canonical_json, digest, _sha


@dataclass(frozen=True)
class MetadataObservation:
    project:str
    config_fingerprint:str
    purpose:str
    provider:str
    target:str
    state:str
    reference:str
    observed_at:float
    source_sha:str|None=None
    source_revision:str|None=None

    def __post_init__(self):
        name(self.project);receipt(self.target);receipt(self.reference)
        if type(self.config_fingerprint) is not str or not re.fullmatch('[a-f0-9]{64}',self.config_fingerprint):raise ValueError('exact metadata configuration binding required')
        if self.purpose not in ('source','service') or self.provider not in ('github','sites','render'):raise ValueError('registered metadata purpose/provider required')
        if self.purpose=='source' and self.provider=='render':raise ValueError('Render is a service metadata target')
        if self.state not in ('observed','permission_denied','unavailable','unknown'):raise ValueError('explicit read-only metadata state required')
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:raise ValueError('actual metadata observation time required')
        if (self.source_sha is None)!=(self.source_revision is None):raise ValueError('source SHA and revision must be supplied together')
        if self.source_sha is not None:
            if self.purpose!='source':raise ValueError('source revisions belong to source metadata only')
            _sha(self.source_sha);name(self.source_revision)


ACTIONS={
    'bellavita-shop':['verify_registered_github_branch_and_shop_requirements','confirm_render_review_service_binding','preserve_shop_erp_original_data_boundaries'],
    'bellavita-erp':['confirm_latest_sites_version_and_erp_requirements','preserve_sheets_d1_original_ownership_and_conflicts','verify_private_sites_receiver_boundary'],
    'hope-sunrise':['confirm_latest_sites_version_and_travel_requirements','separate_sample_balances_from_actual_ledger','verify_member_staff_and_payment_provider_boundaries'],
    'funeral-association':['verify_registered_github_branch_and_association_requirements','confirm_render_review_service_binding','separate_sites_sample_from_postgres_review_data'],
}


class ProjectConnectionPreflight:
    def __init__(self,store,registry,control_onboarding=None,*,metadata=()):
        self.store,self.registry=store,registry
        self.control=control_onboarding or ControlOnboarding(store,registry)
        if registry.store is not store or self.control.store is not store or self.control.registry is not registry:raise ValueError('shared scoped control inventory required')
        if type(metadata) not in (list,tuple) or len(metadata)>44 or any(not isinstance(m,MetadataObservation) for m in metadata):raise ValueError('bounded typed trusted metadata snapshots required')
        self.metadata={}
        for item in metadata:
            if item.project not in ACTIONS:raise ValueError('catalog customer metadata required')
            key=(item.project,item.purpose,item.provider,item.target)
            if key in self.metadata:raise ValueError('duplicate metadata target observation')
            self.metadata[key]=item
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS project_connection_preflight_packets (
                id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,request TEXT,body TEXT,
                fingerprint TEXT,UNIQUE(actor,request_key))''')

    def _selection(self,projects,max_age):
        selected=self.control._selection(list(ACTIONS) if projects is None else projects,max_age)
        if any(p not in ACTIONS for p in selected):raise ValueError('only four exact customer catalog identifiers supported')
        return selected

    def _fresh(self,stamp,max_age):
        now=self.store._now()
        return (type(now) in (int,float) and math.isfinite(now) and type(stamp) in (int,float)
                and math.isfinite(stamp) and 0<=now-stamp<=max_age)

    @staticmethod
    def _table(c,table):return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())

    def _source(self,c,project,config,max_age):
        head=c.execute('SELECT * FROM manager_source_heads WHERE project=?',(project,)).fetchone() if self._table(c,'manager_source_heads') else None
        status=c.execute('SELECT * FROM manager_source_observation_status WHERE project=?',(project,)).fetchone() if self._table(c,'manager_source_observation_status') else None
        result={'classification':'missing','reason':'source_observation_missing','source_pin':None,
                'observation_reference':None,'observed_at':None,'live_connection_verified':False}
        if (status and status['config_fingerprint']==config['fingerprint']
                and self._fresh(status['checked_at'],max_age) and status['state']=='permission_denied'):
            result.update(classification='permission_denied',reason='source_permission_denied');return result
        if not head:return result
        if head['config_fingerprint']!=config['fingerprint']:
            result.update(classification='unverified',reason='source_configuration_changed');return result
        try:
            pin=SourcePin(project,config['config']['source_kind'],config['config']['source_ref'],config['config']['branch'],head['sha'],head['revision'])
            receipt(head['reference'])
        except (ValueError,TypeError):
            result.update(classification='unverified',reason='malformed_cached_source');return result
        result.update(source_pin=vars(pin),observation_reference=head['reference'],observed_at=head['observed_at'])
        if not self._fresh(head['observed_at'],max_age):result.update(classification='unverified',reason='source_stale_or_future')
        elif not status:result.update(classification='unverified',reason='source_status_missing')
        elif status['config_fingerprint']!=config['fingerprint']:result.update(classification='unverified',reason='source_status_configuration_changed')
        elif not self._fresh(status['checked_at'],max_age):result.update(classification='unverified',reason='source_status_stale_or_future')
        elif status['state']=='permission_denied':result.update(classification='permission_denied',reason='source_permission_denied')
        elif status['state'] in ('baseline','unchanged','changed'):result.update(classification='observed_current_source',reason=None)
        else:result.update(classification='unverified',reason='source_connector_'+str(status['state']))
        return result

    def _provider_metadata(self,project,config,max_age):
        targets=[('source',config['config']['source_kind'],config['config']['source_ref'])]
        targets.extend(('service',s['kind'],s['id']) for s in config['config']['services'])
        result=[]
        for purpose,provider,target in targets:
            item=self.metadata.get((project,purpose,provider,target));classification='missing';reason='metadata_not_supplied'
            if item:
                if item.config_fingerprint!=config['fingerprint']:classification='unverified';reason='metadata_configuration_changed'
                elif not self._fresh(item.observed_at,max_age):classification='unverified';reason='metadata_stale_or_future'
                elif item.state=='permission_denied':classification='permission_denied';reason='metadata_permission_denied'
                elif item.state=='observed':classification='observed_metadata';reason=None
                else:classification='unverified';reason='metadata_'+item.state
            result.append({'purpose':purpose,'provider':provider,'target':target,'classification':classification,'reason':reason,
                           'reference':item.reference if item else None,'observed_at':item.observed_at if item else None,
                           'live_connection_verified':False,'source_sha':item.source_sha if item else None,
                           'source_revision':item.source_revision if item else None})
        return result

    @staticmethod
    def _same_source(left,right):
        return bool(left and right and left['sha']==right['sha'] and
                    (left['source_kind']!='sites' or left['revision']==right['revision']))

    def _alignment(self,c,token,project,config,source,metadata,max_age):
        remote=next((m for m in metadata if m['purpose']=='source' and m['classification']=='observed_metadata' and m['source_sha']),None)
        remote_pin=vars(SourcePin(project,config['config']['source_kind'],config['config']['source_ref'],
                                  config['config']['branch'],remote['source_sha'],remote['source_revision'])) if remote else None
        cached=self.control.connections._latest(c,token,project,max_age).get('source_pin')
        try:
            cached=vars(SourcePin(**cached)) if cached else None
            if cached and (cached['project']!=project or tuple(cached[k] for k in ('source_kind','source_ref','branch'))!=tuple(config['config'][k] for k in ('source_kind','source_ref','branch'))):cached=None
        except (ValueError,TypeError):cached=None
        return {'observed_metadata_source_pin':remote_pin,'cached_connection_source_pin':cached,
                'cached_source_matches_metadata':self._same_source(remote_pin,source['source_pin']) if remote_pin and source['source_pin'] else None,
                'cached_connections_match_metadata':self._same_source(remote_pin,cached) if remote_pin and cached else None,
                'metadata_is_execution_authority':False,'latest_source_qa_verified':False}

    def _inspect(self,c,token,projects,max_age):
        actor=self.control._actor(c,token,projects)
        inventory=self.control._inventory(c,token,projects,max_age);reports=[]
        for item in inventory['projects']:
            project=item['project'];blocks=[]
            if item['registration']=='missing':
                reports.append({'project':project,'binding':item['binding'],'classification':'blocked','blocks':['owner_catalog_registration_required'],
                                'source':{'classification':'missing','source_pin':None,'live_connection_verified':False},
                                'roles':item['roles'],'facts':item['connections'],'provider_metadata':[],
                                'required_actions':ACTIONS[project]+['owner_register_catalog'],'execution_blocked':True,
                                'live_connection_verified':False});continue
            config=self.registry._get(c,token,project);source=self._source(c,project,config,max_age)
            metadata=self._provider_metadata(project,config,max_age)
            alignment=self._alignment(c,token,project,config,source,metadata,max_age)
            if alignment['cached_source_matches_metadata'] is False:blocks.append('provider_source_revision_differs_from_cached_source')
            if alignment['observed_metadata_source_pin'] is not None and alignment['cached_connections_match_metadata'] is not True:
                blocks.append('cached_connection_evidence_not_validated_for_latest_source')
                for fact in item['connections']:
                    if fact['classification'] in ('observed_verified','observed_not_applicable'):fact['classification']='unverified'
            if source['classification']!='observed_current_source':blocks.append(source['reason'])
            for role in item['roles']:
                if role['classification']!='registered':blocks.append('role_'+role['role']+'_'+role['classification'])
            for fact in item['connections']:
                if fact['capability'] in ('receiver','sandbox','host') and fact['classification']!='observed_verified':
                    blocks.append('connection_'+fact['capability']+'_'+fact['classification'])
            for observation in metadata:
                if observation['classification']!='observed_metadata':blocks.append(observation['purpose']+'_'+observation['provider']+'_'+observation['classification'])
            # Cached readiness is a useful prerequisite, never the task-specific
            # owner/policy/QA/lease fence that authorizes actual execution.
            blocks.append('task_specific_owner_policy_qa_and_live_execution_fences_required')
            reports.append({'project':project,'binding':item['binding'],'classification':'blocked','blocks':blocks,
                            'source':source,'source_alignment':alignment,'roles':item['roles'],'facts':item['connections'],'provider_metadata':metadata,
                            'required_actions':ACTIONS[project]+['resolve_permission_missing_and_unverified_evidence','review_exact_owner_task_policy_and_qa'],
                            'execution_blocked':True,'live_connection_verified':False,'cached_evidence_only':True})
        result={'projects':reports,'evidence_scope':'cached_connections_source_and_trusted_metadata_only',
                'probe_performed':False,'activation_performed':False,'approval_granted':False,'operational_authorized':False,
                'execution_blocked':True,'model_quality_score':None,'customer_acceptance_verified':False}
        self.control._actor(c,token,projects)
        for report in reports:
            row=c.execute('SELECT revision,fingerprint FROM maintenance_projects WHERE project=?',(report['project'],)).fetchone()
            current={'revision':row['revision'],'fingerprint':row['fingerprint']} if row else {'revision':0,'fingerprint':None}
            if current!=report['binding']:raise Conflict('project changed during connection preflight')
        _reject_known_tokens(c,result)
        return actor,result

    def inspect(self,token,projects=None,*,max_age=300):
        """Read selected catalog customer prerequisites, without invoking providers."""
        projects=self._selection(projects,max_age)
        with self.store._transaction() as c:return self._inspect(c,token,projects,max_age)[1]

    def owner_packet(self,token,request_key,projects=None,*,expected_fingerprints=None,max_age=300):
        """Build one owner-review checklist; it is never an approval endpoint."""
        name(request_key);projects=self._selection(projects,max_age)
        if expected_fingerprints is not None:
            if (type(expected_fingerprints) is not dict or set(expected_fingerprints)!=set(projects)
                    or any(v is not None and (type(v) is not str or not re.fullmatch('[a-f0-9]{64}',v)) for v in expected_fingerprints.values())):
                raise ValueError('exact selected configuration fingerprints required')
        request={'projects':projects,'max_age':max_age,'expected_fingerprints':expected_fingerprints}
        with self.store._transaction() as c:
            actor,body=self._inspect(c,token,projects,max_age)
            _reject_known_tokens(c,{'request_key':request_key,'request':request})
            if expected_fingerprints is not None and {p['project']:p['binding']['fingerprint'] for p in body['projects']}!=expected_fingerprints:
                raise Conflict('project fingerprint changed before owner preflight packet')
            body['owner_actions']=['review_exact_project_versions_and_requirement_boundaries','resolve_registered_role_and_connector_permissions',
                                   'confirm_runtime_receiver_sandbox_and_host_evidence','approve_only_exact_task_policy_and_qa_snapshot_separately']
            fingerprint=digest(body)
            old=c.execute('SELECT * FROM project_connection_preflight_packets WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if json.loads(old['request'])!=request or old['fingerprint']!=fingerprint:raise Conflict('preflight snapshot changed; use a new owner packet request key')
                return self._public(old)
            key=digest({'actor':actor['token_hash'],'request_key':request_key})
            c.execute('INSERT INTO project_connection_preflight_packets VALUES (?,?,?,?,?,?)',(key,actor['token_hash'],request_key,canonical_json(request),canonical_json(body),fingerprint))
            return self._public(c.execute('SELECT * FROM project_connection_preflight_packets WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _public(row):return {'id':row['id'],'fingerprint':row['fingerprint'],'packet':json.loads(row['body']),'state':'current','approval_granted':False}

    def get_packet(self,token,packet_id):
        name(packet_id)
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM project_connection_preflight_packets WHERE id=?',(packet_id,)).fetchone()
            if not row:
                self.control._actor(c,token,[]);raise NotFound('preflight packet unavailable')
            request=json.loads(row['request']);actor,current=self._inspect(c,token,request['projects'],request['max_age'])
            if actor['token_hash']!=row['actor']:raise NotFound('preflight packet private to its origin')
            current['owner_actions']=json.loads(row['body'])['owner_actions']
            result=self._public(row)
            if digest(current)!=row['fingerprint']:result['state']='invalidated'
            _reject_known_tokens(c,result)
            return result
