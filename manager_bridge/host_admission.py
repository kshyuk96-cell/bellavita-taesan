"""Owner-pinned local host prerequisites in the actual new-work admission fence.

A passing observation is a short-lived local measurement, not durable hosting or
remote attestation. No worker, daemon, deployment or model is started here.
"""
from dataclasses import dataclass, asdict
import json
import math
import re
from pathlib import Path
from .store import Conflict, PermissionDenied, canonical_json, digest
from .three_tier import ThreeTier
from .host_preflight import inspect_preflight, local_host_identity
from .continuous_host import _git


def _hex(value, length):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{'+str(length)+'}', value):
        raise ValueError('exact pinned digest required')
    return value


@dataclass(frozen=True)
class HostRequirement:
    host_identity: str
    application_commit: str
    max_age_seconds: int = 30

    def __post_init__(self):
        _hex(self.host_identity, 64); _hex(self.application_commit, 40)
        if type(self.max_age_seconds) is not int or not 1 <= self.max_age_seconds <= 60:
            raise ValueError('bounded local host observation TTL required')


@dataclass(frozen=True)
class HostObservation:
    host_identity: str
    application_commit: str
    namespace_isolation_verified: bool
    private_storage_verified: bool
    observed_at: float

    def __post_init__(self):
        _hex(self.host_identity,64); _hex(self.application_commit,40)
        if (type(self.namespace_isolation_verified) is not bool or type(self.private_storage_verified) is not bool
                or type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at <= 0):
            raise ValueError('bounded actual host facts required')


class LocalHostObserver:
    def __init__(self, *, application_root, state_root):
        self.application_root = Path(application_root)
        self.state_root = Path(state_root)

    def __call__(self, requirement):
        if type(requirement) is not HostRequirement: raise ValueError('typed host requirement required')
        root = self.application_root
        if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)):
            raise ValueError('physical application checkout required')
        commit = _git(root,'rev-parse','HEAD').decode().strip()
        if _git(root,'status','--porcelain'): raise Conflict('clean pinned application required')
        result = inspect_preflight(expected_host_identity=requirement.host_identity,state_root=self.state_root)
        if _git(root,'rev-parse','HEAD').decode().strip()!=commit or _git(root,'status','--porcelain'):
            raise Conflict('application changed during local host measurement')
        return HostObservation(result['observed_local_host_identity'],commit,
            result['namespace_isolation_verified'],result['storage']['private_writable_fsync_verified'],result['observed_at'])


