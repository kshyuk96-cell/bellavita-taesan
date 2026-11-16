"""Task-bound approved criteria shared by developer and independent reviewer.

Packets contain scope constraints and references, never acceptance, tool authority,
external fetches or model-quality guarantees. Reference bodies stay unmaterialized.
"""
import json
import re
from .agent_directory import _reject_known_tokens
from .artifact_content import ArtifactContent
from .proposal_board import _source
from .store import Conflict,PermissionDenied,_manifest,_task_id,canonical_json,digest
from .three_tier import ThreeTier


class WorkerQualityHandoff:
    def __init__(self,core):
        if not isinstance(core,ThreeTier):raise ValueError('registered hierarchy core required')
        self.core,self.store,self.registry=core,core.store,core.registry

    @staticmethod
    def _safe(c,token,value):
        _reject_known_tokens(c,value)
        if len(canonical_json(value).encode())>256000:raise ValueError('bounded quality handoff required')
        def strings(item):
            if isinstance(item,str):yield item
            elif isinstance(item,dict):
                for key,child in item.items():yield from strings(key);yield from strings(child)
            elif isinstance(item,(list,tuple)):
                for child in item:yield from strings(child)
        for item in strings(value):
            ArtifactContent._scan(c,token,item.encode())
        return value

    def _task(self,c,token,task_id):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        task=self.store._get(c,actor,task_id);config=self.registry._get(c,token,task['project'])
        manifest=_manifest(json.loads(task['manifest']));approval=json.loads(task['approval'] or '{}')
        if (task['manifest_digest']!=digest(manifest) or approval.get('version')!=task['version']
                or approval.get('manifest_digest')!=task['manifest_digest']
                or task['state'] in ('pending_approval','cancelled','needs_verification')):
            raise Conflict('exact current owner-approved task required for quality handoff')
        return task,config,manifest,approval

    def _recipient(self,c,task,config,role,candidate_digest):
        row=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()
        recipient={'role':role,'member_id':None,'subject':None,'state':'unassigned'};candidate=None
        if row:
            if (row['version']!=task['version'] or row['manifest_digest']!=task['manifest_digest']
                    or row['config_fingerprint']!=config['fingerprint']):raise Conflict('managed handoff task binding changed')
            member_id=row['developer_id'] if role=='developer' else row['reviewer_id']
            if member_id:
                member=self.core._member(c,task['project'],member_id,role,config)
                recipient.update(member_id=member_id,subject=member['subject'],state='registered')
            if role=='reviewer':
                if not row['candidate']:raise Conflict('exact frozen reviewer candidate required')
                candidate=json.loads(row['candidate']);identity=candidate.get('identity')
                if (candidate.get('digest')!=candidate_digest or type(identity) is not dict or digest(identity)!=candidate_digest
                        or identity.get('task_id')!=task['id'] or identity.get('version')!=task['version']
                        or identity.get('manifest_digest')!=task['manifest_digest'] or identity.get('config_fingerprint')!=config['fingerprint']
                        or candidate.get('config_fingerprint')!=config['fingerprint']
                        or candidate.get('developer_subject')!=identity.get('developer_subject')
                        or candidate.get('task',{}).get('version')!=task['version']
                        or identity.get('report_digest')!=digest(json.loads(task['report']))
                        or candidate.get('task',{}).get('id')!=task['id']
                        or candidate.get('task',{}).get('manifest_digest')!=task['manifest_digest']):
                    raise Conflict('reviewer candidate identity differs from approved task')
                source=candidate.get('source',{})
                if (source.get('base_sha')!=json.loads(task['manifest'])['base_sha']
                        or identity.get('changed_paths')!=list(source.get('changed_paths',[]))
                        or any(identity.get(key)!=source.get(key) for key in ('base_sha','head_sha','artifact_digest','checks_digest','runtime_digest'))):
                    raise Conflict('reviewer source differs from frozen candidate identity')
                if recipient['subject'] is not None and recipient['subject']==candidate.get('developer_subject'):
                    raise PermissionDenied('independent reviewer required')
        elif role=='reviewer':raise Conflict('managed reviewer candidate required')
        return recipient,candidate

    def generate(self,token,task_id,role,candidate_digest=None):
        """Generate inert criteria/QA metadata for a trusted host's actual prompt.

        Owner/manager scope is required. Worker claim/lease and live source checks
        remain in the existing transport; the returned digest is not a signature.
        """
        _task_id(task_id)
        if role not in ('developer','reviewer'):raise ValueError('developer or reviewer handoff role required')
        if role=='developer' and candidate_digest is not None:raise ValueError('developer handoff does not accept a candidate identity')
        if role=='reviewer' and (type(candidate_digest) is not str or not re.fullmatch('[a-f0-9]{64}',candidate_digest)):raise ValueError('exact reviewer candidate digest required')
        from .business_baselines import validate_task,qa_projection
        with self.store._transaction() as c:
            task,config,manifest,approval=self._task(c,token,task_id)
            baseline=validate_task(c,self.store,task)
            task_binding={'task_id':task['id'],'project':task['project'],'version':task['version'],
                          'manifest_digest':task['manifest_digest'],'config_fingerprint':config['fingerprint']}
            if baseline is None:
                return self._safe(c,token,{'schema_version':'worker-quality-handoff/v1','state':'not_bound','task':task_binding,'role':role,
                                           'business_baseline':None,'requirements':[],'references':[],'quality_equivalence_guaranteed':False,
                                           'execution_authorized':False,'technical_acceptance':False,'operational_authorized':False})
            self.store._validate_execution(c,task,check_source_age=True)
            policy=c.execute('SELECT * FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
            if not policy:raise Conflict('exact owner-approved path policy required')
            execution_policy=json.loads(policy['policy'])
            if (digest(execution_policy)!=policy['policy_digest'] or approval.get('policy_digest')!=policy['policy_digest']
                    or execution_policy.get('version')!=task['version'] or execution_policy.get('manifest_digest')!=task['manifest_digest']):
                raise Conflict('quality handoff execution policy changed')
            qa_row=c.execute('SELECT * FROM governance_qa_policies WHERE project=?',(task['project'],)).fetchone()
            if not qa_row:raise Conflict('current manager-owned QA policy required')
            qa_policy=json.loads(qa_row['policy'])
            if (digest(qa_policy)!=qa_row['policy_digest'] or qa_row['policy_digest']!=baseline['qa_policy_digest']
                    or qa_policy.get('config_fingerprint')!=config['fingerprint'] or qa_policy.get('source_config')!=config['config']):
                raise Conflict('quality handoff QA/source configuration changed')
            source=_source(c,task['project'],config,self.store._now())
            recipient,candidate=self._recipient(c,task,config,role,candidate_digest)
            contract=baseline['contract'];expected=qa_policy['expected_contract'];references=[]
            if digest(expected)!=qa_policy['contract_digest']:raise Conflict('manager-owned QA contract digest changed')
            for requirement in contract['requirements']:
                if any(sid not in expected['scenarios'] for sid in requirement['scenario_ids']):raise Conflict('quality requirement scenario differs from manager-owned QA contract')
            for reference in contract['references']:
                row=c.execute('SELECT * FROM artifact_content_records WHERE reference=?',(reference['reference'],)).fetchone()
                if (not row or row['project']!=task['project'] or row['config']!=config['fingerprint']
                        or row['sha256']!=reference['sha256'] or row['expires']<=self.store._now()):
                    raise Conflict('approved reference artifact is unavailable or changed')
                references.append({**reference,'materialized':False,'body_transferred':False,'visual_validation':'not_measured'})
            identity={key:baseline[key] for key in ('version','digest','revision','config_fingerprint','qa_policy_digest')}
            projected=qa_projection(contract,qa_policy)
            mapped_ids=sorted({scenario for requirement in contract['requirements'] for scenario in requirement['scenario_ids']})
            if set(projected.get('scenarios',{}))!=set(mapped_ids):raise Conflict('quality projection must retain every mapped requirement scenario')
            criteria={'requirements':contract['requirements'],'preserve':contract['preserve'],'references':references,
                      'expected_contract':projected,'required_coverage':qa_policy['required_coverage']}
            binding={'task':task_binding,'business_baseline':identity,'role':role,'recipient':recipient,
                     'source':source,'policy_digest':policy['policy_digest'],'candidate_digest':candidate_digest,
                     'candidate_source':candidate['source'] if candidate else None,'criteria_digest':digest(criteria)}
            result={'schema_version':'worker-quality-handoff/v1','state':'bound','task':task_binding,'role':role,'recipient':recipient,
                    'manifest':manifest,'execution_policy':execution_policy,'business_baseline':identity,
                    **criteria,'qa_contract':{'version':qa_policy['contract_version'],'digest':qa_policy['contract_digest'],
                        'policy_digest':qa_row['policy_digest'],'scope':qa_policy['scope'],'admission_result_included':False,
                        'projection_digest':digest(projected),'is_full':projected==expected,'mapped_ids':mapped_ids},
                    'source_binding':source,'candidate_digest':candidate_digest,'criteria_digest':digest(criteria),'binding_digest':digest(binding),
                    'binding_digest_is_signature':False,'reference_materialized':False,'reference_delivery_pending':bool(references),
                    'visual_validation':'not_measured','candidate_live_recheck_required':role=='reviewer',
                    'content_authority':'approved_scope_constraints_only','quality_equivalence_guaranteed':False,
                    'execution_authorized':False,'technical_acceptance':False,'operational_authorized':False}
            # Pure DB checks share one transaction; recheck authority at the return boundary.
            current,latest,_,_=self._task(c,token,task_id)
            if (latest['fingerprint']!=config['fingerprint'] or current['version']!=task['version']
                    or validate_task(c,self.store,current)!=baseline):raise Conflict('quality handoff changed during generation')
            return self._safe(c,token,result)
