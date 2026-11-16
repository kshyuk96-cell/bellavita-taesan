"""Real three-tier orchestration over disposable, strictly synthetic command workers.

The developer and semantic reviewer run fixed bounded subprocesses over JSON in
one temporary Git repository. They are not Codex/model agents. Mechanical proof
comes only from the unmodified default FixedReviewRunner. If its actual isolation
is unavailable, the workflow stays blocked; this module supplies no replacement
engine, mocked evidence, or acceptance shortcut. No network, customer source,
deployment, external tester, credential import or persistent service is used.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import resource
import signal
import stat
import subprocess
import tempfile
import time

from .continuous_host import ContinuousHost, ProjectHost, _git
from .continuous_manager import ContinuousManager
from .coordination import Coordination
from .fleet_codex import ExecutionReceipt
from .maintenance_registry import Registry
from .store import Store, Conflict, PermissionDenied, canonical_json, digest, token_hash
from .three_tier import ThreeTier, CodeAssessment
from .three_tier_runner import BubblewrapReviewEngine

PROJECT = 'synthetic-governance-flow'
REPOSITORY = 'synthetic/governance-flow'
SESSION = '00000000-0000-4000-8000-000000000091'
COMMAND_TIMEOUT = 5

# These programs and their arguments are host constants, never task instructions.
DEVELOPER_PROGRAM = '''import json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve()
p=root/'src'/'value.json'
if p.is_symlink() or not p.is_file() or p.stat().st_size>64:raise SystemExit(4)
old=json.loads(p.read_text())
if type(old) is not dict or set(old)!={'value'} or type(old['value']) is not int or old['value'] not in (0,1,2):raise SystemExit(5)
p.write_text(json.dumps({'value':old['value']+1})+'\\n')
print(json.dumps({'written_value':old['value']+1}))
'''
SEMANTIC_PROGRAM = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1])/'src'/'value.json'
if p.is_symlink() or not p.is_file() or p.stat().st_size>64:raise SystemExit(4)
data=json.loads(p.read_text())
if type(data) is not dict or set(data)!={'value'} or type(data['value']) is not int:raise SystemExit(5)
matched=data['value']==2
print(json.dumps({'recommendation':'pass' if matched else 'rework','observed_value':data['value'],'expected_value':2}))
raise SystemExit(0 if matched else 1)
'''
MECHANICAL_PROGRAM = '''import json,pathlib
p=pathlib.Path('/work/src/value.json')
assert p.is_file() and not p.is_symlink() and p.stat().st_size<=64
data=json.loads(p.read_text())
assert type(data) is dict and set(data)=={'value'} and type(data['value']) is int and 0<data['value']<=3
'''


def _limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)


def _command(argv, cwd, heartbeat):
    """Execute only module-built commands; success here is not sandbox evidence."""
    heartbeat()
    started = time.monotonic()
    process = subprocess.Popen(list(argv), cwd=cwd, env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, preexec_fn=_limits)
    try:
        while process.poll() is None:
            heartbeat()
            if time.monotonic() - started > COMMAND_TIMEOUT:
                raise TimeoutError('synthetic fixed command timed out')
            time.sleep(.01)
        content = process.stdout.read(8193)
        if len(content) > 8192:raise ValueError('synthetic fixed output exceeded bound')
        heartbeat()
        return {'exit_code':process.returncode, 'elapsed_seconds':round(time.monotonic()-started,6),
                'stdout_digest':digest(content.decode()), 'output':json.loads(content)}
    finally:
        if process.poll() is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=2)
        if process.stdout:process.stdout.close()


@dataclass(frozen=True)
class _CommandSession:
    """Host protocol metadata for the synthetic command worker, not a Codex UI."""
    project:str
    repository:str
    source_sha:str
    session_id:str=SESSION
    external_ref:str=SESSION
    can_receive:bool=True


class _Developer:
    def __init__(self,source):self.source=source;self.outcomes=[]

    def discover(self,project):
        if project!=PROJECT:raise PermissionDenied('synthetic project required')
        return (_CommandSession(PROJECT,REPOSITORY,_git(self.source,'rev-parse','HEAD').decode().strip()),)

    def execute(self,project,assignment,session,*,heartbeat):
        if (project,session,assignment['project'],assignment['manifest']['repository'])!=(PROJECT,SESSION,PROJECT,REPOSITORY):
            raise PermissionDenied('synthetic command worker scope mismatch')
        argv=('/usr/bin/python3','-I','-S','-c',DEVELOPER_PROGRAM,str(self.source))
        result=_command(argv,self.source,heartbeat)
        if result['exit_code']!=0 or type(result['output']) is not dict or set(result['output'])!={'written_value'}:
            raise Conflict('synthetic developer outcome unavailable')
        recorded={'task_id':assignment['id'],'command_digest':digest(list(argv)),
            'exit_code':result['exit_code'],'elapsed_seconds':result['elapsed_seconds'],
            'stdout_digest':result['stdout_digest'],'written_value':result['output']['written_value']}
        self.outcomes.append(recorded)
        return ExecutionReceipt(PROJECT,assignment['id'],SESSION,'synthetic_command_completed',0,True,1,1,0,
            ({'command_digest':recorded['command_digest'],'exit_code':0},),digest(recorded),time.time())


class _SemanticReviewer:
    def __init__(self,frozen,candidates,runtime,outcomes):
        self.frozen,self.candidates,self.runtime,self.outcomes=frozen,candidates,runtime,outcomes

    def assess(self,project,candidate,reviewer,*,heartbeat):
        if project!=PROJECT or self.frozen.parent!=self.candidates or reviewer['role']!='reviewer':
            raise PermissionDenied('independent synthetic reviewer required')
        head=_git(self.frozen,'rev-parse','HEAD').decode().strip()
        if head!=candidate['source']['head_sha']:raise Conflict('frozen semantic source differs')
        argv=('/usr/bin/python3','-I','-S',str(self.runtime/'semantic_check.py'),str(self.frozen))
        result=_command(argv,self.frozen,heartbeat);output=result['output']
        if (type(output) is not dict or set(output)!={'recommendation','observed_value','expected_value'}
                or output['recommendation'] not in ('pass','rework') or output['expected_value']!=2
                or type(output['observed_value']) is not int
                or result['exit_code']!=(0 if output['recommendation']=='pass' else 1)):
            raise Conflict('independent semantic command evidence malformed')
        record={'candidate_digest':candidate['digest'],'head_sha':head,'recommendation':output['recommendation'],
            'observed_value':output['observed_value'],'command_digest':digest(list(argv)),
            'exit_code':result['exit_code'],'elapsed_seconds':result['elapsed_seconds'],
            'stdout_digest':result['stdout_digest'],'worker_scope':'fixed_synthetic_semantic_command'}
        self.outcomes.append(record)
        return CodeAssessment(candidate['digest'],reviewer['subject'],output['recommendation'],digest(output),
            'synthetic-semantic/'+digest(record)[:32],time.time())


def _token_file(root,name,token):
    path=root/name
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as stream:stream.write(token+'\n')
    return path


def _parent(value):
    if value is None:return None
    root=Path(value).absolute()
    if '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('private physical rehearsal parent required')
    info=root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('private owned rehearsal parent required')
    return root


def rehearse_governance_flow(*,work_parent=None,max_ticks=30):
    """Create fresh fixture authority/state, then destroy it; return safe evidence.

    This authors two *synthetic* owner approvals only (parent and dependent child).
    No passed boolean can enable or replace mechanical isolation. On an unsupported
    host the actual independent review fails closed and the return is blocked.
    """
    if type(max_ticks) is not int or not 4<=max_ticks<=60:raise ValueError('bounded synthetic tick count required')
    parent=_parent(work_parent)
    with tempfile.TemporaryDirectory(prefix='taesan-governance-flow-',dir=parent) as directory:
        return _rehearse(Path(directory),max_ticks)


def _rehearse(root,max_ticks):
    source=root/'source';source.mkdir(mode=0o700);(source/'src').mkdir()
    (source/'src/value.json').write_text('{"value":0}\n')
    _git(source,'init','-q');_git(source,'add','src/value.json');_git(source,'commit','-qm','Synthetic governance base')
    base=_git(source,'rev-parse','HEAD').decode().strip()
    candidates=root/'candidates';candidates.mkdir(mode=0o700)
    runtime=root/'runtime';runtime.mkdir(mode=0o700)
    (runtime/'semantic_check.py').write_text(SEMANTIC_PROGRAM)
    (runtime/'mechanical_check.py').write_text(MECHANICAL_PROGRAM)
    store=Store(root/'state.sqlite3');registry=Registry(store);coordination=Coordination(store)
    owner=store.add_token('synthetic-owner','owner',['*'])
    registry.register(owner,PROJECT,{'company':'synthetic','name':'Disposable governance command rehearsal',
        'source_kind':'github','source_ref':REPOSITORY,'branch':'main','services':[],'maintenance_enabled':True})
    manager=store.add_token('synthetic-manager','manager',[PROJECT])
    developer=store.add_token('synthetic-developer','worker',[PROJECT])
    reviewer=store.add_token('synthetic-reviewer','worker',[PROJECT])
    core=ThreeTier(store,registry);core.set_limits(owner,None);core.set_limits(owner,PROJECT)
    for member,credential,role in (('manager',manager,'taesan'),('developer',developer,'developer'),('reviewer',reviewer,'reviewer')):
        core.register(owner,PROJECT,member,token_hash(credential),role,agent_id=None if role=='taesan' else 'synthetic-'+member)
    manager_file=_token_file(root,'manager.token',manager)
    developer_file=_token_file(root,'developer.token',developer)
    reviewer_file=_token_file(root,'reviewer.token',reviewer)
    transport=_Developer(source);semantic=[]
    project=ProjectHost(PROJECT,REPOSITORY,source,candidates,runtime,'developer','reviewer',developer_file,reviewer_file,
        {'mechanical-structure':('/usr/bin/python3','-I','-S','/runtime/mechanical_check.py')},transport,
        lambda frozen,**kwargs:_SemanticReviewer(frozen,candidates,runtime,semantic),session_id=SESSION)
    host=ContinuousHost(core,manager_file,{PROJECT:project});controller=ContinuousManager(core,handlers=host.handlers())
    sequence=0
    def task(label,*,approved=True,sha=base,dependency=None):
        nonlocal sequence
        sequence+=1
        value=store.submit(owner,'synthetic-'+str(sequence),{'project':PROJECT,'repository':REPOSITORY,'base_sha':sha,
            'kind':'development','description':'Synthetic command-only '+label+'; not Codex/model work.',
            'checks':['mechanical-structure']})
        coordination.set_policy(owner,value['id'],value['version'],value['manifest_digest'],
            allowed_paths=['src/'],dependencies=[dependency] if dependency else [])
        if approved:store.approve(owner,value['id'],value['version'],value['manifest_digest'],
            policy_digest=digest(coordination.get_policy(owner,value['id'])))
        return store.get_task(owner,value['id'])
    current=task('parent')
    denied_start=task('unapproved-start',approved=False)
    unapproved_start_denied=False
    try:core.start(manager,denied_start['id'],'developer','unapproved-start')
    except (Conflict,PermissionDenied):unapproved_start_denied=True
    ticks=[];states=[];child=None;sibling=None;unapproved_next_denied=False
    isolation=BubblewrapReviewEngine().probe()
    try:
        for _ in range(max_ticks):
            tick=controller.tick(manager,[PROJECT],'synthetic-governance-loop',limit=1)
            ticks.extend({'task_id':a['task_id'],'phase':a['phase'],'state':a['state'],
                'observed_core_state':a.get('observed_core_state')} for a in tick['actions'])
            status=core.get(manager,current['id']);states.append(status['state'])
            if status['state']=='accepted':break
            if any(a['state'] in ('uncertain','project_quarantined') for a in tick['actions']):break
        parent_state=core.get(manager,current['id'])
        if parent_state['technical_accepted']:
            parent_head=parent_state['source']['head_sha']
            child=task('preapproved-dependent-next',sha=parent_head,dependency=current['id'])
            sibling=task('unapproved-dependent-next',approved=False,sha=parent_head,dependency=current['id'])
            try:core.approve_next(manager,current['id'],sibling['id'],parent_state['candidate_digest'],'deny-unapproved-next')
            except (Conflict,PermissionDenied):unapproved_next_denied=True
            for _ in range(4):
                tick=controller.tick(manager,[PROJECT],'synthetic-governance-loop',limit=1)
                ticks.extend({'task_id':a['task_id'],'phase':a['phase'],'state':a['state'],
                    'observed_core_state':a.get('observed_core_state')} for a in tick['actions'])
                if any(outcome['task_id']==child['id'] for outcome in transport.outcomes):break
        parent_state=core.get(manager,current['id'])
        next_dispatched=bool(child and any(o['task_id']==child['id'] for o in transport.outcomes))
        unapproved_preserved=store.get_task(manager,denied_start['id'])['state']=='pending_approval' and (
            sibling is None or store.get_task(manager,sibling['id'])['state']=='pending_approval')
        mechanical=parent_state['checks']
        full=(parent_state['technical_accepted'] and parent_state['rework_count']==1
            and [r['recommendation'] for r in semantic]==['rework','pass']
            and 'rework_queued' in states and next_dispatched and unapproved_start_denied and unapproved_next_denied and unapproved_preserved)
        try:
            from .workflow_trace import WorkflowTrace
            trace=WorkflowTrace(core).report(manager,current['id'])
        except Exception:trace={'scope':'recorded_database_snapshot','state':'unavailable',
            'current_technical_acceptance_verified':False,'execution_authorized':False}
        return {'schema_version':'governance-flow-rehearsal/v1','scope':'disposable_synthetic_command_workers',
            'state':'passed' if full else 'blocked','completed_full_flow':bool(full),
            'blocker':None if full else 'mechanical_isolation_unavailable' if isolation.get('isolation_verified') is not True else 'workflow_not_completed',
            'mechanical_probe':isolation,'parent_task_id':current['id'],'parent_state':parent_state['state'],
            'parent_candidate_digest':parent_state['candidate_digest'],'parent_head':parent_state['source']['head_sha'] if parent_state['source'] else None,
            'parent_technical_accepted':parent_state['technical_accepted'],
            'manager_rework_observed':'rework_queued' in states,'rework_count':parent_state['rework_count'],
            'developer_commands':transport.outcomes,'semantic_commands':semantic,
            'mechanical_checks':mechanical,'child_task_id':child['id'] if child else None,
            'preapproved_dependent_dispatched':next_dispatched,
            'unapproved_start_denied':unapproved_start_denied,'unapproved_next_denied':unapproved_next_denied,
            'unapproved_next_guard_exercised':sibling is not None,'unapproved_tasks_remain_pending':unapproved_preserved,
            'synthetic_owner_approvals':2 if child else 1,'actions':ticks,'trace':trace,
            'workspaces_destroyed_on_return':True,'actual_codex_execution_verified':False,
            'model_calls':0,'customer_repositories_used':False,'operational_change':False,
            'external_tester_engine_used':False,'deployment_performed':False,'always_on_verified':False}
    finally:controller.release(manager,[PROJECT],'synthetic-governance-loop')
