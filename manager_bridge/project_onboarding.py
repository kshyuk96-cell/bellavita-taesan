"""Owner-selected catalog enrollment and read-only source handoff planning.

``register_catalog(owner, projects, expected_revisions)`` atomically registers
selected catalog bindings and enrolls their development in governance. Exact
unchanged configurations are idempotent (revision zero may replay first creation).
``plan_source_handoff(token, project, source, baseline, upstream=None)`` accepts
host-side paths and typed observations; it never fetches, resets or copies source.
Connection, execution and operational approval remain separate pending decisions.
"""
import json
from pathlib import Path
from .connection_readiness import SourcePin
from .handoff_inventory import inspect_handoff
from .maintenance_registry import configuration, name
from .store import Conflict, PermissionDenied, _REPOSITORY, canonical_json, digest


class Onboarding:
    def __init__(self,store,registry):
        if registry.store is not store:raise ValueError('shared registered store required')
        self.store,self.registry=store,registry
        self._catalog=json.loads(Path(__file__).with_name('maintenance_projects.json').read_text())
        if type(self._catalog) is not dict or len(self._catalog)!=4:raise ValueError('four-project customer catalog required')
        for project,config in self._catalog.items():
            name(project);configuration(config)
            if config['maintenance_enabled'] or any(service['purpose']!='review' for service in config['services']):
                raise ValueError('inactive review-only catalog required')

    def catalog(self):
        return json.loads(canonical_json(self._catalog))

    def register_catalog(self,owner,projects,expected_revisions):
        """Enroll selected catalog bindings atomically; differing configs need separate review."""
        if (type(projects) is not list or not 1<=len(projects)<=4
                or any(type(p) is not str or p not in self._catalog for p in projects)
                or len(set(projects))!=len(projects)):
            raise ValueError('one to four exact unique catalog projects required')
        if (type(expected_revisions) is not dict or set(expected_revisions)!=set(projects)
                or any(type(v) is not int or v<0 for v in expected_revisions.values())):
            raise ValueError('exact selected-project expected revisions required')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,owner);self.store._role(actor,('owner',))
            # Preflight every selected binding before any mutation.
            prepared=[]
            for project in projects:
                if not self.store._scope(actor,project):
                    raise PermissionDenied('catalog project outside owner scope')
                config=configuration(self._catalog[project])
                repository=config['source_ref'] if config['source_kind']=='github' else 'sites/'+config['source_ref']
                if not _REPOSITORY.fullmatch(repository):raise ValueError('exact catalog repository required')
                bound=c.execute('SELECT repository FROM bridge_projects WHERE project=?',(project,)).fetchone()
                old=c.execute('SELECT * FROM maintenance_projects WHERE project=?',(project,)).fetchone()
                if bound and bound['repository']!=repository:raise Conflict('registered repository differs from catalog')
                expected=expected_revisions[project]
                if old:
                    if json.loads(old['config'])!=config:raise Conflict('existing configuration differs; catalog never overwrites it')
                    if expected not in (0,old['revision']):raise Conflict('project revision changed')
                    revision=old['revision'];fingerprint=old['fingerprint']
                else:
                    if expected!=0:raise Conflict('new catalog project requires revision zero')
                    revision=1;fingerprint=digest({'project':project,'config':config,'revision':revision})
                prepared.append((project,config,repository,old,revision,fingerprint))
            c.execute('CREATE TABLE IF NOT EXISTS governance_enrolled_projects (project TEXT PRIMARY KEY,enrolled_at REAL NOT NULL)')
            registrations=[]
            for project,config,repository,old,revision,fingerprint in prepared:
                now=self.store._now()
                c.execute('INSERT OR IGNORE INTO bridge_projects VALUES (?,?,?)',(project,repository,now))
                if old is None:
                    c.execute('INSERT INTO maintenance_projects VALUES (?,?,?,?,?)',(project,canonical_json(config),revision,fingerprint,now))
                c.execute('INSERT OR IGNORE INTO governance_enrolled_projects VALUES (?,?)',(project,now))
                registrations.append({'project':project,'revision':revision,'fingerprint':fingerprint,
                                      'config':config,'state':'registered','governance_enrolled':True})
            current_actor=self.store._authenticate(c,owner);self.store._role(current_actor,('owner',))
            if any(not self.store._scope(current_actor,p) for p in projects):
                raise PermissionDenied('owner scope changed during catalog registration')
            return {'registrations':registrations,
                    'pending_connections':[{'project':p,'required':['source_observation','runtime','worker','qa_contract']} for p in projects],
                    'maintenance_enabled':False,'execution_ready':False,'operational_change':False,'deployment_authorized':False}

    def plan_source_handoff(self,token,project,source,baseline,upstream=None):
        """Inspect a host-provided checkout and propose preservation steps, without migration."""
        name(project)
        if not isinstance(baseline,SourcePin) or baseline.project!=project:
            raise ValueError('exact project typed source baseline required')
        config=self.registry.get(token,project)
        inspection=inspect_handoff(config['config'],source,baseline,upstream,clock=self.store._now)
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('project configuration changed during source handoff planning')
        return {**inspection,'config_fingerprint':config['fingerprint'],
                'migration_plan':{'preserve_original':True,'fetch_performed':False,'reset_performed':False,
                                  'strategy':'separate_worktree' if inspection['state']=='upstream_changed' else 'preserve_and_reconcile',
                                  'steps':['record_original_source_and_pending_changes',inspection['next_action'],
                                           'review_exact_source_version_and_requirements','verify_runtime_worker_and_qa_before_execution']}}
