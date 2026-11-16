"""Read-only routing diagnostics. A plan never grants execution or business approval."""
import json
import math
from .three_tier import ThreeTier
from .continuous_manager import ContinuousManager
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, digest


class GovernanceRouting:
    def __init__(self, core, controller=None):
        if not isinstance(core, ThreeTier):
            raise ValueError('registered ThreeTier required')
        if controller is not None and (not isinstance(controller, ContinuousManager) or controller.core is not core):
            raise ValueError('controller must share exact core')
        self.core, self.controller, self.store = core, controller, core.store

    @staticmethod
    def _table(c, table):
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _capacity(self, c, project, counts_cache):
        try:
            policies = self.core._capacity(c, project)
        except Conflict:
            return {'state': 'not_configured', 'scopes': [], 'available': False}
        scopes = []
        for scope, limits in sorted(policies.items()):
            if scope not in counts_cache:
                clause = ' WHERE t.project=?' if scope != '*' else ''
                params = (scope,) if scope != '*' else ()
                counts = c.execute("SELECT COALESCE(SUM(CASE WHEN t.state IN ('running','needs_verification') OR (t.state='cancelled' AND t.worker_subject IS NOT NULL AND NOT EXISTS (SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id)) THEN 1 ELSE 0 END),0) active, COALESCE(SUM(CASE WHEN t.state='reported' THEN 1 ELSE 0 END),0) pending FROM bridge_tasks t"+clause, params).fetchone()
                reviewing = c.execute("SELECT count(*) FROM hierarchy_tasks WHERE (state='reviewing' OR (state='needs_verification' AND reviewer_id IS NOT NULL AND lease_hash IS NOT NULL))"+(' AND project=?' if scope != '*' else ''), params).fetchone()[0]
                counts_cache[scope] = (counts['active'], counts['pending'], reviewing)
            active, pending, reviewing = counts_cache[scope]
            reviewers = c.execute('SELECT max_active FROM hierarchy_review_limits WHERE scope=?', (scope,)).fetchone()[0]
            scopes.append({'scope': scope, 'active': active, 'review_backlog': pending, 'reviewing': reviewing,
                           'max_active': limits['max_active'], 'max_review': limits['max_review'], 'max_reviewers': reviewers,
                           'development_available': active < limits['max_active'] and pending < limits['max_review'],
                           'review_available': reviewing < reviewers})
        return {'state': 'configured', 'scopes': scopes,
                'available': all(row['development_available'] and row['review_available'] for row in scopes)}

    def _source(self, c, project, config, now):
        row = c.execute('SELECT * FROM manager_source_heads WHERE project=?', (project,)).fetchone() if self._table(c, 'manager_source_heads') else None
        if row is None:
            return {'state': 'unmeasured', 'fresh': False}
        state = 'current' if row['config_fingerprint'] == config['fingerprint'] else 'configuration_changed'
        observed = row['observed_at']
        fresh = type(now) in (int, float) and math.isfinite(now) and type(observed) in (int, float) and math.isfinite(observed) and 0 <= now-observed <= 300
        if state == 'current' and not fresh:
            state = 'stale'
        if state == 'current' and self._table(c, 'manager_source_observation_status'):
            latest = c.execute('SELECT * FROM manager_source_observation_status WHERE project=?', (project,)).fetchone()
            if latest is not None:
                checked = latest['checked_at']
                if latest['config_fingerprint'] != config['fingerprint'] or latest['state'] not in ('baseline', 'unchanged', 'changed') or type(checked) not in (int, float) or not math.isfinite(checked) or not math.isfinite(now) or not 0 <= now-checked <= 300:
                    state = 'latest_observation_unavailable'
        return {'state': state, 'fresh': state == 'current', 'head_sha': row['sha'], 'observed_at': row['observed_at'],
                'observation_only': True}

    def _qa(self, c, project, config):
        enrolled = bool(c.execute('SELECT 1 FROM governance_enrolled_projects WHERE project=?', (project,)).fetchone())
        row = c.execute('SELECT * FROM governance_qa_policies WHERE project=?', (project,)).fetchone()
        if row is None:
            return {'enrolled': enrolled, 'state': 'missing_policy', 'provider_registered': project in self.core.governance_gate.providers}
        policy = json.loads(row['policy'])
        valid = digest(policy) == row['policy_digest'] and policy['config_fingerprint'] == config['fingerprint'] and policy['source_config'] == config['config']
        return {'enrolled': enrolled, 'state': 'current' if valid else 'invalid_binding',
                'provider_registered': project in self.core.governance_gate.providers,
                'contract_version': policy['contract_version'], 'contract_digest': policy['contract_digest']}

    def _recovery(self, c, projects, now):
        if self.controller is None:
            return {'state': 'not_connected', 'reconciliation_required': False, 'automatic_replay': False, 'actions': []}
        marks = ','.join('?' for _ in projects)
        rows = c.execute('SELECT action_key,task_id,project,phase,state,reason,updated FROM continuous_actions WHERE project IN ('+marks+') ORDER BY updated,action_key LIMIT 101', projects).fetchall()
        total = c.execute('SELECT count(*) FROM continuous_actions WHERE project IN ('+marks+')', projects).fetchone()[0]
        leader = c.execute('SELECT projects_digest,expires,epoch FROM continuous_leader WHERE singleton=1').fetchone()
        relevant = bool(leader and leader['projects_digest'] == digest(sorted(projects)))
        expired = bool(relevant and leader['expires'] <= now)
        actions = [{**dict(row), 'reconciliation_required': row['state'] == 'uncertain' or (row['state'] == 'running' and (expired or not relevant))} for row in rows[:100]]
        unsafe = c.execute("SELECT count(*) FROM continuous_actions WHERE project IN ("+marks+") AND (state='uncertain' OR (state='running' AND ?))", (*projects, expired or not relevant)).fetchone()[0]
        required = expired or bool(unsafe)
        return {'state': 'reconciliation_required' if required else 'observed', 'leader_state': 'expired' if expired else ('live' if relevant else 'unobserved'),
                'reconciliation_required': required, 'automatic_replay': False, 'actions': actions, 'action_count': total, 'truncated': total > 100}

    def plan(self, token, projects):
        if type(projects) not in (list, tuple) or not 1 <= len(projects) <= 100 or any(type(p) is not str for p in projects) or len(set(projects)) != len(projects):
            raise ValueError('explicit unique bounded projects required')
        for project in projects:
            name(project)
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            configs = {p: self.core._config(c, token, p, ('owner', 'manager')) for p in projects}
            now, results = self.store._now(), []
            counts_cache = {}
            for project, config in configs.items():
                members = []
                member_total = c.execute('SELECT count(*) FROM hierarchy_members WHERE project=?', (project,)).fetchone()[0]
                for row in c.execute('SELECT member_id,role,agent_id FROM hierarchy_members WHERE project=? ORDER BY role,member_id LIMIT 100', (project,)):
                    try:
                        self.core._member(c, project, row['member_id'], row['role'], config)
                        state = 'active'
                    except PermissionDenied:
                        state = 'credential_unavailable'
                    except Conflict:
                        state = 'binding_changed'
                    busy = bool(c.execute("SELECT 1 FROM hierarchy_tasks WHERE project=? AND state!='accepted' AND "+('developer_id=?' if row['role']=='developer' else 'reviewer_id=?')+" LIMIT 1", (project, row['member_id'])).fetchone()) if row['role'] != 'taesan' else False
                    members.append({**dict(row), 'state': state, 'busy': busy, 'connection_verified': False})
                roles = {role: [m['member_id'] for m in members if m['role'] == role and m['state'] == 'active' and not m['busy']] for role in ('taesan', 'developer', 'reviewer')}
                source, qa, capacity = self._source(c, project, config, now), self._qa(c, project, config), self._capacity(c, project, counts_cache)
                reasons = ['missing_'+role for role, ids in roles.items() if not ids]
                if member_total > 100: reasons.append('routing_partial')
                if not config['config']['maintenance_enabled']: reasons.append('maintenance_disabled')
                if not capacity['available']: reasons.append('capacity_unavailable')
                if not source['fresh']: reasons.append('source_observation_unavailable')
                if not qa['enrolled'] or qa['state'] != 'current' or not qa['provider_registered']: reasons.append('qa_unavailable')
                hosts = []
                host_total = template_total = 0
                if self._table(c, 'fleet_hosts'):
                    host_total = c.execute('SELECT count(*) FROM fleet_hosts WHERE project=?', (project,)).fetchone()[0]
                    hosts = [{'host_ref': row['host_ref'], 'binding_current': row['config_fingerprint'] == config['fingerprint']} for row in c.execute('SELECT host_ref,config_fingerprint FROM fleet_hosts WHERE project=? ORDER BY host_ref LIMIT 100', (project,))]
                templates = []
                if self._table(c, 'fleet_templates'):
                    template_total = c.execute('SELECT count(*) FROM fleet_templates WHERE project=?', (project,)).fetchone()[0]
                    templates = [{'template_ref': row['template_ref'], 'host_ref': row['host_ref'], 'max_sessions': row['max_sessions'], 'current': row['config_fingerprint'] == config['fingerprint'] and row['expires'] > now} for row in c.execute('SELECT template_ref,host_ref,max_sessions,config_fingerprint,expires FROM fleet_templates WHERE project=? ORDER BY template_ref LIMIT 100', (project,))]
                if (host_total > 100 or template_total > 100) and 'routing_partial' not in reasons: reasons.append('routing_partial')
                backlog = {row['state']: row['amount'] for row in c.execute('SELECT state,count(*) amount FROM bridge_tasks WHERE project=? GROUP BY state', (project,))}
                results.append({'project': project, 'members': members, 'member_total': member_total, 'members_truncated': member_total > 100, 'eligible_roles': roles, 'capacity': capacity, 'backlog': backlog,
                                'source': source, 'qa': qa, 'fleet_hosts': hosts, 'fleet_host_total': host_total, 'fleet_hosts_truncated': host_total > 100, 'fleet_templates': templates, 'fleet_template_total': template_total, 'fleet_templates_truncated': template_total > 100, 'ready': not reasons, 'reasons': reasons})
            recovery = self._recovery(c, tuple(projects), now)
            if recovery['reconciliation_required']:
                affected = {a['project'] for a in recovery['actions'] if a['reconciliation_required']}
                for project_result in results:
                    if recovery.get('truncated') or recovery['leader_state'] == 'expired' or project_result['project'] in affected:
                        project_result['ready'] = False
                        project_result['reasons'].append('reconciliation_required')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c, {'projects': results, 'recovery': recovery})
        return {'projects': results, 'recovery': recovery, 'read_only': True, 'execution_started': False,
                'business_approval': False, 'operational_authorization': False, 'existing_ui_chat_control_verified': False,
                'persistent_host_health': 'unmeasured'}


Routing = GovernanceRouting
