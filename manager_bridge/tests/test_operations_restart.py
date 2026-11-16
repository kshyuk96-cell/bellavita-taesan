"""Two real processes, simulated lease clock, no models or customer operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def child(phase,root):
    from manager_bridge.store import Store,token_hash,digest
    from manager_bridge.maintenance_registry import Registry
    from manager_bridge.coordination import Coordination
    from manager_bridge.three_tier import ThreeTier
    from manager_bridge.continuous_manager import ContinuousManager
    store=Store(root/'state.sqlite3',clock=lambda:1000 if phase=='prepare' else 2000)
    registry=Registry(store);core=ThreeTier(store,registry)
    if phase=='prepare':
        owner=store.add_token('fixture-owner','owner',['*'])
        registry.register(owner,'fixture',{'company':'fixture','name':'Restart fixture','source_kind':'github',
            'source_ref':'fixture/restart','branch':'main','services':[],'maintenance_enabled':True})
        manager=store.add_token('fixture-manager','manager',['fixture'])
        developer=store.add_token('fixture-developer','worker',['fixture'])
        core.register(owner,'fixture','manager',token_hash(manager),'taesan')
        core.register(owner,'fixture','developer',token_hash(developer),'developer')
        core.set_limits(owner);core.set_limits(owner,'fixture')
        task=store.submit(owner,'restart-request',{'project':'fixture','repository':'fixture/restart','base_sha':'a'*40,
            'description':'Synthetic restart proof','checks':['fixture-check'],'kind':'development'})
        coordination=Coordination(store)
        policy=coordination.set_policy(owner,task['id'],1,task['manifest_digest'],allowed_paths=['src/'],dependencies=[])
        store.approve(owner,task['id'],1,task['manifest_digest'],policy_digest=digest(policy))
        credentials=root/'fixture-state.json'
        credentials.write_text(json.dumps({'manager':manager,'task_id':task['id']}));credentials.chmod(0o600)
        def start(action,heartbeat):
            heartbeat();core.start(manager,action.task_id,'developer','assigned-before-response-loss')
            # Skip controller cleanup, leaving a committed running intent.
            os._exit(0)
        ContinuousManager(core,handlers={'start':start}).tick(manager,['fixture'],'first-process')
        raise AssertionError('expected abrupt fixture process exit')
    saved=json.loads((root/'fixture-state.json').read_text());calls=[]
    with store._transaction() as c:
        before=c.execute('SELECT state FROM continuous_actions').fetchone()[0]
    scheduler=ContinuousManager(core,handlers={'start':lambda *_:calls.append('unsafe replay')})
    result=scheduler.tick(saved['manager'],['fixture'],'second-process')
    with store._transaction() as c:
        states=[row[0] for row in c.execute('SELECT state FROM continuous_actions')]
        assignments=c.execute('SELECT count(*) FROM hierarchy_tasks').fetchone()[0]
        task_state=c.execute('SELECT state FROM hierarchy_tasks WHERE task_id=?',(saved['task_id'],)).fetchone()[0]
    assert before=='running' and states==['uncertain'] and assignments==1 and not calls
    assert task_state=='development_queued'
    value={'processes':2,'lease_clock_simulated':True,'before':before,'after':states,'assignment_count':assignments,
        'hierarchy_state':task_state,'new_handler_calls':len(calls),'automatic_replay':False,'models_started':0,
        'task_accepted':False,'infrastructure_reboot_verified':False,'tick_states':[a['state'] for a in result['actions']]}
    print(json.dumps(value))


class OperationsRestartTests(unittest.TestCase):
    def test_running_intent_survives_actual_process_loss_without_duplicate_assignment(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()));root.chmod(0o700)
        project=Path(__file__).resolve().parents[2]
        env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(project),'LANG':'C.UTF-8'}
        command=[sys.executable,'-B',str(Path(__file__).resolve()),'--fixture-child']
        first=subprocess.run(command+['prepare',str(root)],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(first.returncode,0,first.stderr)
        second=subprocess.run(command+['inspect',str(root)],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(second.returncode,0,second.stderr)
        value=json.loads(second.stdout)
        self.assertEqual(value['before'],'running');self.assertEqual(value['after'],['uncertain'])
        self.assertEqual(value['assignment_count'],1);self.assertEqual(value['new_handler_calls'],0)
        self.assertFalse(value['task_accepted']);self.assertFalse(value['infrastructure_reboot_verified'])


if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--fixture-child':
        phase=sys.argv[2]
        if phase not in ('prepare','inspect'):raise SystemExit(2)
        child(phase,Path(sys.argv[3]))
    else:unittest.main()
