"""One bounded owner report of approved standards, unfinished work and review gaps.

Recorded mechanical evidence never establishes business acceptance or equal
quality between workers. No tester engine, test generation or execution exists.
"""
import json
import math
from .store import Conflict,PermissionDenied,NotFound,digest
from .three_tier import ThreeTier

PIN_FIELDS=('version','digest','revision','config_fingerprint','qa_policy_digest')


def _fresh(now,stamp,max_age):
    return type(stamp) in (int,float) and math.isfinite(stamp) and type(max_age) in (int,float) and math.isfinite(max_age) and 0<=now-stamp<=max_age


class QualityDeliveryReport:
    def __init__(self,core,baselines=None):
        if not isinstance(core,ThreeTier):raise ValueError('registered core required')
        if baselines is not None:
            from .business_baselines import BusinessBaselines
            if not isinstance(baselines,BusinessBaselines) or baselines.core is not core or baselines.store is not core.store:
                raise ValueError('same registered business baseline service required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.baselines=baselines

    def _scope(self,c,token,projects):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit bounded quality scope required')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        return {p:self.registry._get(c,token,p) for p in projects}

    def _baseline(self,token,project):
        if self.baselines is None:return {'project':project,'state':'not_configured','version':None,'digest':None,'revision':0,'config_fingerprint':None,'qa_policy_digest':None,'contract':{}}
        return self.baselines.current(token,project)

    @staticmethod
    def _baseline_public(value):
        contract=value.get('contract') or {};requirements=contract.get('requirements',[]);references=contract.get('references',[])
        displayed=[];text_partial=False
        for requirement in requirements[:20]:
            criterion=requirement['criterion'];text_partial|=len(criterion)>2000
            displayed.append({'id':requirement['id'],'category':requirement['category'],'priority':requirement['priority'],
                'criterion':criterion[:2000],'criterion_partial':len(criterion)>2000,
                'scenario_ids':requirement['scenario_ids'][:100],'scenario_ids_partial':len(requirement['scenario_ids'])>100,
                'business_delivery_verified':False,'evidence_state':'not_independently_verified'})
        refs=[{'reference':row['reference'],'sha256':row['sha256'],
               'description':row['description'],
               'reference_materialization_pending':True,'body_delivered_to_workers':False} for row in references[:10]]
        partial=len(requirements)>20 or len(references)>10 or text_partial or any(row['scenario_ids_partial'] for row in displayed)
        return {key:value.get(key) for key in ('state',*PIN_FIELDS)}|{
            'requirements':displayed,'references':refs,
            'requirements_window':{'limit':20,'total':len(requirements),'partial':len(requirements)>20 or text_partial},
            'references_window':{'limit':10,'total':len(references),'partial':len(references)>10},
            'preserve_requirements_count':len(contract.get('preserve',[])),'preserve':contract.get('preserve',[])[:20],
            'preservation_verified':False,'reference_materialization_pending':bool(references),'details_partial':partial}

    def report(self,token,projects,*,limit=20,cursor=None,freshness_limit=3):
        if type(limit) is not int or not 1<=limit<=100 or type(freshness_limit) is not int or not 0<=freshness_limit<=3:
            raise ValueError('bounded quality task/freshness window required')
        with self.store._transaction() as c:configs=self._scope(c,token,projects)
        before={p:self._baseline(token,p) for p in projects}
        # One globally bounded source/QA observation pass. Baseline/role metadata
        # is re-read afterwards so later host callbacks cannot age earlier facts.
        overview=self.core.overview(token,projects,limit=limit,cursor=cursor,freshness_limit=freshness_limit)
        current={p:self._baseline(token,p) for p in projects}
        by_project={row['project']:row for row in overview['projects']};results=[]
        summary={'approved_baselines':0,'baseline_attention':0,'unfinished_recorded':0,
                 'technical_acceptance_current_in_window':0,'acceptance_history_unverified_in_window':0,
                 'review_gaps_in_window':0,'review_pending_in_window':0,'baseline_pin_gaps_in_window':0}
        any_partial=False
        with self.store._transaction() as c:
            final=self._scope(c,token,projects);now=self.store._now()
            if any(final[p]['fingerprint']!=configs[p]['fingerprint'] for p in projects):raise Conflict('quality report configuration changed')
            for p in projects:
                baseline=current[p]
                # current() is a durable read; recheck its revision/digest inside
                # the final transaction when the registered implementation offers it.
                if self.baselines is not None:
                    latest=self.baselines._current(c,token,p)
                    if any(latest.get(key)!=baseline.get(key) for key in ('state',*PIN_FIELDS)):
                        raise Conflict('business baseline changed during quality report')
                baseline_changed=any(before[p].get(key)!=baseline.get(key) for key in ('state',*PIN_FIELDS))
                public=self._baseline_public(baseline);any_partial|=public['details_partial']
                if baseline['state']=='approved':summary['approved_baselines']+=1
                else:summary['baseline_attention']+=1
                members=c.execute('SELECT member_id,role FROM hierarchy_members WHERE project=? ORDER BY member_id LIMIT 201',(p,)).fetchall()
                roles={'taesan':0,'developer':0,'reviewer':0}
                for member in members[:200]:
                    try:
                        self.core._member(c,p,member['member_id'],member['role'],final[p]);roles[member['role']]+=1
                    except (PermissionDenied,Conflict,NotFound):pass
                roles_partial=len(members)>200;any_partial|=roles_partial
                policy=c.execute('SELECT * FROM governance_qa_policies WHERE project=?',(p,)).fetchone()
                policy_body=json.loads(policy['policy']) if policy else None
                policy_current=bool(policy and digest(policy_body)==policy['policy_digest'] and policy_body['config_fingerprint']==final[p]['fingerprint'] and policy_body['source_config']==final[p]['config'])
                qa={'policy_current':policy_current,'provider_registered':p in self.core.governance_gate.providers,
                    'scope':policy_body['scope'] if policy_current else 'unavailable',
                    'actual_business_qa_verified':False}
                counts={r['state']:r['amount'] for r in c.execute('SELECT state,COUNT(*) amount FROM bridge_tasks WHERE project=? GROUP BY state',(p,))}
                unassigned=c.execute('SELECT COUNT(*) FROM bridge_tasks t LEFT JOIN hierarchy_tasks h ON h.task_id=t.id WHERE t.project=? AND t.state!=? AND h.task_id IS NULL',(p,'cancelled')).fetchone()[0]
                unfinished=c.execute("SELECT COUNT(*) FROM bridge_tasks t LEFT JOIN hierarchy_tasks h ON h.task_id=t.id WHERE t.project=? AND t.state!='cancelled' AND (h.state IS NULL OR h.state!='accepted')",(p,)).fetchone()[0]
                summary['unfinished_recorded']+=unfinished
                tasks=[]
                for task in by_project[p]['tasks']:
                    raw=c.execute('SELECT approval,manifest FROM bridge_tasks WHERE id=?',(task['task_id'],)).fetchone()
                    approval=json.loads(raw['approval']) if raw['approval'] else {};manifest=json.loads(raw['manifest'])
                    pin=approval.get('business_baseline');pin_current=baseline['state']=='approved' and type(pin) is dict and all(pin.get(key)==baseline.get(key) for key in PIN_FIELDS)
                    assessment=task['code_assessment']
                    review_recorded=bool(task['reviewer_id'] and task['checks'] and (manifest['kind']!='development' or type(assessment) is dict))
                    review_valid=False
                    if review_recorded:
                        try:
                            reviewer=self.core._member(c,p,task['reviewer_id'],'reviewer',final[p])
                            developer=self.core._member(c,p,task['developer_id'],'developer',final[p])
                            checks_valid=all(type(check) is dict and type(check.get('name')) is str and type(check.get('exit_code')) is int and check['exit_code']==0 for check in task['checks'])
                            names_match=checks_valid and sorted(check['name'] for check in task['checks'])==sorted(manifest['checks'])
                            code_valid=manifest['kind']!='development' or (type(task['candidate_digest']) is str and assessment.get('recommendation')=='pass' and assessment.get('candidate_digest')==task['candidate_digest'] and assessment.get('reviewer_subject')==reviewer['subject'])
                            review_valid=checks_valid and names_match and code_valid and reviewer['subject']!=developer['subject']
                        except (PermissionDenied,Conflict,NotFound):pass
                    review_gap=task['state'] in ('review_passed','accepted') and not review_valid
                    core_current=task['state']=='accepted' and task['technical_accepted'] is True and review_valid and task['source_freshness']=='checked' and not baseline_changed
                    qa_receipt=task.get('qa_admission');qa_scope=qa_receipt.get('scope','not_measured') if type(qa_receipt) is dict else 'not_measured'
                    if core_current and policy_current:
                        stamp=qa_receipt.get('observed_at') if type(qa_receipt) is dict else None
                        if not _fresh(now,stamp,policy_body['max_age_seconds']):core_current=False
                    gaps=[]
                    if baseline['state']!='approved':gaps.append('business_baseline_'+baseline['state'])
                    elif not pin_current:gaps.append('business_baseline_pin_missing_or_changed')
                    if baseline_changed:gaps.append('baseline_changed_during_collection')
                    if not review_recorded:gaps.append('independent_review_missing' if review_gap else 'independent_review_not_completed')
                    elif not review_valid:gaps.append('independent_review_invalid_or_authority_changed')
                    if not qa['policy_current']:gaps.append('qa_policy_unavailable_or_stale')
                    if not qa['provider_registered']:gaps.append('external_qa_provider_unavailable')
                    if qa_scope=='synthetic_only':gaps.append('synthetic_qa_is_not_business_acceptance')
                    elif qa_scope=='not_measured':gaps.append('qa_admission_not_recorded')
                    if task['acceptance_recorded'] and not core_current:gaps.append('acceptance_currentness_not_verified')
                    if core_current:summary['technical_acceptance_current_in_window']+=1
                    if task['acceptance_recorded'] and not core_current:summary['acceptance_history_unverified_in_window']+=1
                    if review_gap:summary['review_gaps_in_window']+=1
                    elif not review_recorded:summary['review_pending_in_window']+=1
                    if not pin_current:summary['baseline_pin_gaps_in_window']+=1
                    tasks.append({'task_id':task['task_id'],'state':task['state'],'version':task['version'],
                        'manifest_digest':task['manifest_digest'],'approved_baseline_pin_current':pin_current,
                        'approved_baseline_version':pin.get('version') if type(pin) is dict else None,
                        'core_technical_acceptance_current':core_current,'acceptance_recorded':task['acceptance_recorded'],
                        'source_freshness':task['source_freshness'],'independent_review_recorded':review_recorded,'independent_review_binding_current':review_valid,
                        'qa_evidence_scope':qa_scope,'gaps':gaps,'business_acceptance_verified':False})
                source={'state':'not_checked','fresh':False}
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_source_heads'").fetchone():
                    head=c.execute('SELECT * FROM manager_source_heads WHERE project=?',(p,)).fetchone()
                    if head:
                        fresh=head['config_fingerprint']==final[p]['fingerprint'] and _fresh(now,head['observed_at'],300)
                        source={'state':'observed' if fresh else 'stale','fresh':fresh,'head_sha':head['sha']}
                    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_source_observation_status'").fetchone():
                        status=c.execute('SELECT * FROM manager_source_observation_status WHERE project=?',(p,)).fetchone()
                        if status and (status['config_fingerprint']!=final[p]['fingerprint'] or status['state'] not in ('baseline','unchanged','changed') or not _fresh(now,status['checked_at'],300)):
                            source={'state':'unavailable','fresh':False}
                results.append({'project':p,'source':source,'baseline':public,'baseline_changed_during_collection':baseline_changed,
                    'active_role_counts':roles,'roles_window':{'limit':200,'partial':roles_partial},'qa':qa,
                    'recorded_task_state_counts':counts,'unfinished_recorded':unfinished,'unassigned_recorded':unassigned,
                    'tasks':tasks,'task_window_partial':by_project[p]['tasks_truncated'],
                    'reference_materialization_pending':public['reference_materialization_pending']})
            result={'projects':results,'summary':summary,'task_window':{'limit':limit,'displayed':overview['displayed_tasks'],
                'total_admitted_recorded':overview['total_tasks'],'partial':overview['truncated'],
                'next_cursor':overview['next_cursor'],'freshness_checks':overview['freshness_checks'],
                'scope':'admitted_tasks_only_unassigned_counts_separate'},
                'report_details_partial':any_partial,'observed_at':now,'summary_complete':not any_partial and not overview['truncated'],
                'summary_scope':'bounded_quality_evidence_metadata','unfinished_scope':'stored_workflow_state_not_business_completion','business_delivery_status':'not_independently_verified','reference_bodies_delivered_to_workers':False,'quality_score':None,'worker_quality_equivalence_verified':False,'business_acceptance_verified':False,
                'operational_authorization':False,'messages_sent':False,'tester_engine_started':False}
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,result)
            return result
