"""Synthetic workflow/ownership tests; no model, transport or always-on claims."""
from dataclasses import replace
import json
import threading
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.continuous_manager import ContinuousManager,ActionRequest,ActionReceipt
from manager_bridge.store import Conflict,PermissionDenied,NotFound,token_hash


class ContinuousTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    task=fixtures.ThreeTierTests.task
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    start=fixtures.ThreeTierTests.start
    reported=fixtures.ThreeTierTests.reported
    reviewed=fixtures.ThreeTierTests.reviewed
    accepted=fixtures.ThreeTierTests.accepted

    def receipt(self,action,state='observed'):
        return ActionReceipt(action.action_key,action.task_id,action.phase,state,'fixture/action',self.now)

    def scheduler(self,handlers=None):
        return ContinuousManager(self.core,handlers=handlers)

    def tick(self,service,**kwargs):return service.tick(self.manager,['project'],'instance-a',**kwargs)

    def test_real_start_transition_is_confirmed_once_and_receipt_is_not_approval(self):
        task=self.task();calls=[]
        def start(action,heartbeat):
            heartbeat();calls.append(action);self.core.start(self.manager,action.task_id,'dev','start-scheduler')
            return self.receipt(action)
        service=self.scheduler({'start':start})
        result=self.tick(service)
        self.assertEqual(result['actions'][0]['state'],'confirmed');self.assertEqual(len(calls),1)
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'development_queued')
        self.tick(service)
        self.assertEqual(len(calls),1);self.assertFalse(result['always_on_verified'])

    def test_missing_handler_is_not_configured_and_does_not_start(self):
        task=self.task();service=self.scheduler();result=self.tick(service)
        self.assertEqual(result['actions'][0]['state'],'not_configured');self.assertFalse(result['actions'][0]['execution_started'])
        with self.assertRaises(NotFound):self.core.get(self.manager,task['id'])

    def test_observed_receipt_without_core_progress_is_uncertain_not_success(self):
        self.task();calls=[]
        def fake(action,heartbeat):calls.append(action);return self.receipt(action)
        service=self.scheduler({'start':fake});result=self.tick(service)
        self.assertEqual(result['actions'][0]['state'],'uncertain')
        self.tick(service);self.assertEqual(len(calls),1)

    def test_uncertain_command_is_not_replayed_after_process_reopen_or_new_leader(self):
        self.task();calls=[]
        def fail(action,heartbeat):
            calls.append(action)
            with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM continuous_actions WHERE action_key=?',(action.action_key,)).fetchone()[0],'running')
            raise ConnectionError('private credential error never retained')
        first=self.scheduler({'start':fail});self.tick(first)
        self.now+=31;reopened=self.scheduler({'start':fail})
        reopened.tick(self.manager,['project'],'instance-b')
        self.assertEqual(len(calls),1)
        self.assertNotIn('private',json.dumps(reopened.status(self.manager,['project'])))

    def test_same_and_overlapping_project_leaders_cannot_dispatch_simultaneously(self):
        self.task();nested=[]
        service=self.scheduler()
        def start(action,heartbeat):
            nested.append(service.tick(self.manager,['project'],'instance-a'))
            nested.append(service.tick(self.manager,['project','other'],'instance-b'))
            heartbeat();self.core.start(self.manager,action.task_id,'dev','start-a');return self.receipt(action)
        service.handlers['start']=start
        result=self.tick(service)
        self.assertEqual([x['state'] for x in nested],['leader_busy','leader_busy'])
        self.assertEqual(result['actions'][0]['state'],'confirmed')

    def test_fresh_intent_written_before_callback_and_old_epoch_cannot_confirm(self):
        self.task();service=self.scheduler();taken=[]
        def stale(action,heartbeat):
            self.now+=31
            taken.append(service.tick(self.manager,['project'],'instance-b'))
            with self.assertRaises(Conflict):heartbeat()
            return self.receipt(action)
        service.handlers['start']=stale
        result=self.tick(service)
        self.assertEqual(result['actions'][0]['state'],'uncertain')
        self.assertNotEqual(taken[0]['state'],'leader_busy')
        self.assertEqual(next(x for x in service.status(self.manager,['project'])['actions'] if x['phase']=='start')['state'],'uncertain')

    def test_not_ready_is_bounded_safe_retry_with_backoff(self):
        self.task();calls=[]
        def wait(action,heartbeat):calls.append(action);return self.receipt(action,'not_ready')
        service=ContinuousManager(self.core,handlers={'start':wait},max_attempts=2)
        self.tick(service);self.tick(service);self.assertEqual(len(calls),1)
        self.now+=2;self.tick(service);self.assertEqual(len(calls),2)
        self.now+=4;self.tick(service);self.assertEqual(len(calls),2)
        self.assertEqual(service.status(self.manager,['project'])['actions'][0]['reason'],'readiness_attempt_limit')

    def test_cross_company_scope_checked_before_any_callback(self):
        self.task();calls=[];service=self.scheduler({'start':lambda a,h:calls.append(a)})
        limited=self.store.add_token('limited','manager',['project'])
        with self.assertRaises((PermissionDenied,NotFound)):service.tick(limited,['project','other'],'instance')
        self.assertFalse(calls)

    def test_worker_or_owner_cannot_act_as_taesan_scheduler(self):
        service=self.scheduler()
        for token in (self.dev,self.reviewer,self.owner):
            with self.subTest(role=token_hash(token)):
                with self.assertRaises(PermissionDenied):service.tick(token,['project'],'instance')

    def test_revocation_mid_callback_prevents_confirmation_and_other_work(self):
        self.task();self.task('other');calls=[]
        def revoke(action,heartbeat):
            calls.append(action);self.store.revoke_token(self.owner,token_hash(self.manager))
            with self.assertRaises(PermissionDenied):heartbeat()
            return self.receipt(action)
        service=self.scheduler({'start':revoke})
        result=service.tick(self.manager,['project','other'],'instance',limit=2)
        self.assertEqual(len(calls),1);self.assertEqual(result['actions'][0]['state'],'uncertain')

    def test_missing_capacity_or_disabled_project_fails_closed(self):
        self.task();service=self.scheduler({'start':lambda a,h:self.receipt(a)})
        with self.store._transaction() as c:c.execute("DELETE FROM hierarchy_review_limits WHERE scope='*'")
        with self.assertRaises(Conflict):self.tick(service)

    def test_unapproved_tasks_never_enter_plan(self):
        self.task(approved=False);calls=[];service=self.scheduler({'start':lambda a,h:calls.append(a)})
        self.assertEqual(self.tick(service)['actions'],[]);self.assertFalse(calls)

    def test_changed_or_stale_receipt_cannot_confirm(self):
        self.task()
        def wrong(action,heartbeat):return replace(self.receipt(action),action_key='a'*64)
        service=self.scheduler({'start':wrong});result=self.tick(service)
        self.assertEqual(result['actions'][0]['state'],'uncertain')

    def test_explicit_reconcile_uses_registered_observation_then_actual_core_progress(self):
        task=self.task();calls=[]
        def start(action,heartbeat):raise ConnectionError('uncertain')
        def reconcile(action,heartbeat):
            heartbeat();calls.append(action)
            self.core.start(self.manager,action.task_id,'dev','reconciled-start')
            return self.receipt(action)
        service=self.scheduler({'start':start,'reconcile':reconcile});self.tick(service)
        key=service.status(self.manager,['project'])['actions'][0]
        with self.store._transaction() as c:action_key=c.execute("SELECT action_key FROM continuous_actions WHERE phase='start'").fetchone()[0]
        result=service.reconcile(self.manager,['project'],'instance-a',action_key)
        self.assertEqual(result['actions'][0]['state'],'confirmed');self.assertEqual(len(calls),1)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM continuous_actions WHERE action_key=?',(action_key,)).fetchone()[0],'reconciled')
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'development_queued')

    def test_next_task_requires_real_accepted_parent_and_exact_prior_approval(self):
        parent,accepted=self.accepted();following=self.task(dependencies=[parent['id']],base='b'*40);calls=[]
        def next_(action,heartbeat):
            heartbeat();calls.append(action)
            self.core.approve_next(self.manager,action.task_id,action.next_task_id,action.candidate_digest,'scheduler-next')
            return self.receipt(action)
        service=self.scheduler({'approve_next':next_});result=self.tick(service)
        self.assertEqual(len(calls),1);self.assertEqual(result['actions'][0]['phase'],'approve_next')
        self.assertEqual(result['actions'][0]['state'],'confirmed');self.assertEqual(calls[0].next_task_id,following['id'])

    def test_stop_event_prevents_dispatch_and_mid_command_records_uncertain(self):
        self.task();stop=threading.Event();stop.set();calls=[]
        service=self.scheduler({'start':lambda a,h:calls.append(a)})
        self.assertEqual(self.tick(service,stop_event=stop)['state'],'stopped');self.assertFalse(calls)
        stop.clear()
        def interrupted(action,heartbeat):stop.set();heartbeat()
        service.handlers['start']=interrupted
        result=self.tick(service,stop_event=stop)
        self.assertEqual(result['state'],'stopped');self.assertEqual(result['actions'][0]['state'],'uncertain')

    def test_bounded_run_and_status_do_not_claim_always_on(self):
        service=self.scheduler();result=service.run(self.manager,['project'],'run',max_ticks=1)
        self.assertEqual(result['ticks'],1);self.assertFalse(result['always_on_verified'])
        self.assertFalse(service.status(self.manager,['project'])['leader_observed'])
        self.assertEqual(service.status(self.owner,['project'])['actions'],[])

    def test_contract_rejects_unbounded_phases_prose_and_fake_booleans(self):
        with self.assertRaises(ValueError):ActionReceipt('a'*64,'b'*32,'deploy','observed','fixture',1000)
        with self.assertRaises(ValueError):ActionReceipt('a'*64,'b'*32,'start','passed','fixture',1000)
        with self.assertRaises(ValueError):ContinuousManager(self.core,handlers={'deploy':lambda a,h:None})
        with self.assertRaises(ValueError):self.scheduler().tick(self.manager,['project'],'instance',limit=1000)

    def test_later_registered_handler_can_start_previously_unconnected_future_work(self):
        task=self.task();service=self.scheduler();self.tick(service)
        calls=[]
        def start(action,heartbeat):
            calls.append(action);heartbeat();self.core.start(self.manager,action.task_id,'dev','late-registered')
            return self.receipt(action)
        reopened=self.scheduler({'start':start});result=self.tick(reopened)
        self.assertEqual(len(calls),1);self.assertEqual(result['actions'][0]['state'],'confirmed')
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'development_queued')

    def test_bounded_scan_cursor_reaches_later_tasks_without_reading_entire_history(self):
        # Exercise the real keyset cursor with a tiny scan window, avoiding a
        # hundreds-of-fixtures test which would only mirror SQL mechanics.
        tasks=[self.task() for _ in range(3)];service=self.scheduler()
        with self.store._transaction() as c:
            actor,configs=service._auth(c,self.manager,['project'])
            first=service._plans(c,actor,configs,2)
            second=service._plans(c,actor,configs,2)
        self.assertEqual(len(first),2);self.assertEqual(len(second),1)
        self.assertEqual({a.task_id for a in first+second},{t['id'] for t in tasks})

    def test_not_ready_receipt_after_effect_is_uncertain_and_never_replayed(self):
        self.task();calls=[]
        def malformed(action,heartbeat):
            calls.append(action);self.core.start(self.manager,action.task_id,'dev','bad-readiness')
            return self.receipt(action,'not_ready')
        service=self.scheduler({'start':malformed});result=self.tick(service)
        self.assertEqual(result['actions'][0]['state'],'uncertain')
        self.now+=10;self.tick(service);self.assertEqual(len(calls),1)

    def test_expired_orphan_quarantines_other_approved_task_in_same_project(self):
        first=self.task();second=self.task();calls=[];service=self.scheduler()
        def orphan(action,heartbeat):
            calls.append(action.task_id)
            self.now+=31
            result=service.tick(self.manager,['project'],'replacement',limit=20)
            self.assertTrue(result['actions'])
            self.assertTrue(all(x['state']=='project_quarantined' for x in result['actions']))
            with self.assertRaises(Conflict):heartbeat()
            return self.receipt(action)
        service.handlers['start']=orphan
        result=self.tick(service,limit=20)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['actions'][0]['state'],'uncertain')
        self.now+=31
        service.tick(self.manager,['project'],'replacement',limit=20)
        self.assertEqual(len(calls),1)

    def test_explicit_recovery_observes_original_completed_phase_without_replay(self):
        task=self.task();calls=[]
        def start(action,heartbeat):
            heartbeat();calls.append('start')
            self.core.start(self.manager,action.task_id,'dev','completed-before-crash')
            raise ConnectionError('response lost after effect')
        def reconcile(action,heartbeat):
            heartbeat();calls.append('stopped-observer')
            # Trusted fixture stands for host-side stopped/effect observation;
            # it performs no execution or state transition.
            return self.receipt(action)
        service=self.scheduler({'start':start,'reconcile':reconcile})
        self.assertEqual(self.tick(service)['actions'][0]['state'],'uncertain')
        with self.store._transaction() as c:key=c.execute("SELECT action_key FROM continuous_actions WHERE phase='start'").fetchone()[0]
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'development_queued')
        result=service.reconcile(self.manager,['project'],'instance-a',key)
        self.assertEqual(result['actions'][0]['state'],'confirmed')
        self.assertEqual(calls,['start','stopped-observer'])
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM continuous_actions WHERE action_key=?',(key,)).fetchone()[0],'reconciled')
