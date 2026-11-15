"""Trusted local phase driver for the continuous three-tier controller.

Only this host reads role token files and chooses source/runtime/CLI bindings.
HTTP cannot supply these objects. Source changes are committed only in the
registered development checkout after path-policy checks; no remote push,
merge, deployment, payment or message operation exists here.
"""
from dataclasses import dataclass,asdict,replace,field
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import time
from .store import Store,Conflict,PermissionDenied,NotFound,canonical_json,digest,token_hash,_sha
from .maintenance_registry import Registry,name
from .coordination import Coordination,scope_violations
from .three_tier import ThreeTier
from .three_tier_runner import ReviewProfile,FixedReviewRunner
from .source_rehearsal import observe_checkout
from .control import secret


def _physical(path,*,private=False):
    value=Path(path).absolute()
    if '..' in value.parts or any(p.is_symlink() for p in (value,*value.parents)) or not value.is_dir():
        raise ValueError('registered physical host directory required')
    if private and (value.stat().st_uid!=os.getuid() or value.stat().st_mode&0o077):raise ValueError('private owned host directory required')
    return value


def _private_file(path):
    value=Path(path).absolute()
    if any(p.is_symlink() for p in (value,*value.parents)):raise ValueError("private physical file required")
    info=value.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError("owned private file required")
    return value


def _token(path):return secret(_private_file(path))


def _private_json(path):
    value=Path(path).absolute()
    if any(p.is_symlink() for p in (value,*value.parents)) or not value.is_file():raise ValueError('private host configuration required')
    info=value.stat()
    if info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>128000:raise ValueError('private bounded host configuration required')
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('duplicate configuration field')
            out[k]=v
        return out
    result=json.loads(value.read_text(),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite config')))
    if type(result) is not dict:raise ValueError('host config object required')
    return result


def _guard_git_filters(root):
    """Never execute repository-configured clean/process filters on the host."""
    if not (Path(root)/'.git').exists():return
    result=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(root),
        'config','--includes','--name-only','--get-regexp',r'^filter\..*\.(clean|process|smudge)$'],
        env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'},
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
    if result.returncode!=1:raise Conflict('registered source has executable Git filters or unreadable effective repository configuration')


def _observe(root):
    _guard_git_filters(root)
    return observe_checkout(root)


def _git(root,*args):
    _guard_git_filters(root)
    result=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
        '-c','core.untrackedCache=false','-c','commit.gpgsign=false','-c','user.name=Taesan Development Host',
        '-c','user.email=taesan-host@invalid','-C',str(root),*args],
        env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1',
             'GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1','GIT_NO_REPLACE_OBJECTS':'1'},
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30)
    if result.returncode or len(result.stdout)>8*1024*1024:raise Conflict('registered development source operation failed')
    return result.stdout


@dataclass(frozen=True)
class ProjectHost:
    project:str
    repository:str
    source:Path
    candidates:Path
    runtime:Path
    developer_id:str
    reviewer_id:str
    developer_token_file:Path=field(repr=False)
    reviewer_token_file:Path=field(repr=False)
    checks:dict
    developer_transport:object=field(repr=False)
    reviewer_factory:object=field(repr=False)
    session_id:str|None=None
    fleet_template:str|None=None
    fleet_host_ref:str|None=None
    review_engine:object=field(default=None,repr=False)
    def __post_init__(self):
        for value in (self.project,self.developer_id,self.reviewer_id):name(value)
        if type(self.repository) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',self.repository):raise ValueError('registered repository required')
        for key in ('source','candidates','runtime'):object.__setattr__(self,key,_physical(getattr(self,key),private=key=='candidates'))
        _guard_git_filters(self.source)
        if self.source==self.candidates or self.source in self.candidates.parents or self.candidates in self.source.parents:raise ValueError('independent candidate directory required')
        if type(self.checks) is not dict or not self.checks:raise ValueError('registered independent checks required')
        copied={}
        for key,argv in self.checks.items():
            if type(key) is not str or not key or type(argv) not in (list,tuple) or not argv or any(type(x) is not str or not x or len(x)>4000 for x in argv):raise ValueError('fixed bounded test argv required')
            copied[key]=tuple(argv)
        object.__setattr__(self,'checks',copied)
        if not callable(getattr(self.developer_transport,'execute',None)) or not callable(getattr(self.developer_transport,'discover',None)) or not callable(self.reviewer_factory):raise ValueError('registered Codex transports required')
        # Validate private token paths now; values remain host-only and are reread.
        for key in ('developer_token_file','reviewer_token_file'):_token(getattr(self,key))
        if self.fleet_template is not None:name(self.fleet_template)
        if self.fleet_host_ref is not None:name(self.fleet_host_ref)
        if self.session_id is None and self.fleet_template is None:raise ValueError('registered session or approved future-session template required')