class HostAdmission:
    def __init__(self, core, *, observer=None):
        if not isinstance(core, ThreeTier) or (observer is not None and not callable(observer)):
            raise ValueError('same hierarchy and trusted local observer required')
        self.core,self.store,self.registry,self.observer=core,core.store,core.registry,observer
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS host_admission_requirements (project TEXT PRIMARY KEY,config_fingerprint TEXT,revision INTEGER,body TEXT,requirement_digest TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS host_admission_observations (project TEXT PRIMARY KEY,requirement_digest TEXT,sequence INTEGER,body TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS host_admission_requests (project TEXT PRIMARY KEY,sequence INTEGER)')
            if 'watermark' not in {row[1] for row in c.execute('PRAGMA table_info(host_admission_observations)')}:
                c.execute('ALTER TABLE host_admission_observations ADD COLUMN watermark REAL NOT NULL DEFAULT 0')
                c.execute('UPDATE host_admission_observations SET watermark=?',(self.store._now(),))

    def _auth(self,c,token,project,owner=False):
        return self.core._config(c,token,project,('owner',) if owner else ('owner','manager'))

    def install(self,token,project,requirement,*,expected_revision=0):
        if type(requirement) is not HostRequirement or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('typed host requirement and exact prior revision required')
        with self.store._transaction() as c:
            config=self._auth(c,token,project,True)
            old=c.execute('SELECT revision FROM host_admission_requirements WHERE project=?',(project,)).fetchone()
            if expected_revision != (old['revision'] if old else 0): raise Conflict('host requirement revision changed')
            revision=expected_revision+1;body=asdict(requirement)
            stamp=digest({'project':project,'config':config['fingerprint'],'revision':revision,'body':body})
            c.execute('INSERT OR REPLACE INTO host_admission_requirements VALUES (?,?,?,?,?)',(project,config['fingerprint'],revision,canonical_json(body),stamp))
            c.execute('DELETE FROM host_admission_observations WHERE project=?',(project,))
            return {'project':project,'revision':revision,'requirement_digest':stamp,'execution_authorized':False}

    def refresh(self,token,project):
        with self.store._transaction() as c:
            config=self._auth(c,token,project)
            row=c.execute('SELECT * FROM host_admission_requirements WHERE project=?',(project,)).fetchone()
            if row is None: raise Conflict('owner host requirement required')
            if row['config_fingerprint'] != config['fingerprint']: raise Conflict('host requirement configuration changed')
            requirement=HostRequirement(**json.loads(row['body']));stamp=row['requirement_digest']
            c.execute('INSERT OR IGNORE INTO host_admission_requests VALUES (?,0)',(project,))
            c.execute('UPDATE host_admission_requests SET sequence=sequence+1 WHERE project=?',(project,))
            sequence=c.execute('SELECT sequence FROM host_admission_requests WHERE project=?',(project,)).fetchone()[0]
        value=None
        if self.observer is not None:
            try: value=self.observer(requirement)
            except Exception: value=None
        with self.store._transaction() as c:
            current=self._auth(c,token,project)
            latest=c.execute('SELECT * FROM host_admission_requirements WHERE project=?',(project,)).fetchone()
            if current['fingerprint']!=config['fingerprint'] or latest is None or latest['requirement_digest']!=stamp:
                raise Conflict('host observation authority changed')
            active=c.execute('SELECT sequence FROM host_admission_requests WHERE project=?',(project,)).fetchone()[0]
            if active!=sequence: return {'project':project,'state':'superseded','execution_authorized':False}
            now=self.store._now()
            if type(now) not in (int,float) or not math.isfinite(now) or now<=0:raise Conflict('finite host observation clock required')
            typed=type(value) is HostObservation
            fresh=typed and 0<=now-value.observed_at<requirement.max_age_seconds
            watermark=value.observed_at if fresh else now
            prior=c.execute('SELECT body,watermark FROM host_admission_observations WHERE project=?',(project,)).fetchone()
            def positive(body):
                return (type(body) is dict and type(body.get('observed_at')) in (int,float)
                        and math.isfinite(body['observed_at']) and 0<=now-body['observed_at']<requirement.max_age_seconds
                        and body.get('host_identity')==requirement.host_identity
                        and body.get('application_commit')==requirement.application_commit
                        and body.get('namespace_isolation_verified') is True and body.get('private_storage_verified') is True)
            body=asdict(value) if typed else None
            if prior and (watermark<prior['watermark'] or
                    (watermark==prior['watermark'] and positive(body) and not positive(json.loads(prior['body'])))):
                return {'project':project,'state':'superseded','execution_authorized':False}
            c.execute('INSERT OR REPLACE INTO host_admission_observations(project,requirement_digest,sequence,body,watermark) VALUES (?,?,?,?,?)',
                      (project,stamp,sequence,canonical_json(body),watermark))
            return self._check(c,token,project)

    def _check(self,c,token,project):
        config=self._auth(c,token,project)
        row=c.execute('SELECT * FROM host_admission_requirements WHERE project=?',(project,)).fetchone()
        reason=None
        if row is None: reason='host_requirement_missing'
        elif row['config_fingerprint']!=config['fingerprint']: reason='host_configuration_changed'
        else:
            body=json.loads(row['body']);req=HostRequirement(**body)
            if row['requirement_digest']!=digest({'project':project,'config':config['fingerprint'],'revision':row['revision'],'body':body}):
                reason='host_requirement_changed'
            observation=c.execute('SELECT * FROM host_admission_observations WHERE project=?',(project,)).fetchone()
            value=json.loads(observation['body']) if observation else None
            if reason is None:
                if observation is None or observation['requirement_digest']!=row['requirement_digest'] or value is None:
                    reason='host_observation_unavailable'
                else:
                    obs=HostObservation(**value);now=self.store._now()
                    if not 0 <= now-obs.observed_at < req.max_age_seconds: reason='host_observation_expired'
                    elif obs.host_identity!=req.host_identity: reason='host_identity_changed'
                    elif obs.application_commit!=req.application_commit: reason='host_application_changed'
                    elif not obs.namespace_isolation_verified: reason='host_isolation_unavailable'
                    elif not obs.private_storage_verified: reason='host_storage_unavailable'
                    else:
                        try:
                            if local_host_identity()!=req.host_identity: reason='host_process_moved'
                        except Exception: reason='host_identity_unavailable'
        return {'project':project,'allowed':reason is None,'state':'local_prerequisites_observed' if reason is None else 'paused',
                'reason':reason,'scope':'local_new_work_admission_only','always_on_verified':False,'execution_authorized':False}

    def check(self,token,project):
        with self.store._transaction() as c: return self._check(c,token,project)

    def poll(self,token,projects):
        if type(projects) not in (list,tuple) or not 1 <= len(projects) <= 100 or len(set(projects)) != len(projects):
            raise ValueError('bounded explicit host polling scope required')
        with self.store._transaction() as c:
            configs={p:self._auth(c,token,p)['fingerprint'] for p in projects}
            due=[p for p in projects if c.execute('SELECT 1 FROM host_admission_requirements WHERE project=?',(p,)).fetchone() and not self._check(c,token,p)['allowed']]
        results=[]
        for project in due:
            try:results.append(self.refresh(token,project))
            except Conflict:results.append({'project':project,'allowed':False,'reason':'host_requirement_changed','execution_authorized':False})
        with self.store._transaction() as c:
            if any(self._auth(c,token,p)['fingerprint']!=configs[p] for p in projects): raise Conflict('host polling configuration changed')
        return {'observations':results,'models_started':0,'always_on_verified':False}
