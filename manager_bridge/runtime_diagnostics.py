"""One transaction, ten retained-state summaries; no providers or runtime writes."""
import math
from .control_center import ControlCenter
from .store import Conflict,digest


def inspect_runtime(control,token,projects):
    if not isinstance(control,ControlCenter):raise ValueError('registered control center required')
    expected=control._scope(token,projects)
    store=control.store
    with store._transaction() as c:
        c.execute('PRAGMA query_only=ON')
        actor=store._authenticate(c,token);store._role(actor,('owner','manager'))
        configs={p:control.registry._get(c,token,p) for p in projects}
        if {p:v['fingerprint'] for p,v in configs.items()}!=expected:
            raise Conflict('diagnostic scope changed')
        now=store._now()
        if type(now) not in (int,float) or not math.isfinite(now) or now<=0:
            raise Conflict('finite diagnostic observation clock required')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        marks=','.join('?' for _ in projects);params=tuple(projects)
        def counts(table,field,states,where=None,arguments=None):
            if table not in tables:return {'availability':'not_recorded'}
            # Table/field/where fragments are fixed in this function's call sites.
            rows=c.execute('SELECT '+field+',COUNT(*) FROM '+table+' WHERE '+(where or 'project IN ('+marks+')')+' GROUP BY '+field,params if arguments is None else arguments)
            value={key:0 for key in states};unknown=0
            for row in rows:
                if row[0] in value:value[row[0]]+=row[1]
                else:unknown+=row[1]
            return {'availability':'recorded','counts':value,'other_count':unknown}
        def section(key,value):return {'section':key,**value}
        items=[section('registration',{'availability':'recorded','project_count':len(projects),
            'maintenance_enabled_count':sum(v['config']['maintenance_enabled'] is True for v in configs.values())})]
        items.append(section('roles',counts('hierarchy_members','role',('taesan','developer','reviewer'))))
        items.append(section('tasks',counts('bridge_tasks','state',('pending_approval','queued','running','reported','reviewed','cancelled','needs_verification'))))
        items.append(section('actions',counts('continuous_actions','state',('queued','running','uncertain','confirmed','reconciled','blocked'))))
        items.append(section('schedules',counts('governance_supervision_jobs','state',('running','uncertain','observed','unavailable','retention_required'),
            'scope=?',(digest(sorted(projects)),))))
        items.append(section('authentication_records',counts('operating_budget_auth','state',('ready','expired','unknown','unavailable'))))
        if 'host_admission_requirements' in tables:
            host_count=c.execute('SELECT COUNT(*) FROM host_admission_requirements WHERE project IN ('+marks+')',params).fetchone()[0]
            host={'availability':'recorded','requirement_count':host_count}
        else:host={'availability':'not_recorded'}
        items.append(section('host_requirements',host))
        items.append(section('transitions',counts('runtime_transition_state','mode',('accepting','draining'))))
        quarantine=bool('governance_restore_marker' in tables and c.execute('SELECT 1 FROM governance_restore_marker WHERE reconciliation_required=1 LIMIT 1').fetchone())
        items.append(section('restore_quarantine',{'availability':'recorded' if 'governance_restore_marker' in tables else 'not_recorded','reconciliation_required':quarantine}))
        if actor['role']!='owner' or actor['projects']!=['*']:
            backup={'availability':'restricted'}
        else:backup=counts('encrypted_backups','state',('preparing','uploading','stored','uncertain'),'1=1',())
        items.append(section('backup_records',backup))
        return {'scope':'retained_database_metadata_only','observed_at':now,'project_count':len(projects),
                'sections':items,'section_count':len(items),'automatic_replay':False,
                'credential_refresh_performed':False,'providers_invoked':False,'models_started':0,
                'runtime_records_written':False,'authentication_verified':False,
                'worker_execution_verified':False,'scheduler_running_verified':False,
                'always_on_verified':False,'execution_authorized':False}