def _validate_resources(projects,manager_file,database):
    values=list(projects.values())
    def overlap(a,b):return a==b or a in b.parents or b in a.parents
    for i,host in enumerate(values):
        for other in values[i+1:]:
            if overlap(host.source,other.source):raise ValueError('distinct non-nested project source roots required')
        for other in values:
            if overlap(host.source,other.candidates) or overlap(host.source,other.runtime):raise ValueError('source overlaps trusted candidate or runtime root')
            for private in (other.developer_token_file,other.reviewer_token_file,manager_file,database):
                private=Path(private).absolute()
                if private==host.source or host.source in private.parents:raise ValueError('host credential or database inside project source')


class ContinuousHost:
    def __init__(self,core,manager_token_file,projects,*,fleet=None):
        if not isinstance(core,ThreeTier) or type(projects) is not dict or not projects:raise ValueError('registered three-tier host required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.manager_token_file=Path(manager_token_file);_token(self.manager_token_file)
        self.projects=dict(projects);self.fleet=fleet;self._review_fences={};self._session_refs={}
        for project,config in self.projects.items():
            if not isinstance(config,ProjectHost) or project!=config.project:raise ValueError('exact project host scope required')
        _validate_resources(self.projects,self.manager_token_file,self.store.path)
        with self.store._transaction() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS continuous_host_assignments (
                task_id TEXT PRIMARY KEY,version INTEGER,manifest_digest TEXT,action_key TEXT,
                assignment TEXT,phase TEXT,receipt TEXT,candidate_head TEXT,candidate_path TEXT,updated REAL);
                CREATE TABLE IF NOT EXISTS continuous_host_sessions (
                project TEXT PRIMARY KEY,session_id TEXT,creation_ref TEXT,updated REAL);
                CREATE TABLE IF NOT EXISTS continuous_host_history (action_key TEXT PRIMARY KEY,record TEXT NOT NULL);''')
        for project in self.projects:
            if project in core.source_observers or project in core.review_hosts:raise ValueError('do not replace an already registered verification host')
            core.source_observers[project]=self.observe
            core.review_hosts[project]=self.review

    def manager_token(self):return _token(self.manager_token_file)

    def _task(self,request):
        token=self.manager_token();task=self.store.get_task(token,request.task_id)
        config=self.registry.get(token,request.project)
        if (task['project'],task['version'],task['manifest_digest'],config['fingerprint'])!=(request.project,request.version,request.manifest_digest,request.config_fingerprint):raise Conflict('continuous host action binding changed')
        host=self.projects.get(request.project)
        if host is None or task['manifest']['repository']!=host.repository:raise PermissionDenied('project execution host unavailable')
        if any(check not in host.checks for check in task['manifest']['checks']):raise Conflict('approved check not registered')
        return task,host,token

    def _row(self,task_id):
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM continuous_host_assignments WHERE task_id=?',(task_id,)).fetchone()
            return dict(row) if row else None

    def _receipt(self,request,state='observed',suffix='complete'):
        from .continuous_manager import ActionReceipt
        return ActionReceipt(request.action_key,request.task_id,request.phase,state,
                             'continuous-host/'+request.phase+'/'+suffix,self.store._now())

    def _session(self,task,host,token,key):
        with self.store._transaction() as c:
            prior=c.execute('SELECT session_id FROM continuous_host_sessions WHERE project=?',(host.project,)).fetchone()
        session=prior['session_id'] if prior else host.session_id
        if session is None:
            if self.fleet is None or host.fleet_template is None:raise Conflict('future worker provisioning not connected')
            created=self.fleet.create_for_task(token,task['id'],host.fleet_template,key)
            if created.get('state')!='created' or not created.get('external_ref'):raise Conflict('session creation requires reconciliation')
            session=created['external_ref']
            # Canonical CLI external_ref may use its UUID; discovery is final.
            observations=host.developer_transport.discover(host.project)
            matches=[o for o in observations if o.external_ref==session and o.can_receive]
            if len(matches)!=1:raise Conflict('created session reception not observed')
            session=matches[0].session_id
            with self.store._transaction() as c:
                c.execute('INSERT OR REPLACE INTO continuous_host_sessions VALUES (?,?,?,?)',(host.project,session,created['id'],self.store._now()))
        observations=host.developer_transport.discover(host.project)
        matches=[o for o in observations if o.session_id==session and o.can_receive and o.project==host.project and o.repository==host.repository]
        if len(matches)!=1:raise Conflict('registered development receiver unavailable')
        self._session_refs[(host.project,session)]=matches[0].external_ref
        # Refresh base-bound metadata only while the checkout is clean. After a
        # real development turn it is expected to be dirty and not dispatchable.
        if self.fleet is not None and host.fleet_host_ref is not None:
            self.fleet.discover(token,host.project,host.fleet_host_ref)
        return session

    def start(self,request,heartbeat):
        task,host,token=self._task(request);heartbeat()
        source=_observe(host.source)
        if source['sha']!=task['manifest']['base_sha'] or not source['tracked_and_visible_untracked_clean']:return self._receipt(request,'not_ready','source_not_ready')
        self._session(task,host,token,request.action_key);heartbeat()
        self.core.start(token,task['id'],host.developer_id,request.action_key)
        return self._receipt(request)

    def dispatch(self,request,heartbeat,*,rework=False):
        task,host,token=self._task(request);heartbeat()
        source=_observe(host.source)
        if not source['tracked_and_visible_untracked_clean']:raise Conflict('dirty development checkout requires reconciliation')
        if not rework and source['sha']!=task['manifest']['base_sha']:raise Conflict('development base differs from approved task')
        prior=self._row(task['id'])
        if prior is not None:
            if not rework or prior['phase']!='frozen' or source['sha']!=prior['candidate_head']:raise Conflict('prior host execution exists; do not replay')
            with self.store._transaction() as c:
                c.execute('INSERT OR IGNORE INTO continuous_host_history VALUES (?,?)',(prior['action_key'],canonical_json(prior)))
            from .fleet_codex import CodexHost
            if isinstance(host.developer_transport,CodexHost):
                binding=host.developer_transport.bindings[host.project]
                host.developer_transport.bindings[host.project]=replace(binding,allowed_base_shas=(source['sha'],))
        session=self._session(task,host,token,request.action_key)
        developer=_token(host.developer_token_file);coord=Coordination(self.store)
        coord.readiness(token,token_hash(developer),ready=True,reason='ready',ttl=300)
        if not rework:coord.route(token,task['id'],worker_hash=token_hash(developer))
        assignment=(self.core.claim_rework(developer,task['id']) if rework else self.core.claim_development(developer,task['id']))
        if assignment is None:
            current=self.core.get(token,task['id']);upstream=self.store.get_task(token,task['id'])
            if current['state']==request.state and upstream['state']=='queued':return self._receipt(request,'not_ready','capacity_no_execution')
            return self._receipt(request,'unavailable','capacity_state_changed')
        # Private host checkpoint is never sent to the model.
        stored_assignment={**assignment,'host_start_sha':source['sha']}
        with self.store._transaction() as c:
            c.execute("INSERT OR REPLACE INTO continuous_host_assignments VALUES (?,?,?,?,?,'executing',NULL,NULL,NULL,?)",
                      (task['id'],task['version'],task['manifest_digest'],request.action_key,canonical_json(stored_assignment),self.store._now()))
        def fence():
            heartbeat()
            fresh=self.store.heartbeat(developer,task['id'],assignment['lease_token'],lease_seconds=300,progress='registered_codex_execution')
            if fresh.get('cancel_requested') is not False:raise Conflict('development execution cancelled')
            return True
        try:
            result=host.developer_transport.execute(host.project,assignment,session,heartbeat=fence)
            from .fleet_codex import ExecutionReceipt
            if not isinstance(result,ExecutionReceipt) or result.project!=host.project or result.task_id!=task['id'] or result.session_id!=session or not result.turn_completed or result.process_exit!=0:raise Conflict('actual Codex completion not observed')
            fence()
            if self.fleet is not None and host.fleet_host_ref is not None:
                external=self._session_refs.get((host.project,session))
                if external is None:raise Conflict('executed session identity unavailable')
                self.fleet.verify_delivery(token,host.project,host.fleet_host_ref,external,task['id'])
            with self.store._transaction() as c:
                c.execute("UPDATE continuous_host_assignments SET phase='executed',receipt=?,updated=? WHERE task_id=? AND phase='executing'",
                          (canonical_json(asdict(result)),self.store._now(),task['id']))
        except BaseException:
            with self.store._transaction() as c:c.execute("UPDATE continuous_host_assignments SET phase='uncertain',updated=? WHERE task_id=?",(self.store._now(),task['id']))
            raise
        return self._receipt(request)

    def collect(self,request,heartbeat):
        task,host,token=self._task(request);row=self._row(task['id']);heartbeat()
        if row is None or row['phase']!='executed' or not row['receipt']:raise Conflict('actual completed development receipt unavailable')
        assignment=json.loads(row['assignment']);receipt=json.loads(row['receipt'])
        if _observe(host.source)['sha']!=assignment.get('host_start_sha'):raise Conflict('development changed registered starting HEAD')
        if not receipt.get('turn_completed') or receipt.get('process_exit')!=0:raise Conflict('developer completion unverified')
        developer=_token(host.developer_token_file)
        fresh=self.store.heartbeat(developer,task['id'],assignment['lease_token'],lease_seconds=300,progress='freeze_candidate')
        if fresh.get('cancel_requested') is not False:raise Conflict('developer lease cancelled')
        changed=_git(host.source,'diff','--no-ext-diff','--no-textconv','--no-renames','--name-only','-z','HEAD','--').split(b'\0')
        untracked=_git(host.source,'ls-files','--others','--exclude-standard','-z').split(b'\0')
        paths=sorted(set(p.decode('utf-8') for p in changed+untracked if p))
        policy=Coordination(self.store).get_policy(token,task['id'])
        if policy is None or scope_violations(paths,policy['allowed_paths']):raise Conflict('developer changed unapproved paths')
        if not paths:raise Conflict('development produced no source change')
        with self.store._transaction() as c:c.execute("UPDATE continuous_host_assignments SET phase='freezing',updated=? WHERE task_id=? AND phase='executed'",(self.store._now(),task['id']))
        heartbeat();_git(host.source,'add','--',*paths)
        heartbeat();_git(host.source,'commit','-m','Taesan frozen development '+task['id']+' '+request.action_key)
        head=_git(host.source,'rev-parse','HEAD').decode().strip();_sha(head)
        frozen=host.candidates/(task['id']+'-'+head)
        if frozen.exists():raise Conflict('candidate destination already exists; reconcile')
        heartbeat();_git(host.source,'worktree','add','--detach',str(frozen),head)
        _observe(frozen)
        with self.store._transaction() as c:
            c.execute("UPDATE continuous_host_assignments SET phase='frozen',candidate_head=?,candidate_path=?,updated=? WHERE task_id=?",
                      (head,str(frozen),self.store._now(),task['id']))
        heartbeat()
        claims=[{'name':'codex_turn_completed','passed':True}]
        summary=('Registered Codex turn completed. Command attempts: '+str(receipt.get('command_attempts',0))+'; failures: '+str(receipt.get('command_failures',0))+'. Independent review is separate.')
        self.store.report(developer,task['id'],assignment['lease_token'],{'head_sha':head,'summary':summary,'checks':claims})
        heartbeat();self.core.submit_candidate(developer,task['id']);return self._receipt(request)

    def _runner(self,task):
        host=self.projects[task['project']];row=self._row(task['id'])
        if row is None or not row['candidate_path'] or row['version']!=task['version'] or row['manifest_digest']!=task['manifest_digest']:raise Conflict('frozen host candidate unavailable')
        source=_physical(row['candidate_path']);_guard_git_filters(source)
        if source.parent!=host.candidates or source.name!=task['id']+'-'+row['candidate_head']:raise Conflict('candidate path binding differs')
        return FixedReviewRunner(ReviewProfile(host.project,host.repository,task['manifest']['base_sha'],row['candidate_head'],source,host.runtime,host.checks),engine=host.review_engine,clock=self.store.clock)

    def observe(self,task):return self._runner(task).observe(task)

    def review(self,candidate,reviewer,*,heartbeat):
        task=candidate['task'];host=self.projects[task['project']];runner=self._runner(task)
        outer=self._review_fences.get(task['id'])
        if outer is None:raise Conflict('continuous review supervisor fence unavailable')
        def fence():
            outer();return heartbeat()
        fence();transport=host.reviewer_factory(runner.profile.source,heartbeat=fence)
        fence();semantic=transport.assess(host.project,candidate,reviewer,heartbeat=fence)
        from .three_tier import CodeAssessment
        if not isinstance(semantic,CodeAssessment) or semantic.candidate_digest!=candidate['digest'] or semantic.reviewer_subject!=reviewer['subject']:raise Conflict('actual independent code assessment unavailable')
        fence();mechanical=runner(candidate,reviewer,heartbeat=fence)
        return replace(mechanical,code_assessment=semantic)

    def review_phase(self,request,heartbeat):
        task,host,_=self._task(request);reviewer=_token(host.reviewer_token_file);heartbeat()
        lease=self.core.claim_review(reviewer,task['id'],lease_seconds=600)
        self._review_fences[task['id']]=heartbeat
        try:
            heartbeat();self.core.verify_review(reviewer,task['id'],lease['lease_token'])
        finally:self._review_fences.pop(task['id'],None)
        return self._receipt(request)

    def decide(self,request,heartbeat,*,rework=False):
        _,_,token=self._task(request);heartbeat()
        self.core.decide(token,request.task_id,request.candidate_digest,decision='rework' if rework else 'accept')
        return self._receipt(request)

    def next(self,request,heartbeat):
        _,host,token=self._task(request);heartbeat()
        self.core.approve_next(token,request.task_id,request.next_task_id,request.candidate_digest,request.action_key,developer_id=host.developer_id)
        return self._receipt(request)

    def reconcile(self,request,heartbeat):
        task,_,token=self._task(request);row=self._row(task['id']);heartbeat()
        if row and row['phase'] in ('executing','uncertain','freezing'):
            return self._receipt(request,'unavailable','action_bound_stop_evidence_required')
        current=self.core.get(token,task['id'])
        if row and row['phase']=='executed' and current['state'] in ('developing','rework_developing'):
            return self.collect(request,heartbeat)
        if current['state'] in ('candidate_ready','review_passed','review_failed','accepted','rework_queued'):return self._receipt(request)
        # Never assume a process stopped from a timeout, missing receipt or exit0.
        if current['state'] in ('reviewing','needs_verification'):
            self.core.reconcile_review(token,task['id']);return self._receipt(request)
        return self._receipt(request,'unavailable','exact_stop_or_receipt_required')

    def handlers(self):
        return {'start':self.start,'dispatch_development':self.dispatch,'collect_development':self.collect,
                'review':self.review_phase,'collect_review':self.reconcile,
                'technical_decision':self.decide,'rework_decision':lambda r,h:self.decide(r,h,rework=True),
                'dispatch_rework':lambda r,h:self.dispatch(r,h,rework=True),'reconcile':self.reconcile,'approve_next':self.next}


def load_host(config_file):
    """Load only fixed private host paths; no dynamic imports or HTTP commands.

    Database, role memberships, capacity and future-worker templates must already
    exist through the owner's approved registration flow. This loader creates no
    user credentials, approvals, templates or requirements. Control state is a
    separate private directory; source/candidate work roots cannot be inside it.
    """
    from .fleet import Fleet
    from .fleet_codex import CodexHost,ProjectBinding
    from .continuous_manager import ContinuousManager
    config=_private_json(config_file)
    required={'version','application_root','expected_source_commit','data_root','database','manager_token_file','instance_id','projects'}
    if not required<=set(config) or set(config)-required-{'poll_seconds'} or config['version']!=1:
        raise ValueError('exact host configuration schema required')
    application=_physical(config['application_root']);expected=config['expected_source_commit'];_sha(expected)
    observation=_observe(application)
    if observation['sha']!=expected or not observation['tracked_and_visible_untracked_clean']:
        raise Conflict('trusted host application differs from pinned clean commit')
    # The running driver must be the module from the registered application.
    if Path(__file__).absolute().parents[1]!=application:raise Conflict('running host code differs from registered application')
    data=_physical(config['data_root'],private=True)
    database=_private_file(config['database'])
    if database.parent!=data:raise ValueError('persistent database must be in private data root')
    manager_path=_private_file(config['manager_token_file']);manager=_token(manager_path)
    instance=config['instance_id'];name(instance)
    poll=config.get('poll_seconds',2)
    if type(poll) not in (int,float) or not 1<=poll<=60:raise ValueError('bounded poll interval required')
    entries=config['projects']
    if type(entries) is not list or not 1<=len(entries)<=100:raise ValueError('explicit bounded project list required')
    store=Store(database);registry=Registry(store);core=ThreeTier(store,registry)
    projects={};providers={}
    forbidden=tuple([manager_path,database,data,application,Path(config_file).absolute()]+[Path(value).absolute() for entry in entries if type(entry) is dict for value in (entry.get('developer_token_file','/nonexistent'),entry.get('reviewer_token_file','/nonexistent'))])
    for entry in entries:
        needed={'project','repository','source','candidates','runtime','developer_id','reviewer_id','developer_token_file','reviewer_token_file','checks','developer','reviewer'}
        if type(entry) is not dict or not needed<=set(entry) or set(entry)-needed-{'session_id','fleet_template','fleet_host_ref'}:raise ValueError('exact project host schema required')
        project=entry['project'];name(project)
        if project in projects:raise ValueError('duplicate project host')
        source=_physical(entry['source']);candidates=_physical(entry['candidates'],private=True)
        if any(path==data or data in path.parents for path in (source,candidates)):
            raise ValueError('control data root must be separate from source and candidate work roots')
        if source==application or source in application.parents or application in source.parents:raise ValueError('development source cannot overlap running host application')
        runtime=_physical(entry['runtime']);devfile=_private_file(entry['developer_token_file']);reviewfile=_private_file(entry['reviewer_token_file'])
        dev_token=_token(devfile);review_token=_token(reviewfile)
        # A source cannot contain host credentials, state, profiles or runtime.
        private_paths=[data,manager_path,devfile,reviewfile,Path(config_file).absolute(),candidates,runtime]
        transport_settings={}
        for role in ('developer','reviewer'):
            setting=entry[role]
            if type(setting) is not dict or set(setting)-{'binary','codex_home','require_origin','execution_timeout'} or not {'binary','codex_home'}<=set(setting):raise ValueError('fixed Codex transport schema required')
            settings={**setting,'codex_home':_physical(setting['codex_home'],private=True)}
            transport_settings[role]=settings;private_paths.append(settings['codex_home'])
        if any(p==source or source in p.parents for p in private_paths):raise ValueError('host state or credentials cannot be inside development source')
        if transport_settings['developer']['codex_home']==transport_settings['reviewer']['codex_home']:
            raise ValueError('separate developer and reviewer coding profiles required')
        def authorize(assignment,session_id,*,project=project,devfile=devfile):
            # Session comes only from fixed config/Fleet metadata. The lease and
            # exact owner approval remain live checks on every execution fence.
            current_token=_token(devfile)
            with store._transaction() as c:
                task,_,row,member=core._bound(c,current_token,assignment['id'],'developer')
                store._lease(task,store._authenticate(c,current_token),assignment['lease_token'],c=c)
                return (task['state']=='running' and row['state'] in ('developing','rework_developing') and
                        task['project']==project and task['version']==assignment['version'] and
                        task['manifest_digest']==assignment['manifest_digest'] and
                        member['credential_hash']==token_hash(current_token) and task['lease_expires']>store._now())
        devsetting=transport_settings['developer']
        developer=CodexHost(devsetting['binary'],devsetting['codex_home'],{
            project:ProjectBinding(project,entry['repository'],source,require_origin=devsetting.get('require_origin',True))},
            authorize=authorize,clock=store.clock,execution_timeout=devsetting.get('execution_timeout',180))
        def reviewer_factory(frozen,*,heartbeat,project=project,repository=entry['repository'],settings=transport_settings['reviewer'],reviewfile=reviewfile,candidates=candidates):
            frozen=_physical(frozen)
            if frozen.parent!=candidates:raise Conflict('review checkout outside registered candidates')
            def authorize_review(candidate,reviewer):
                token=_token(reviewfile)
                with store._transaction() as c:
                    task,_,row,_=core._bound(c,token,candidate['task']['id'],'reviewer')
                    actor=core._actor(c,token,project,'reviewer',core._config(c,token,project))
                    return (row['state']=='reviewing' and json.loads(row['candidate'])['digest']==candidate['digest'] and row['lease_expires']>store._now() and
                            actor['subject']==reviewer['subject'] and task['project']==project)
            transport=CodexHost(settings['binary'],settings['codex_home'],{
                project:ProjectBinding(project,repository,frozen,require_origin=settings.get('require_origin',True))},
                authorize_review=authorize_review,clock=store.clock,execution_timeout=settings.get('execution_timeout',180))
            transport.verify_confidentiality(project,forbidden,heartbeat=heartbeat)
            return transport
        projects[project]=ProjectHost(project,entry['repository'],source,candidates,runtime,
            entry['developer_id'],entry['reviewer_id'],devfile,reviewfile,entry['checks'],developer,reviewer_factory,
            session_id=entry.get('session_id'),fleet_template=entry.get('fleet_template'),fleet_host_ref=entry.get('fleet_host_ref'))
        if entry.get('fleet_template') is not None:
            host_ref=entry.get('fleet_host_ref');name(host_ref)
            providers[(project,host_ref)]=developer.provider(project)
    # Cross-project roots must not expose another project's trusted runtime,
    # frozen artifacts or CLI credentials as developer-writable source.
    _validate_resources(projects,manager_path,database)
    all_profiles=[settings['codex_home'] for entry in entries for settings in (entry['developer'],entry['reviewer'])]
    for host in projects.values():
        if any(Path(p).absolute()==host.source or host.source in Path(p).absolute().parents or Path(p).absolute() in host.source.parents for p in all_profiles):raise ValueError('coding profile inside project source')
    # Every coding profile is host-private, including other projects' profiles.
    # The shared closure is finalized before any developer/reviewer probe runs.
    forbidden=tuple(dict.fromkeys((*forbidden,*(Path(p).absolute() for p in all_profiles))))
    # Success is an actual non-model OS sandbox probe, never a config flag.
    for host in projects.values():host.developer_transport.verify_confidentiality(host.project,forbidden)
    fleet=Fleet(store,registry,providers=providers) if providers else None
    host=ContinuousHost(core,manager_path,projects,fleet=fleet)
    controller=ContinuousManager(core,handlers=host.handlers())
    # Validate current scopes without starting work or changing registration.
    controller.status(manager,tuple(projects))
    return host,controller,{'projects':tuple(projects),'instance_id':instance,'poll_seconds':poll,'expected_source_commit':expected}
