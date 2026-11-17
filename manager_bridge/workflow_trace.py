"""Bounded recorded manager workflow, never fresh acceptance or execution authority."""
import json
from .store import _task_id,digest
from .three_tier import ThreeTier
from .worker_quality_handoff import WorkerQualityHandoff


class WorkflowTrace:
    def __init__(self,core):
        if not isinstance(core,ThreeTier):raise ValueError('registered hierarchy required')
        self.core,self.store,self.registry=core,core.store,core.registry

    def report(self,token,task_id,*,limit=30):
        _task_id(task_id)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded workflow window required')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            task=self.store._get(c,actor,task_id);config=self.registry._get(c,token,task['project'])
            row=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone()
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('continuous_actions','continuous_host_assignments')")}
            total=c.execute('SELECT count(*) FROM hierarchy_events WHERE task_id=?',(task_id,)).fetchone()[0]
            events=[{'id':r['id'],'kind':r['kind'],'recorded_state':r['state'],'recorded_at':r['recorded']}
                    for r in c.execute('SELECT * FROM hierarchy_events WHERE task_id=? ORDER BY id DESC LIMIT ?',(task_id,limit))]
            events.reverse();actions=[];action_count=0
            recovery={'state':'controller_history_unavailable','project_quarantined_recorded':False,
                      'project_uncertain_actions':0,'task_uncertain_actions':0,'task_running_actions':0,
                      'automatic_retry_authorized':False,'lease_expiry_allows_retry':False,
                      'processes_stopped_verified':False,'reconciliation_performed':False}
            if 'continuous_actions' in tables:
                uncertain=c.execute("SELECT count(*) FROM continuous_actions WHERE project=? AND state='uncertain'",(task['project'],)).fetchone()[0]
                task_uncertain=c.execute("SELECT count(*) FROM continuous_actions WHERE project=? AND task_id=? AND state='uncertain'",(task['project'],task_id)).fetchone()[0]
                running=c.execute("SELECT count(*) FROM continuous_actions WHERE project=? AND task_id=? AND state='running'",(task['project'],task_id)).fetchone()[0]
                recovery.update(state='manual_reconciliation_required' if uncertain else 'running_outcome_pending' if running else 'no_uncertainty_recorded',
                                project_quarantined_recorded=bool(uncertain),project_uncertain_actions=uncertain,
                                task_uncertain_actions=task_uncertain,task_running_actions=running)
                action_count=c.execute('SELECT count(*) FROM continuous_actions WHERE task_id=? AND project=?',(task_id,task['project'])).fetchone()[0]
                for action in c.execute('SELECT * FROM continuous_actions WHERE task_id=? AND project=? ORDER BY created DESC,action_key DESC LIMIT ?',(task_id,task['project'],limit)):
                    try:
                        request=json.loads(action['request'])
                        matches=(request['task_id'],request['project'],request['version'],request['manifest_digest'],request['config_fingerprint'])==(task_id,task['project'],task['version'],task['manifest_digest'],config['fingerprint'])
                    except (ValueError,KeyError,TypeError):matches=False
                    actions.append({'phase':action['phase'],'recorded_state':action['state'],'attempts':action['attempts'],
                                    'created_at':action['created'],'updated_at':action['updated'],'binding_matches_task':matches})
            edges=[]
            edge_count=c.execute('SELECT count(*) FROM hierarchy_next_assignments n JOIN bridge_tasks b ON b.id=n.next_id WHERE n.parent_id=? AND b.project=?',(task_id,task['project'])).fetchone()[0]
            for edge in c.execute('SELECT n.next_id,n.parent_digest,n.created,b.state,b.version,b.manifest_digest FROM hierarchy_next_assignments n JOIN bridge_tasks b ON b.id=n.next_id WHERE n.parent_id=? AND b.project=? ORDER BY n.created DESC,n.next_id DESC LIMIT ?',(task_id,task['project'],limit)):
                edges.append({'task_id':edge['next_id'],'current_child_recorded_state':edge['state'],'current_child_version':edge['version'],
                              'current_child_manifest_digest':edge['manifest_digest'],'recorded_at':edge['created'],
                              'parent_candidate_digest':edge['parent_digest'],'execution_verified':False})
            host=None
            if 'continuous_host_assignments' in tables:
                found=c.execute('SELECT phase,version,manifest_digest,action_key FROM continuous_host_assignments WHERE task_id=?',(task_id,)).fetchone()
                if found:
                    version_match=(found['version'],found['manifest_digest'])==(task['version'],task['manifest_digest'])
                    action=c.execute('SELECT request FROM continuous_actions WHERE action_key=? AND task_id=? AND project=?',(found['action_key'],task_id,task['project'])).fetchone() if 'continuous_actions' in tables else None
                    try:
                        saved=json.loads(action['request']) if action else {}
                        config_match=(saved.get('task_id'),saved.get('project'),saved.get('version'),saved.get('manifest_digest'),saved.get('config_fingerprint'))==(task_id,task['project'],task['version'],task['manifest_digest'],config['fingerprint'])
                    except (ValueError,TypeError,AttributeError):config_match=False
                    host={'recorded_phase':found['phase'],'task_version_manifest_match':version_match,
                          'config_binding_verified':config_match,'binding_matches_task':version_match and config_match}
            bound=row is not None and (row['project'],row['version'],row['manifest_digest'],row['config_fingerprint'])==(task['project'],task['version'],task['manifest_digest'],config['fingerprint'])
            value={'schema_version':'workflow-trace/v1','scope':'recorded_database_snapshot','task_id':task_id,'project':task['project'],
                   'version':task['version'],'manifest_digest':task['manifest_digest'],'config_fingerprint':config['fingerprint'],'observed_at':self.store._now(),
                   'recorded_task_state':task['state'],'recorded_hierarchy_state':row['state'] if row else 'unassigned',
                   'binding_matches_task':bound,'rework_count':row['rework_count'] if row else 0,
                   'events':events,'event_count':total,'events_truncated':total>len(events),
                   'actions':actions,'action_count':action_count,'actions_truncated':action_count>len(actions),
                   'controller_history_available':'continuous_actions' in tables,'action_history_scope':'selected_project_task_all_actors',
                   'host_history_scope':'current_assignment_only','host':host,'recovery':recovery,
                   'next_assignments':edges,'next_assignment_count':edge_count,'next_assignments_truncated':edge_count>len(edges),
                   'fresh_observers_invoked':False,'current_technical_acceptance_verified':False,
                   'snapshot_digest_is_signature':False,'execution_authorized':False,'operational_authorized':False,'always_on_verified':False}
            value['snapshot_digest']=digest(value)
            return WorkerQualityHandoff._safe(c,token,value)
