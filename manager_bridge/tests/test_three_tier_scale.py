"""Bounded read tests; synthetic stored history is not execution evidence."""
import json
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.store import Conflict,PermissionDenied,NotFound,digest,token_hash
from manager_bridge.three_tier_dashboard import build_summary,render_dashboard


class ThreeTierScaleTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    task=fixtures.ThreeTierTests.task
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def history(self,amount,project='project',state='accepted'):
        tasks=[]
        # Stored synthetic history isolates pagination from execution policy tests.
        for _ in range(amount):
            task=self.task(project=project)
            self.core.start(self.manager,task['id'],'dev','history-'+str(self.n))
            candidate={'digest':digest({'task':task['id']}),'source':{'base_sha':'a'*40,'head_sha':'b'*40,'artifact_digest':'c'*64}}
            evidence={'checks':[{'name':'fixture-check','exit_code':0}], 'code_assessment':None}
            with self.store._transaction() as c:
                c.execute('UPDATE hierarchy_tasks SET state=?,candidate=?,evidence=? WHERE task_id=?',(state,json.dumps(candidate),json.dumps(evidence),task['id']))
            tasks.append(task['id'])
        return tasks

    def collected(self,raw):
        return [task for project in raw['projects'] for task in project['tasks']]

    def stub_freshness(self):
        self.calls=[]
        self.core._read_freshness=lambda token,task_id:(self.calls.append(task_id) or None)

    def test_default_page_is_global_and_expensive_checks_are_hard_bounded(self):
        self.history(35);self.history(35,'other');self.stub_freshness()
        raw=self.core.overview(self.manager,['project','other'])
        self.assertEqual(raw['total_tasks'],70);self.assertEqual(raw['displayed_tasks'],50)
        self.assertEqual(len(self.collected(raw)),50);self.assertEqual(len(self.calls),3)
        self.assertTrue(raw['truncated']);self.assertIsNotNone(raw['next_cursor'])
        self.assertEqual(raw['state_counts'],{'accepted':70})
        self.assertEqual([p['total_tasks'] for p in raw['projects']],[35,35])

    def test_unchecked_acceptance_is_history_and_not_current_permission(self):
        self.history(4);self.stub_freshness()
        raw=self.core.overview(self.manager,['project'],limit=4,freshness_limit=1)
        rows=self.collected(raw)
        self.assertEqual(sum(t['source_freshness']=='checked' for t in rows),1)
        unchecked=[t for t in rows if t['source_freshness']=='not_checked']
        self.assertEqual(len(unchecked),3)
        self.assertTrue(all(t['acceptance_recorded'] and not t['technical_accepted'] and t['review_state']=='not_measured' for t in unchecked))
        summary=build_summary(self.core,self.manager,['project'],freshness_limit=0)
        self.assertEqual(summary['total_tasks'],4);self.assertEqual(summary['displayed_tasks'],4)
        self.assertTrue(all(not t['technical_accepted'] for t in summary['projects'][0]['tasks']))
        self.assertIn('현재 소스 최신성 미점검',render_dashboard(summary))

    def test_cursor_excludes_new_tasks_and_does_not_duplicate_existing_page(self):
        original=self.history(7);self.stub_freshness()
        first=self.core.overview(self.manager,['project'],limit=3,freshness_limit=0)
        self.history(2)
        second=self.core.overview(self.manager,['project'],limit=3,cursor=first['next_cursor'],freshness_limit=0)
        third=self.core.overview(self.manager,['project'],limit=3,cursor=second['next_cursor'],freshness_limit=0)
        ids=[t['task_id'] for raw in (first,second,third) for t in self.collected(raw)]
        self.assertEqual(ids,list(reversed(original)));self.assertEqual(len(set(ids)),7)
        self.assertEqual(second['total_tasks'],7);self.assertIsNone(third['next_cursor'])
        self.assertEqual(self.calls,[])

    def test_cursor_cannot_cross_actor_scope_configuration_or_expiry(self):
        self.history(3);self.stub_freshness()
        cursor=self.core.overview(self.manager,['project'],limit=1)['next_cursor']
        other=self.store.add_token('another-manager','manager',['project','other'])
        with self.assertRaises(Conflict):self.core.overview(other,['project'],cursor=cursor)
        with self.assertRaises(Conflict):self.core.overview(self.manager,['project','other'],cursor=cursor)
        self.now+=301
        with self.assertRaises(Conflict):self.core.overview(self.manager,['project'],cursor=cursor)

    def test_config_change_invalidates_cursor_before_source_callback(self):
        self.history(3);self.stub_freshness()
        cursor=self.core.overview(self.manager,['project'],limit=1)['next_cursor'];self.calls.clear()
        self.registry.register(self.owner,'project',{**self.config,'name':'Changed'},expected_revision=1)
        with self.assertRaises(Conflict):self.core.overview(self.manager,['project'],cursor=cursor)
        self.assertEqual(self.calls,[])

    def test_access_and_whole_set_configuration_rechecked_after_observation(self):
        self.history(1);self.history(1,'other')
        def revoke(token,task_id):
            self.store.revoke_token(self.owner,token_hash(self.manager));return None
        self.core._read_freshness=revoke
        with self.assertRaises(PermissionDenied):self.core.overview(self.manager,['project','other'])

    def test_changed_candidate_during_read_is_not_promoted(self):
        task_id=self.history(1)[0]
        def change(token,task_id):
            with self.store._transaction() as c:
                candidate=json.loads(c.execute('SELECT candidate FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone()[0]);candidate['digest']='f'*64
                c.execute('UPDATE hierarchy_tasks SET candidate=? WHERE task_id=?',(json.dumps(candidate),task_id))
            return None
        self.core._read_freshness=change
        row=self.collected(self.core.overview(self.manager,['project']))[0]
        self.assertFalse(row['technical_accepted']);self.assertEqual(row['blocked_reason'],'source_changed')

    def test_invalid_query_never_invokes_source(self):
        self.stub_freshness()
        for options in ({'limit':0},{'limit':201},{'limit':True},{'freshness_limit':21},{'freshness_limit':True},{'cursor':'../private'}):
            with self.subTest(options=options),self.assertRaises(ValueError):self.core.overview(self.manager,['project'],**options)
        self.assertFalse(self.calls)

    def test_counts_do_not_become_displayed_totals_or_page_only_backlog(self):
        self.history(4,state='candidate_ready');self.stub_freshness()
        summary=build_summary(self.core,self.manager,['project'],limit=1,freshness_limit=0)
        project=summary['projects'][0]
        self.assertEqual(project['total_tasks'],4);self.assertEqual(project['displayed_tasks'],1)
        self.assertTrue(project['tasks_truncated']);self.assertTrue(project['capacity']['review_backlog_observed'])
        self.assertEqual(project['capacity']['review_pending_recorded'],4)
        self.assertEqual(sum(summary['counts_displayed'].values()),1)
        self.assertIn('기록된 전체 4건 중 현재 1건 표시',render_dashboard(summary))

    def test_counts_bind_full_project_page_even_with_opaque_cursor(self):
        self.history(5);self.stub_freshness()
        summary=build_summary(self.core,self.manager,['project'],limit=2,freshness_limit=0)
        second=build_summary(self.core,self.manager,['project'],limit=2,cursor=summary['next_cursor'],freshness_limit=0)
        self.assertEqual(second['total_tasks'],5);self.assertEqual(second['displayed_tasks'],2)
        self.assertEqual(second['projects'][0]['state_counts_recorded'],{'accepted':5})
        self.assertEqual(second['freshness_checks'],0)
        self.assertTrue(second['tasks_truncated'])

    def test_whole_project_config_change_during_page_freshness_aborts(self):
        self.history(1);self.history(1,'other');changed=[False]
        def change(token,task_id):
            if not changed[0]:
                changed[0]=True
                self.registry.register(self.owner,'other',{**self.config,'source_ref':'fixture/other','name':'Changed'},expected_revision=1)
            return None
        self.core._read_freshness=change
        with self.assertRaises(Conflict):self.core.overview(self.manager,['project','other'])
