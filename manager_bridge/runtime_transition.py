"""Owner-CAS admission drain and reviewable Linux service-unit drafts.

A drain is an admission fence, never evidence that an old worker stopped.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from .store import Conflict,canonical_json,digest
from .maintenance_registry import name
from .three_tier import ThreeTier
from .governance_supervision import GovernanceSupervision

REASONS=frozenset({'planned_update','planned_restart','planned_stop'})


def assert_new_start(c,project):
    """Call inside the real admission transaction; an absent row preserves legacy."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_transition_state'").fetchone():return
    row=c.execute('SELECT * FROM runtime_transition_state WHERE project=?',(project,)).fetchone()
    if row is None:return
    current=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(project,)).fetchone()
    if current is None or current['fingerprint']!=row['config_fingerprint']:
        raise Conflict('runtime transition configuration changed; owner review required')
    if row['mode']!='accepting':raise Conflict('runtime transition draining; new admission blocked')


class RuntimeTransition:
    def __init__(self,core,*,supervision=None):
        if not isinstance(core,ThreeTier):raise ValueError('registered core required')
        if supervision is not None and (not isinstance(supervision,GovernanceSupervision) or supervision.host.core is not core):raise ValueError('same registered supervision required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.supervision=supervision
        with self.store._transaction() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS runtime_transition_state (
                project TEXT PRIMARY KEY,revision INTEGER,config_fingerprint TEXT,mode TEXT,reason TEXT,updated REAL);
                CREATE TABLE IF NOT EXISTS runtime_transition_commands (
                actor TEXT,request_key TEXT,binding TEXT,result TEXT,PRIMARY KEY(actor,request_key));''')

    def _scope(self,c,token,projects,owner=False):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit bounded transition scope required')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner',) if owner else ('owner','manager'))
        configs={p:self.registry._get(c,token,p)['fingerprint'] for p in projects}
        return actor,configs

    def _change(self,token,projects,expected_revisions,request_key,mode,reason):
        name(request_key)
        if type(expected_revisions) is not dict or type(reason) is not str or reason not in REASONS:raise ValueError('exact transition decision required')
        with self.store._transaction() as c:
            actor,configs=self._scope(c,token,projects,True)
            if set(expected_revisions)!=set(projects) or any(type(v) is not int or v<0 for v in expected_revisions.values()):raise ValueError('exact transition revisions required')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,{'request_key':request_key})
            binding=digest({'configs':configs,'expected':expected_revisions,'mode':mode,'reason':reason})
            old=c.execute('SELECT * FROM runtime_transition_commands WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if old['binding']!=binding:raise Conflict('transition request binding changed')
                receipt=json.loads(old['result'])
                for item in receipt['projects']:
                    current=c.execute('SELECT revision,mode FROM runtime_transition_state WHERE project=?',(item['project'],)).fetchone()
                    if current is None or (current['revision'],current['mode'])!=(item['revision'],item['mode']):raise Conflict('transition receipt superseded')
                return receipt
            rows={p:c.execute('SELECT * FROM runtime_transition_state WHERE project=?',(p,)).fetchone() for p in projects}
            for p,row in rows.items():
                if (row['revision'] if row else 0)!=expected_revisions[p]:raise Conflict('transition revision changed')
            now=self.store._now();items=[]
            for p in projects:
                revision=expected_revisions[p]+1
                c.execute('INSERT OR REPLACE INTO runtime_transition_state VALUES (?,?,?,?,?,?)',(p,revision,configs[p],mode,reason,now))
                items.append({'project':p,'revision':revision,'mode':mode,'reason':reason})
            result={'projects':items,'new_admission_blocked':mode=='draining','workers_stopped_verified':False,
                    'automatic_replay':False,'system_service_changed':False}
            _reject_known_tokens(c,result)
            c.execute('INSERT INTO runtime_transition_commands VALUES (?,?,?,?)',(actor['token_hash'],request_key,binding,canonical_json(result)))
            return result

    def drain(self,token,projects,expected_revisions,request_key,*,reason='planned_update'):
        return self._change(token,projects,expected_revisions,request_key,'draining',reason)

    def resume(self,token,projects,expected_revisions,request_key):
        return self._change(token,projects,expected_revisions,request_key,'accepting','planned_restart')

    def status(self,token,projects):
        with self.store._transaction() as c:
            _,configs=self._scope(c,token,projects);tables={r['name'] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")};items=[]
            shared_jobs=0
            if self.supervision is not None:
                self._scope(c,token,self.supervision.projects)
                shared_jobs=c.execute("SELECT COUNT(*) FROM governance_supervision_jobs WHERE scope=? AND state IN ('running','uncertain')",(self.supervision.scope,)).fetchone()[0]
            for p in projects:
                row=c.execute('SELECT * FROM runtime_transition_state WHERE project=?',(p,)).fetchone()
                configured=row is not None;mode=row['mode'] if row else 'accepting';stale=bool(row and row['config_fingerprint']!=configs[p])
                counts={}
                counts['hierarchy_inflight']=c.execute("SELECT COUNT(*) FROM hierarchy_tasks WHERE project=? AND state!='accepted'",(p,)).fetchone()[0]
                counts['unconfirmed_worker_tasks']=c.execute("SELECT COUNT(*) FROM bridge_tasks t WHERE project=? AND state IN ('running','reported','needs_verification','cancelled') AND worker_token_hash IS NOT NULL AND NOT EXISTS(SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id)",(p,)).fetchone()[0]
                counts['unresolved_actions']=c.execute("SELECT COUNT(*) FROM continuous_actions WHERE project=? AND state IN ('running','uncertain')",(p,)).fetchone()[0] if 'continuous_actions' in tables else 0
                counts['unresolved_host_assignments']=c.execute("SELECT COUNT(*) FROM continuous_host_assignments h JOIN bridge_tasks t ON t.id=h.task_id WHERE t.project=? AND h.phase IN ('executing','uncertain','freezing')",(p,)).fetchone()[0] if 'continuous_host_assignments' in tables else 0
                ready=configured and mode=='draining' and not stale and not any(counts.values()) and shared_jobs==0
                items.append({'project':p,'configured':configured,'revision':row['revision'] if row else 0,'mode':mode,
                    'configuration_current':not stale,'new_admission_blocked':stale or mode=='draining',
                    'counts':counts,'drain_ready':ready})
            result={'projects':items,'drain_ready':all(item['drain_ready'] for item in items),
                    'shared_supervision_unresolved':shared_jobs,'supervision_checked':self.supervision is not None,
                    'workers_stopped_verified':False,'host_update_authorized':False,'automatic_replay':False,
                    'process_state':'not_inferred_from_pid_or_timeout'}
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,result)
            return result


@dataclass(frozen=True)
class UnitDraft:
    application_root: str
    python: str
    config_file: str
    state_directory: str
    user: str = 'taesan-manager'
    group: str = 'taesan-manager'
    unit_name: str = 'taesan-governance'
    subscription_config_file: str | None = None

    def __post_init__(self):
        fields=('application_root','python','config_file','state_directory')
        if self.subscription_config_file is not None:fields+=('subscription_config_file',)
        for field in fields:
            value=getattr(self,field)
            if type(value) is not str or len(value)>512 or not value.startswith('/') or value.startswith('//') or not re.fullmatch(r'[A-Za-z0-9_/.-]+',value) or '..' in Path(value).parts:
                raise ValueError('fixed physical-style absolute unit path required')
        for field in ('user','group','unit_name'):
            value=getattr(self,field)
            if type(value) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',value) or value=='root':raise ValueError('fixed non-root unit identity required')


def render_unit(draft):
    if not isinstance(draft,UnitDraft):raise ValueError('typed unit draft required')
    return f'''# Draft only. Not installed, enabled, started, stopped, or deployed.
# Suggested unit name: {draft.unit_name}.service
# Drain new admission and verify existing records before an operator update.
[Unit]
Description=Taesan registered governance manager
After=network-online.target
Wants=network-online.target
RequiresMountsFor={draft.state_directory}
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User={draft.user}
Group={draft.group}
UMask=0077
WorkingDirectory={draft.application_root}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={draft.python} {draft.application_root}/scripts/run_governance_service.py --config {draft.config_file}{' --subscription-config '+draft.subscription_config_file if draft.subscription_config_file is not None else ''}
Restart=on-failure
RestartSec=15
KillSignal=SIGTERM
KillMode=control-group
TimeoutStopSec=60
NoNewPrivileges=true
RestrictRealtime=true

[Install]
WantedBy=multi-user.target
'''


def write_unit_draft(draft,destination):
    data=render_unit(draft).encode()
    if not isinstance(destination,(str,Path)) or not str(destination).startswith('/') or str(destination).startswith('//'):
        raise ValueError('single-leading-slash draft destination required')
    path=Path(destination).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)) or any(path==root or root in path.parents for root in (Path('/etc/systemd'),Path('/usr/lib/systemd'),Path('/lib/systemd'),Path('/run/systemd'))):
        raise ValueError('review draft destination required; system installation forbidden')
    parent=path.parent.stat()
    if parent.st_uid!=os.getuid() or parent.st_mode&0o077 or not stat.S_ISDIR(parent.st_mode):raise ValueError('owned private draft directory required')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    return {'state':'unit_draft_created','sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
            'installed':False,'enabled':False,'started':False,'system_service_changed':False}
