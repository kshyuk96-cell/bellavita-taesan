"""Restart diagnostics and an opt-in cooperative local manager lifetime guard.

No timeout, missing heartbeat, PID, or expired lease proves a worker stopped.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from .store import Conflict,PermissionDenied,NotFound,digest
from .maintenance_registry import name
from .three_tier import ThreeTier
from .continuous_manager import ContinuousManager
from .governance_supervision import GovernanceSupervision


class RecoveryReadiness:
    def __init__(self,core,*,controller=None,supervision=None):
        if not isinstance(core,ThreeTier):raise ValueError('registered core required')
        if controller is not None and (not isinstance(controller,ContinuousManager) or controller.core is not core):raise ValueError('same registered controller required')
        if supervision is not None and (not isinstance(supervision,GovernanceSupervision) or supervision.host.core is not core):raise ValueError('same registered supervision required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.controller,self.supervision=controller,supervision

    def _scope(self,c,token,projects):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit bounded recovery scope required')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        configs={p:self.registry._get(c,token,p)['fingerprint'] for p in projects}
        return actor,configs

    def inspect(self,token,projects,*,limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded recovery detail window required')
        with self.store._transaction() as c:
            actor,configs=self._scope(c,token,projects);now=self.store._now()
            tables={row['name'] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            marks=','.join('?' for _ in projects);parameters=tuple(projects);items=[];counts={};blockers=[]
            def collect(kind,sql,params,requires):
                total=c.execute('SELECT COUNT(*) FROM ('+sql+')',params).fetchone()[0]
                counts[kind]=total
                rows=c.execute(sql+' LIMIT ?',(*params,limit+1)).fetchall()
                for row in rows[:limit]:
                    values=dict(row);items.append({'kind':kind,**values,'reconciliation_required':requires})
                if requires and total:blockers.append(kind)
                return len(rows)>limit
            partial=False
            restored='governance_restore_marker' in tables and bool(c.execute('SELECT 1 FROM governance_restore_marker WHERE reconciliation_required=1 LIMIT 1').fetchone())
            if restored:blockers.append('restored_authority_quarantined')
            leader=c.execute('SELECT * FROM continuous_leader WHERE singleton=1').fetchone() if 'continuous_leader' in tables else None
            leader_state='unobserved'
            if leader and leader['expires']<=now:leader_state='expired'
            elif leader and leader['actor_hash']==actor['token_hash'] and leader['projects_digest']==digest(sorted(projects)):
                try:
                    for project in projects:
                        self.core._actor(c,token,project,'taesan',self.registry._get(c,token,project))
                    mismatch=c.execute("SELECT 1 FROM continuous_actions WHERE project IN ("+marks+") AND state='running' AND (actor_hash IS NULL OR epoch IS NULL OR actor_hash!=? OR epoch!=?) LIMIT 1",
                        (*parameters,leader['actor_hash'],leader['epoch'])).fetchone() if 'continuous_actions' in tables else True
                    if not mismatch:leader_state='live'
                except (PermissionDenied,Conflict,NotFound):pass
            if 'continuous_actions' in tables:
                partial|=collect('uncertain_actions','SELECT action_key,task_id,project,phase FROM continuous_actions WHERE project IN ('+marks+") AND state='uncertain' ORDER BY updated,action_key",parameters,True)
                partial|=collect('running_actions','SELECT action_key,task_id,project,phase FROM continuous_actions WHERE project IN ('+marks+") AND state='running' ORDER BY updated,action_key",parameters,leader_state!='live')
            partial|=collect('expired_worker_leases',"SELECT id AS task_id,project FROM bridge_tasks WHERE project IN ("+marks+") AND state IN ('running','needs_verification','cancelled') AND worker_token_hash IS NOT NULL AND (lease_expires IS NULL OR lease_expires<=?) AND NOT EXISTS (SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=bridge_tasks.id) ORDER BY updated,id",(*parameters,now),True)
            if 'hierarchy_tasks' in tables:
                partial|=collect('expired_review_leases','SELECT task_id,project FROM hierarchy_tasks WHERE project IN ('+marks+") AND state='reviewing' AND (lease_expires IS NULL OR lease_expires<=?) ORDER BY task_id",(*parameters,now),True)
            if 'continuous_host_assignments' in tables:
                if leader_state=='live' and 'continuous_actions' in tables:
                    host_filter="(h.phase IN ('uncertain','freezing') OR (h.phase='executing' AND NOT EXISTS (SELECT 1 FROM continuous_actions a WHERE a.task_id=h.task_id AND a.state='running' AND a.actor_hash=? AND a.epoch=?)))"
                    host_params=(*parameters,leader['actor_hash'],leader['epoch'])
                else:
                    host_filter="h.phase IN ('uncertain','freezing','executing')";host_params=parameters
                partial|=collect('unresolved_host_assignments','SELECT h.task_id,t.project,h.phase FROM continuous_host_assignments h JOIN bridge_tasks t ON t.id=h.task_id WHERE t.project IN ('+marks+') AND '+host_filter+' ORDER BY h.task_id',host_params,True)
            supervision_state='not_configured'
            if self.supervision is not None:
                # The jobs are shared across its full configured scope. Check that
                # scope before exposing even aggregate unresolved job metadata.
                self._scope(c,token,self.supervision.projects)
                supervision_state='observed'
                partial|=collect('unresolved_supervision_jobs',"SELECT job,state FROM governance_supervision_jobs WHERE scope=? AND (state='uncertain' OR (state='running' AND next_due<=?)) ORDER BY job",(self.supervision.scope,now),True)
            for p in projects:
                if self.registry._get(c,token,p)['fingerprint']!=configs[p]:raise Conflict('recovery configuration changed')
            result={'projects':list(projects),'observed_at':now,'leader_state':leader_state,
                    'restore_quarantined':bool(restored),'dispatch_ready':not blockers,'blockers':sorted(set(blockers)),
                    'counts':counts,'items':items[:limit],'details':{'limit':limit,'partial':partial or len(items)>limit},
                    'supervision_state':supervision_state,'local_process_guard':'not_acquired_by_inspection',
                    'diagnostic_access_allowed':True,'automatic_replay':False,'workers_stopped_verified':False,
                    'operational_change':False,'always_on_verified':False}
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,result)
            return result

    def _guard_parent(self):
        database=Path(self.store.path).absolute();parent=database.parent
        if any(p.is_symlink() for p in (database,*database.parents)):raise ValueError('physical manager store required')
        info=parent.stat();db=database.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or not stat.S_ISREG(db.st_mode) or db.st_uid!=os.getuid() or db.st_mode&0o077 or db.st_nlink!=1:
            raise ValueError('private owned manager store required')
        return parent,(info.st_dev,info.st_ino)

    @contextmanager
    def instance_guard(self,token,projects,instance_id):
        name(instance_id)
        with self.store._transaction() as c:
            _,configs=self._scope(c,token,projects)
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,{'instance_id':instance_id})
        parent,identity=self._guard_parent()
        directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        lock=None;acquired=False
        try:
            if (os.fstat(directory).st_dev,os.fstat(directory).st_ino)!=identity:raise Conflict('manager directory changed')
            filename='.manager-runtime-'+hashlib.sha256(self.store.path.name.encode()).hexdigest()[:24]+'.guard'
            lock=os.open(filename,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
            info=os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size!=0:raise ValueError('private unshared manager guard required')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);acquired=True
            except BlockingIOError:raise Conflict('another cooperative local manager owns this store') from None
            linked=os.stat(filename,dir_fd=directory,follow_symlinks=False)
            if (linked.st_dev,linked.st_ino)!=(info.st_dev,info.st_ino):raise Conflict('manager guard identity changed')
            if self._guard_parent()[1]!=identity:raise Conflict('manager directory changed')
            with self.store._transaction() as c:
                if self._scope(c,token,projects)[1]!=configs:raise Conflict('manager guard scope changed')
            yield {'state':'exclusive_local_manager_guard_held','instance_id':instance_id,
                   'scope_fingerprint':digest(sorted(projects)),'guard_scope':'same_local_store_only',
                   'workers_stopped_verified':False,'automatic_replay':False,'always_on_verified':False}
        finally:
            if lock is not None:
                if acquired:fcntl.flock(lock,fcntl.LOCK_UN)
                os.close(lock)
            os.close(directory)
