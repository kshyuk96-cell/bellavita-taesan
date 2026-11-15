"""Durable, read-only source-head observations from registered host connectors.

No polling daemon or remote fetch is started here. A local frozen checkout is not
an up-to-date remote observation. Events only notify a manager; acknowledgements
never approve work, alter source, requeue tasks or invalidate running leases.
"""
from dataclasses import dataclass
import json
import math
from .agent_directory import _reject_known_tokens
from .connection_readiness import SourcePin
from .delegation import SourceProof
from .maintenance_registry import name, receipt
from .store import Conflict, NotFound, PermissionDenied, _reject_credentials, digest


@dataclass(frozen=True)
class SourceHead:
    project: str
    source_kind: str
    source_ref: str
    branch: str
    sha: str
    revision: str
    reference: str
    observed_at: float

    def __post_init__(self):
        SourcePin(self.project,self.source_kind,self.source_ref,self.branch,self.sha,self.revision)
        receipt(self.reference)
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('actual source observation time required')


@dataclass(frozen=True)
class HeadProbe:
    source_kind: str
    source_ref: str
    branch: str
    observe: object
    read_only: bool = True

    def __post_init__(self):
        if self.source_kind not in ('github','sites') or not callable(self.observe) or self.read_only is not True:
            raise ValueError('registered read-only head observer required')
        receipt(self.source_ref);receipt(self.branch)


