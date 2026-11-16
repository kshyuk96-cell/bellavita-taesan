"""Metadata-only artifact receipts and one durable, grouped operations report."""
from dataclasses import dataclass,field
import hashlib
import json
import math
import secrets
from .store import Conflict,NotFound,canonical_json,digest
from .maintenance_registry import name
from .three_tier import ThreeTier
from .control_digest import ControlDigest
from .continuous_manager import ContinuousManager
from .governance_durability import GovernanceDurability

KINDS=frozenset({'candidate','review_evidence','qa_report','backup_manifest','diagnostic'})
MAX_ARTIFACT_BYTES=8*1024*1024


@dataclass(frozen=True)
class ArtifactObservation:
    project: str
    kind: str
    artifact_key: str
    data: bytes=field(repr=False)
    observed_at: float

    def __post_init__(self):
        name(self.project);name(self.artifact_key)
        if type(self.kind) is not str or self.kind not in KINDS or type(self.data) is not bytes or len(self.data)>MAX_ARTIFACT_BYTES:
            raise ValueError('bounded observed artifact bytes required')
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('actual artifact observation time required')


class OperationsReporting:
    def __init__(self,core,control_digest,*,controller=None,durability=None,artifact_sources=None,budget=None):
        if not isinstance(core,ThreeTier) or not isinstance(control_digest,ControlDigest) or control_digest.core is not core:
            raise ValueError('same registered core and digest required')
        if controller is not None and (not isinstance(controller,ContinuousManager) or controller.core is not core):
            raise ValueError('same registered continuous manager required')
        if durability is not None and (not isinstance(durability,GovernanceDurability) or durability.core is not core):
            raise ValueError('same registered durability service required')
        if budget is not None:
            from .operating_budget import OperatingBudget
            if not isinstance(budget,OperatingBudget) or budget.core is not core:raise ValueError('same registered admission budget required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.control_digest,self.controller=control_digest,controller
        self.durability=durability or GovernanceDurability(core)
        self.budget=budget
        self.artifact_sources=dict(artifact_sources or {})
        for (project,kind),observer in self.artifact_sources.items():
            name(project)
            if kind not in KINDS or not callable(observer):raise ValueError('trusted artifact source required')
        with self.store._transaction() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS operations_artifacts (
                ref TEXT PRIMARY KEY,project TEXT,kind TEXT,artifact_key TEXT,config TEXT,sha256 TEXT,
                size_bytes INTEGER,observed_at REAL,expires REAL,UNIQUE(project,kind,artifact_key,config));
                CREATE TABLE IF NOT EXISTS operations_grouped_reports (
                id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,scope TEXT,configs TEXT,payload TEXT,created REAL,
                UNIQUE(actor,request_key));
                CREATE TABLE IF NOT EXISTS operations_report_cursors (
                id TEXT PRIMARY KEY,report_id TEXT,actor TEXT,binding TEXT,offset INTEGER,expires REAL);''')

    def _scope(self,c,token,projects):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit bounded projects required')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        configs={p:self.registry._get(c,token,p)['fingerprint'] for p in projects}
        return actor,configs

    def _recheck(self,token,projects,configs):
        with self.store._transaction() as c:
            _,current=self._scope(c,token,projects)
            if current!=configs:raise Conflict('operations reporting configuration changed')

    @staticmethod
    def _artifact(row,now):
        return {'reference':row['ref'],'project':row['project'],'kind':row['kind'],'sha256':row['sha256'],
                'size_bytes':row['size_bytes'],'observed_at':row['observed_at'],'expires_at':row['expires'],
                'state':'expired' if row['expires']<=now else 'retained_metadata',
                'retrieval':'metadata_only','physical_deletion_verified':False,'acceptance_authorized':False}

    def register_artifact(self,token,project,kind,artifact_key,*,retention_seconds=86400):
        name(project);name(artifact_key)
        if type(kind) is not str or kind not in KINDS or type(retention_seconds) is not int or not 60<=retention_seconds<=30*86400:
            raise ValueError('bounded artifact kind and retention required')
        with self.store._transaction() as c:_,configs=self._scope(c,token,[project])
        observer=self.artifact_sources.get((project,kind))
        if observer is None:raise NotFound('trusted artifact source not configured')
        # An opaque source key is never treated as a host path or uploaded proof.
        observed=observer(project,artifact_key)
        if not isinstance(observed,ArtifactObservation) or (observed.project,observed.kind,observed.artifact_key)!=(project,kind,artifact_key):raise ValueError('exact trusted artifact observation required')
        sha=hashlib.sha256(observed.data).hexdigest();size=len(observed.data)
        with self.store._transaction() as c:
            _,current=self._scope(c,token,[project]);now=self.store._now()
            if current!=configs:raise Conflict('artifact source configuration changed')
            if not 0<=now-observed.observed_at<=300:raise Conflict('fresh artifact observation required')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,{'project':project,'kind':kind,'artifact_key':artifact_key})
            old=c.execute('SELECT * FROM operations_artifacts WHERE project=? AND kind=? AND artifact_key=? AND config=?',(project,kind,artifact_key,configs[project])).fetchone()
            if old:
                if (old['sha256'],old['size_bytes'])!=(sha,size):raise Conflict('artifact reference is immutable')
                return self._artifact(old,now)
            ref=secrets.token_hex(16)
            c.execute('INSERT INTO operations_artifacts VALUES (?,?,?,?,?,?,?,?,?)',(ref,project,kind,artifact_key,configs[project],sha,size,observed.observed_at,observed.observed_at+retention_seconds))
            return self._artifact(c.execute('SELECT * FROM operations_artifacts WHERE ref=?',(ref,)).fetchone(),now)

    def get_artifact(self,token,reference):
        name(reference)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            row=c.execute('SELECT * FROM operations_artifacts WHERE ref=?',(reference,)).fetchone()
            if row is None:raise NotFound('artifact metadata unavailable')
            _,configs=self._scope(c,token,[row['project']])
            if row['config']!=configs[row['project']]:raise Conflict('artifact configuration changed')
            return self._artifact(row,self.store._now())

    def _collect(self,token,projects,configs):
        value=self.control_digest.report(token,projects)
        self._recheck(token,projects,configs)
        continuous=self.controller.status(token,projects,limit=100) if self.controller is not None else None
        self._recheck(token,projects,configs)
        notices=self.durability.list_notices(token,projects)
        self._recheck(token,projects,configs)
        admissions=self.budget.report(token,projects) if self.budget is not None else None
        self._recheck(token,projects,configs)
        groups={}
        def add(project,category,urgency,count):
            if count:
                key=(project or '',category)
                groups[key]={'project':project,'category':category,'urgency':urgency,'count':groups.get(key,{}).get('count',0)+count}
        for project in value['project_summaries']:
            add(project['project'],'development_blocked','normal',project['checks']['blocked'])
            add(project['project'],'observation_missing','normal',project['checks']['unknown'])
        add(None,'owner_decision_required','normal',value['summary']['owner_decisions_in_collected_window'])
        if continuous:
            for action in continuous['actions']:
                if action['state']=='uncertain':add(action['project'],'uncertain_execution','urgent',1)
        for notice in notices['items']:
            if notice['kind']=='credential_refresh_required':add(notice['project'],'credential_attention','urgent',1)
            else:add(notice['project'],'credential_observation_missing','normal',1)
        if admissions is not None:
            for project in admissions['projects']:
                if project.get('configured') and not project.get('allowed'):
                    reason=project.get('reason','unknown')
                    urgency='urgent' if reason=='authentication_expired' else 'normal'
                    add(project['project'],'admission_'+reason,urgency,1)
        ordered=sorted(groups.values(),key=lambda group:(group['urgency']!='urgent',group['project'] or '',group['category']))
        windows={'task':value['task_window'],'batch':value['batch_window'],'decision':value['decision_window'],
                 'continuous':{'configured':continuous is not None,'partial':continuous['truncated'] if continuous else False,'limit':100},
                 'auth_notices':{'partial':notices['truncated'],'limit':notices['limit'],'total':notices['total']}}
        partial=any(window.get('partial',False) for window in windows.values())
        return {'groups':ordered,'summary':value['summary'],'owner_decisions':value['owner_decisions'],
                'admission_budget':admissions,
                'input_windows':windows,'input_partial':partial,'input_complete':value['summary_complete'] and continuous is not None,'controller_configured':continuous is not None,
                'messages_sent':False,'per_task_messages_sent':0,'acceptance_authorized':False}

    def report(self,token,projects,request_key,after=None,limit=20):
        name(request_key)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded grouped report page required')
        if after is not None:name(after)
        scope=None
        with self.store._transaction() as c:
            actor,configs=self._scope(c,token,projects)
            scope=digest(sorted(projects))
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,{'request_key':request_key})
            saved=c.execute('SELECT * FROM operations_grouped_reports WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if saved and (saved['scope']!=scope or json.loads(saved['configs'])!=configs):raise Conflict('report request binding changed')
        if saved is None:
            if after is not None:raise NotFound('report cursor unavailable')
            payload=self._collect(token,projects,configs)
            with self.store._transaction() as c:
                current_actor,current=self._scope(c,token,projects)
                if current!=configs:raise Conflict('report configuration changed')
                _reject_known_tokens(c,payload)
                report_id=secrets.token_hex(16)
                c.execute('INSERT OR IGNORE INTO operations_grouped_reports VALUES (?,?,?,?,?,?,?)',(report_id,current_actor['token_hash'],request_key,scope,canonical_json(configs),canonical_json(payload),self.store._now()))
                saved=c.execute('SELECT * FROM operations_grouped_reports WHERE actor=? AND request_key=?',(current_actor['token_hash'],request_key)).fetchone()
        with self.store._transaction() as c:
            actor,current=self._scope(c,token,projects)
            if current!=configs or saved['scope']!=scope or json.loads(saved['configs'])!=configs:raise Conflict('report configuration changed')
            binding=digest({'scope':scope,'configs':configs,'request_key':request_key})
            offset=0;now=self.store._now()
            c.execute('DELETE FROM operations_report_cursors WHERE expires<=?',(now,))
            if after is not None:
                cursor=c.execute('SELECT * FROM operations_report_cursors WHERE id=?',(after,)).fetchone()
                if cursor is None or cursor['report_id']!=saved['id'] or cursor['actor']!=actor['token_hash'] or cursor['binding']!=binding:raise NotFound('report cursor unavailable')
                offset=cursor['offset']
            payload=json.loads(saved['payload']);groups=payload.pop('groups');page=groups[offset:offset+limit]
            more=offset+len(page)<len(groups);next_cursor=None
            if more:
                existing=c.execute('SELECT id FROM operations_report_cursors WHERE report_id=? AND actor=? AND binding=? AND offset=? AND expires>? LIMIT 1',
                    (saved['id'],actor['token_hash'],binding,offset+len(page),now)).fetchone()
                if existing:next_cursor=existing['id']
                else:
                    next_cursor=secrets.token_hex(16)
                    c.execute('DELETE FROM operations_report_cursors WHERE id IN (SELECT id FROM operations_report_cursors ORDER BY expires LIMIT MAX(0,(SELECT COUNT(*) FROM operations_report_cursors)-1023))')
                    c.execute('INSERT INTO operations_report_cursors VALUES (?,?,?,?,?,?)',(next_cursor,saved['id'],actor['token_hash'],binding,offset+len(page),now+300))
            result={**payload,'report_id':saved['id'],'observed_at':saved['created'],'snapshot_state':'stale' if now-saved['created']>300 else 'recorded',
                    'urgent':[g for g in page if g['urgency']=='urgent'],'normal':[g for g in page if g['urgency']=='normal'],
                    'group_window':{'limit':limit,'total':len(groups),'displayed':len(page),'partial':more,'next_cursor':next_cursor},
                    'summary_scope':'recorded_observation_at_timestamp','summary_complete':not more and not payload['input_partial'] and payload['input_complete']}
            _reject_known_tokens(c,result)
            return result
