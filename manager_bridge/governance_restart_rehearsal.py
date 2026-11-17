"""Disposable two-process manager restarts; never an OS reboot or native Codex proof.

Fixed synthetic developer/semantic commands are reused from the flow rehearsal.
Real FixedReviewRunner remains the sole mechanical verifier. No mock, substitute
isolation engine, customer path, model, network service or daemon is involved.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from .continuous_host import ContinuousHost,ProjectHost,_git
from .continuous_manager import ContinuousManager
from .coordination import Coordination
from .control import secret
from .governance_flow_rehearsal import (_Developer,_SemanticReviewer,_token_file,_parent,
    PROJECT,REPOSITORY,SESSION,SEMANTIC_PROGRAM,MECHANICAL_PROGRAM)
from .maintenance_registry import Registry
from .store import Store,Conflict,canonical_json,digest,token_hash
from .three_tier import ThreeTier

STAGES=('clean_first','clean_resume','uncertain_first','uncertain_resume')
PROCESS_TIMEOUT=60


def _write(path,value):
    """A private fixture journal, not a general command transport protocol."""
    temporary=path.with_name(path.name+'.new')
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as stream:
        stream.write(canonical_json(value)+'\n');stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _journal(root,kind,record):
    path=root/'commands.json';data=json.loads(path.read_text())
    data[kind].append({**record,'manager_pid':os.getpid()})
    _write(path,data)


class _PersistentDeveloper(_Developer):
    def __init__(self,source,root,stage):super().__init__(source);self.root,self.stage=root,stage
    def execute(self,*args,**kwargs):
        receipt=super().execute(*args,**kwargs)
        _journal(self.root,'developer',self.outcomes[-1])
        if self.stage=='uncertain_first':
            # The write and private observation are durable, but the transport
            # never returns its completion receipt to ContinuousHost.
            raise ConnectionError('synthetic completion response lost after actual write')
        return receipt


class _PersistentSemantic(_SemanticReviewer):
    def __init__(self,*args,root,**kwargs):super().__init__(*args,**kwargs);self.root=root
    def assess(self,*args,**kwargs):
        value=super().assess(*args,**kwargs)
        _journal(self.root,'semantic',self.outcomes[-1])
        return value


def _bootstrap(root,scenario):
    source=root/'source';source.mkdir(mode=0o700);(source/'src').mkdir()
    (source/'src/value.json').write_text('{"value":0}\n')
    _git(source,'init','-q');_git(source,'add','src/value.json');_git(source,'commit','-qm','Synthetic restart base')
    for name in ('candidates','runtime'):(root/name).mkdir(mode=0o700)
    (root/'runtime/semantic_check.py').write_text(SEMANTIC_PROGRAM)
    (root/'runtime/mechanical_check.py').write_text(MECHANICAL_PROGRAM)
    store=Store(root/'state.sqlite3');registry=Registry(store);coord=Coordination(store)
    owner=store.add_token('synthetic-restart-owner','owner',['*'])
    registry.register(owner,PROJECT,{'company':'synthetic','name':'Disposable restart fixture','source_kind':'github',
        'source_ref':REPOSITORY,'branch':'main','services':[],'maintenance_enabled':True})
    manager=store.add_token('synthetic-restart-manager','manager',[PROJECT])
    developer=store.add_token('synthetic-restart-developer','worker',[PROJECT])
    reviewer=store.add_token('synthetic-restart-reviewer','worker',[PROJECT])
    core=ThreeTier(store,registry);core.set_limits(owner,None);core.set_limits(owner,PROJECT)
    for member,credential,role in (('manager',manager,'taesan'),('developer',developer,'developer'),('reviewer',reviewer,'reviewer')):
        core.register(owner,PROJECT,member,token_hash(credential),role,agent_id=None if role=='taesan' else 'synthetic-'+member)
    for name,credential in (('manager',manager),('developer',developer),('reviewer',reviewer)):_token_file(root,name+'.token',credential)
    task=store.submit(owner,'restart-parent',{'project':PROJECT,'repository':REPOSITORY,
        'base_sha':_git(source,'rev-parse','HEAD').decode().strip(),'kind':'development',
        'description':'Disposable synthetic process restart; no Codex/model work.','checks':['mechanical-structure']})
    coord.set_policy(owner,task['id'],task['version'],task['manifest_digest'],allowed_paths=['src/'],dependencies=[])
    store.approve(owner,task['id'],task['version'],task['manifest_digest'],policy_digest=digest(coord.get_policy(owner,task['id'])))
    _write(root/'settings.json',{'schema':'synthetic-governance-restart/v1','scenario':scenario,'task_id':task['id']})
    _write(root/'commands.json',{'developer':[],'semantic':[]})


def _child(root,stage):
    if stage not in STAGES:raise ValueError('fixed child stage required')
    root=Path(root).absolute();_parent(root)
    if not root.name.startswith('taesan-governance-restart-'):raise ValueError('disposable restart root required')
    settings=json.loads((root/'settings.json').read_text())
    scenario=stage.split('_',1)[0]
    if set(settings)!={'schema','scenario','task_id'} or settings['schema']!='synthetic-governance-restart/v1' or settings['scenario']!=scenario:
        raise ValueError('exact restart fixture required')
    store=Store(root/'state.sqlite3');registry=Registry(store);core=ThreeTier(store,registry)
    manager=secret(root/'manager.token');developer=_PersistentDeveloper(root/'source',root,stage)
    project=ProjectHost(PROJECT,REPOSITORY,root/'source',root/'candidates',root/'runtime','developer','reviewer',
        root/'developer.token',root/'reviewer.token',
        {'mechanical-structure':('/usr/bin/python3','-I','-S','/runtime/mechanical_check.py')},developer,
        lambda frozen,**kwargs:_PersistentSemantic(frozen,root/'candidates',root/'runtime',[],root=root),session_id=SESSION)
    host=ContinuousHost(core,root/'manager.token',{PROJECT:project})
    controller=ContinuousManager(core,handlers=host.handlers());instance='synthetic-'+stage;actions=[]
    try:
        for _ in range(2 if stage.endswith('_first') else 20 if scenario=='clean' else 4):
            tick=controller.tick(manager,[PROJECT],instance,limit=1)
            actions.extend({'phase':a['phase'],'state':a['state']} for a in tick['actions'])
            status=core.get(manager,settings['task_id'])
            if stage=='clean_resume' and status['state']=='accepted':break
            if scenario=='clean' and any(a['state'] in ('uncertain','project_quarantined') for a in tick['actions']):break
    finally:released=controller.release(manager,[PROJECT],instance)['released']
    status=core.get(manager,settings['task_id']);row=host._row(settings['task_id'])
    commands=json.loads((root/'commands.json').read_text())
    with store._transaction() as c:
        quarantined=bool(c.execute("SELECT 1 FROM continuous_actions WHERE project=? AND state='uncertain' LIMIT 1",(PROJECT,)).fetchone())
    identity=store.path.stat()
    return {'stage':stage,'task_id':settings['task_id'],
        'source_head':_git(root/'source','rev-parse','HEAD').decode().strip(),
        'database_identity_digest':digest({'device':identity.st_dev,'inode':identity.st_ino}),
        'manager_pid':os.getpid(),'instance_id':instance,'leader_released':released,
        'hierarchy_state':status['state'],'host_phase':row['phase'] if row else None,
        'technical_accepted':status['technical_accepted'],'rework_count':status['rework_count'],
        'developer_commands':len(commands['developer']),'semantic_commands':len(commands['semantic']),
        'source_value':json.loads((root/'source/src/value.json').read_text())['value'],
        'project_quarantined':quarantined,'actions':actions,'mechanical_checks':status['checks'],
        'operational_change':False}


def _process(root,stage):
    application=Path(__file__).resolve().parents[1]
    loader='import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("manager_bridge.governance_restart_rehearsal",run_name="__main__")'
    argv=(sys.executable,'-I','-S','-c',loader,str(application),stage,str(root))
    started=time.monotonic()
    process=subprocess.Popen(argv,cwd=application,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        output,_=process.communicate(timeout=PROCESS_TIMEOUT)
        if process.returncode or len(output)>64000:raise Conflict('restart child unverified')
        value=json.loads(output)
        if value.get('stage')!=stage or value.get('manager_pid')!=process.pid:raise Conflict('restart child identity differs')
        value['process_exit']=process.returncode;value['elapsed_seconds']=round(time.monotonic()-started,6)
        return value
    finally:
        if process.poll() is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=2)


def rehearse_governance_restart(*,work_parent=None):
    """Two sequential real manager processes per scenario; no true OS reboot."""
    parent=_parent(work_parent);scenarios={}
    for scenario in ('clean','uncertain'):
        with tempfile.TemporaryDirectory(prefix='taesan-governance-restart-',dir=parent) as directory:
            root=Path(directory);_bootstrap(root,scenario)
            first=_process(root,scenario+'_first');second=_process(root,scenario+'_resume')
            commands=json.loads((root/'commands.json').read_text())
            distinct=first['manager_pid']!=second['manager_pid'] and first['instance_id']!=second['instance_id']
            same_store=first['task_id']==second['task_id'] and first['database_identity_digest']==second['database_identity_digest']
            resumed_phases=[a['phase'] for a in second['actions']]
            if scenario=='clean':
                complete=bool(distinct and same_store and first['leader_released'] and first['host_phase']=='executed'
                    and first['developer_commands']==1 and second['developer_commands']==2
                    and second['technical_accepted'] and second['rework_count']==1
                    and [r['written_value'] for r in commands['developer']]==[1,2]
                    and [r['recommendation'] for r in commands['semantic']]==['rework','pass']
                    and resumed_phases and resumed_phases[0]=='collect_development'
                    and 'dispatch_rework' in resumed_phases and 'dispatch_development' not in resumed_phases)
                result={'state':'passed' if complete else 'blocked','completed':complete,
                    'original_command_duplicated':sum(r['written_value']==1 for r in commands['developer'])>1,
                    'original_write_receipts':sum(r['written_value']==1 for r in commands['developer']),
                    'duplicate_observation_scope':'fixed_fixture_value1_receipts',
                    'accepted_after_process_restart':second['technical_accepted']}
            else:
                complete=bool(distinct and same_store and first['leader_released'] and first['host_phase']=='uncertain'
                    and first['developer_commands']==second['developer_commands']==1
                    and first['source_value']==second['source_value']==1
                    and first['project_quarantined'] and second['project_quarantined']
                    and not second['technical_accepted'])
                result={'state':'passed' if complete else 'unverified','completed':complete,
                    'normal_restart_reexecuted':second['developer_commands']>first['developer_commands'],
                    'quarantine_preserved':second['project_quarantined']}
            scenarios[scenario]={**result,'same_task_and_database_verified':same_store,'first_process':first,'second_process':second,
                'developer_command_journal':commands['developer'],'semantic_command_journal':commands['semantic']}
    passed=all(s['completed'] for s in scenarios.values())
    return {'schema_version':'governance-restart-rehearsal/v1','scope':'disposable_synthetic_process_restart',
        'state':'passed' if passed else 'blocked','completed':passed,'scenarios':scenarios,
        'true_os_reboot_verified':False,'actual_codex_execution_verified':False,'model_calls':0,
        'customer_repositories_used':False,'external_tester_engine_used':False,'operational_change':False,
        'workspaces_destroyed_on_return':True,'always_on_verified':False}


if __name__=='__main__':
    try:
        if len(sys.argv)!=3:raise ValueError('fixed child invocation required')
        print(json.dumps(_child(sys.argv[2],sys.argv[1])))
    except Exception:
        print(json.dumps({'state':'unverified','error':'synthetic_restart_child_failed'}));raise SystemExit(1)