class SourceWatch:
    def __init__(self,store,registry,probes=None,*,max_age=300):
        if registry.store is not store:raise ValueError('shared registry required')
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('bounded observation age required')
        self.store,self.registry,self.max_age=store,registry,max_age
        self.probes=dict(probes or {})
        for project,probe in self.probes.items():
            name(project)
            if not isinstance(probe,HeadProbe):raise ValueError('typed registered head probe required')
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS manager_source_heads (
              project TEXT PRIMARY KEY,config_fingerprint TEXT,sha TEXT,revision TEXT,
              reference TEXT,observed_at REAL,sequence INTEGER);
            CREATE TABLE IF NOT EXISTS manager_source_changes (
              id TEXT PRIMARY KEY,project TEXT,config_fingerprint TEXT,previous_sha TEXT,
              head_sha TEXT,revision TEXT,reference TEXT,observed_at REAL,sequence INTEGER,
              acknowledged_at REAL,UNIQUE(project,config_fingerprint,sequence));
            ''')

    @staticmethod
    def _binding(config):
        return tuple(config[key] for key in ('source_kind','source_ref','branch'))

    def _collect(self,project,config,*,token=None):
        probe=self.probes.get(project)
        if probe is None:return 'not_configured',None
        if (probe.source_kind,probe.source_ref,probe.branch)!=self._binding(config):
            raise Conflict('registered source observer scope changed')
        try:
            observed=probe.observe(project)
        except PermissionDenied:
            return 'permission_denied',None
        except Exception:
            return 'unavailable',None
        if not isinstance(observed,SourceHead):return 'unknown',None
        if observed.project!=project or self._binding(observed.__dict__)!=self._binding(config):
            return 'unknown',None
        if not 0<=self.store._now()-observed.observed_at<=self.max_age:return 'stale',None
        if token is not None:
            try:_reject_credentials(observed.__dict__,token)
            except ValueError:return 'unknown',None
        return 'observed',observed

    @staticmethod
    def _event(row):
        return {'id':row['id'],'project':row['project'],'previous_sha':row['previous_sha'],
                'head_sha':row['head_sha'],'revision':row['revision'],'reference':row['reference'],
                'observed_at':row['observed_at'],'sequence':row['sequence'],
                'acknowledged':row['acknowledged_at'] is not None,'approval_granted':False}

    def inspect(self,token,project):
        name(project)
        config=self.registry.get(token,project)
        state,observed=self._collect(project,config['config'],token=token)
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('source configuration changed during observation')
            prior=c.execute('SELECT * FROM manager_source_heads WHERE project=?',(project,)).fetchone()
            matching=prior is not None and prior['config_fingerprint']==config['fingerprint']
            result={'project':project,'state':state,'previous_sha':prior['sha'] if matching else None,
                    'head_sha':None,'event_id':None,'checked_at':self.store._now(),
                    'approval_granted':False,'source_modified':False,'tasks_replayed':False}
            if observed is None:return result
            try:_reject_known_tokens(c,observed.__dict__)
            except ValueError:
                result['state']='unknown';return result
            # A delayed older response cannot roll back a newer observed baseline.
            if matching and observed.observed_at<prior['observed_at']:
                result['state']='stale';return result
            sequence=prior['sequence'] if matching else 0
            result.update(head_sha=observed.sha,revision=observed.revision,reference=observed.reference,
                          observed_at=observed.observed_at)
            if not matching:result['state']='baseline'
            elif prior['sha']==observed.sha:
                result['state']='unchanged'
            else:
                sequence+=1
                key=digest({'project':project,'config':config['fingerprint'],'sequence':sequence})
                c.execute('INSERT INTO manager_source_changes VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                    (key,project,config['fingerprint'],prior['sha'],observed.sha,observed.revision,
                     observed.reference,observed.observed_at,sequence))
                result.update(state='changed',event_id=key)
            c.execute('INSERT OR REPLACE INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',
                (project,config['fingerprint'],observed.sha,observed.revision,observed.reference,observed.observed_at,sequence))
            return result

    def pending(self,token,projects,*,limit=100):
        if type(projects) not in (tuple,list) or not 1<=len(projects)<=20 or len(set(projects))!=len(projects):
            raise ValueError('explicit bounded project batch required')
        if type(limit) is not int or not 1<=limit<=500:raise ValueError('bounded event limit required')
        for project in projects:name(project)
        with self.store._transaction() as c:
            configs={p:self.registry._get(c,token,p) for p in sorted(projects)}
            events=[]
            for project,config in configs.items():
                rows=c.execute('''SELECT * FROM manager_source_changes WHERE project=? AND
                    config_fingerprint=? AND acknowledged_at IS NULL ORDER BY sequence LIMIT ?''',
                    (project,config['fingerprint'],limit+1)).fetchall()
                events.extend(self._event(row) for row in rows)
            events.sort(key=lambda row:(row['observed_at'],row['project'],row['sequence']))
            return {'projects':sorted(projects),'events':events[:limit],'more':len(events)>limit,
                    'approval_granted':False,'background_polling_started':False}

    def acknowledge(self,token,event_id):
        if type(event_id) is not str or len(event_id)!=64 or any(x not in '0123456789abcdef' for x in event_id):
            raise ValueError('exact event identifier required')
        with self.store._transaction() as c:
            self.store._authenticate(c,token)
            row=c.execute('SELECT * FROM manager_source_changes WHERE id=?',(event_id,)).fetchone()
            if row is None:raise NotFound('source event unavailable')
            config=self.registry._get(c,token,row['project'])
            if config['fingerprint']!=row['config_fingerprint']:raise Conflict('event belongs to an earlier source configuration')
            c.execute('UPDATE manager_source_changes SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?',
                      (self.store._now(),event_id))
            row=c.execute('SELECT * FROM manager_source_changes WHERE id=?',(event_id,)).fetchone()
            return self._event(row)

    def source_observer(self,project):
        """Host-only fresh proof callback for Delegation; no client callback loading.

        Delegation authenticates before and after invoking this callback. Read the
        same registered configuration on both sides of provider observation too.
        """
        name(project)
        def observe(requested_project):
            if requested_project!=project:raise Conflict('source observer belongs to another project')
            with self.store._transaction() as c:
                row=c.execute('SELECT config,fingerprint FROM maintenance_projects WHERE project=?',(project,)).fetchone()
                if row is None:raise NotFound('registered source unavailable')
                config=json.loads(row['config']);fingerprint=row['fingerprint']
            state,head=self._collect(project,config)
            if state!='observed':raise Conflict('fresh source observation unavailable')
            with self.store._transaction() as c:
                row=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(project,)).fetchone()
                if row is None or row['fingerprint']!=fingerprint:raise Conflict('source configuration changed during proof')
                try:_reject_known_tokens(c,head.__dict__)
                except ValueError:raise Conflict('invalid source proof reference') from None
            return SourceProof(project,head.source_ref,head.branch,head.sha,head.reference,head.observed_at)
        return observe
