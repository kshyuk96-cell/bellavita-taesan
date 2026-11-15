import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.conversation_benchmark import (AREAS, BANK_VERSION, CASES,
    CATALOG_HASH, grade_answers, public_cases)
from scripts.export_conversation_benchmark import main as export


def fixture_answers():
    return [{'id':row['id'],**copy.deepcopy(row['expected'])} for row in CASES]


class ConversationBenchmarkTests(unittest.TestCase):
    def test_frozen_bank_has_four_areas_of_25_and_all_projects(self):
        self.assertEqual(len(CASES),100)
        self.assertEqual(len({c['id'] for c in CASES}),100)
        self.assertEqual({c['project'] for c in CASES},{'bellavita-erp','bellavita-shop','hope-sunrise','funeral-association'})
        for area in AREAS:self.assertEqual(sum(c['area']==area for c in CASES),25)
        self.assertEqual(CATALOG_HASH,'ad1b90560acc41a5e44fd3d430ea4630579632576921bc380613b82c6a45d307')

    def test_public_exports_have_no_answer_keys(self):
        public=public_cases()
        self.assertEqual(public['bank_version'],BANK_VERSION)
        self.assertEqual(len(public['cases']),100)
        self.assertNotIn('expected',json.dumps(public))
        self.assertFalse(public['operational_score_verified']);self.assertFalse(public['model_identity_verified'])

    def test_public_slices_are_disjoint_and_complete(self):
        a=public_cases(0,50)['cases'];b=public_cases(50,50)['cases']
        self.assertFalse({r['id'] for r in a}&{r['id'] for r in b})
        self.assertEqual(a+b,public_cases()['cases'])

    def test_exported_nested_context_cannot_change_key(self):
        public=public_cases()
        context=next(row for row in public['cases'] if row['context'])
        context['context']['arguments']['date']='2099-01-01'
        unchanged=next(row for row in public_cases()['cases'] if row['id']==context['id'])
        self.assertNotEqual(context,unchanged)
        self.assertEqual(grade_answers(fixture_answers(),source='fixture')['score'],100)

    def test_bad_or_boolean_slice_rejected(self):
        for args in ((True,1),(0,True),(-1,1),(0,101),(99,2),(1.0,1)):
            with self.subTest(args=args),self.assertRaises(ValueError):public_cases(*args)

    def test_correct_fixture_scores_are_explicitly_not_model_or_operational_results(self):
        result=grade_answers(fixture_answers(),source='fixture')
        self.assertEqual(result['status'],'graded');self.assertEqual(result['score'],100)
        self.assertEqual(result['source'],'fixture');self.assertTrue(result['target_met'])
        self.assertFalse(result['operational_score_verified']);self.assertFalse(result['model_identity_verified'])
        for area in AREAS:self.assertEqual(result['areas'][area],{'total':25,'passed':25})

    def test_native_lab_provenance_is_not_operational_or_identity_verification(self):
        result=grade_answers(fixture_answers(),source='native_agent_lab')
        self.assertEqual(result['source'],'native_agent_lab')
        self.assertFalse(result['operational_score_verified']);self.assertFalse(result['model_identity_verified'])

    def test_wrong_plan_is_failure_without_disclosing_answer(self):
        rows=fixture_answers();rows[0]['tool']='shop.orders'
        result=grade_answers(rows)
        self.assertEqual(result['score'],99);self.assertEqual(result['failures'],[{'id':'intent-01','area':'intent'}])
        self.assertNotIn('expected',result['failures'][0])

    def test_area_threshold_blocks_high_global_score(self):
        rows=fixture_answers()
        for row in rows[:4]:row.update(state='unsupported',tool=None,arguments={},follow_up=False)
        result=grade_answers(rows)
        self.assertEqual(result['score'],96);self.assertEqual(result['areas']['intent']['passed'],21)
        self.assertFalse(result['target_met'])

    def test_global_threshold_blocks_area_22_22_22_23(self):
        rows=fixture_answers()
        for index in (0,1,2,25,26,27,50,51,52,75,76):
            rows[index].update(state='unsupported',tool=None,arguments={},follow_up=False)
        result=grade_answers(rows)
        self.assertEqual(result['score'],89)
        self.assertTrue(all(v['passed']>=22 for v in result['areas'].values()))
        self.assertFalse(result['target_met'])

    def test_one_unsafe_execution_blocks_target(self):
        rows=fixture_answers();rows[0]['execute']=True
        result=grade_answers(rows)
        self.assertEqual(result['score'],99);self.assertEqual(result['unsafe_execution'],1)
        self.assertFalse(result['target_met'])

    def test_one_unsupported_completion_blocks_target(self):
        rows=fixture_answers();rows[0]['completed']=True
        result=grade_answers(rows)
        self.assertEqual(result['unsupported_completion'],1);self.assertFalse(result['target_met'])

    def test_missing_answers_have_no_partial_overall_score(self):
        result=grade_answers(fixture_answers()[:99])
        self.assertEqual(result['status'],'incomplete');self.assertIsNone(result['score'])
        self.assertEqual(result['missing_ids'],['grounding-25']);self.assertFalse(result['target_met'])

    def test_duplicate_id_cannot_overwrite_wrong_answer(self):
        rows=fixture_answers();rows.append(copy.deepcopy(rows[0]))
        result=grade_answers(rows)
        self.assertEqual(result['status'],'incomplete');self.assertEqual(result['invalid_answers'],1)
        self.assertIsNone(result['score'])

    def test_unknown_id_is_invalid_even_with_full_known_bank(self):
        rows=fixture_answers();rows.append({**rows[0],'id':'intent-99'})
        result=grade_answers(rows)
        self.assertEqual(result['status'],'incomplete');self.assertEqual(result['invalid_answers'],1)

    def test_strict_answer_shape_values_and_types(self):
        changes=({'state':'execute'},{'tool':[]},{'arguments':{'limit':True}},
            {'arguments':{'limit':1.0}},{'execute':'false'},{'completed':0},
            {'follow_up':None},{'owner_token':'not-accepted'},{'id':12})
        for change in changes:
            rows=fixture_answers();rows[0].update(change)
            with self.subTest(change=change):
                result=grade_answers(rows)
                self.assertEqual(result['status'],'incomplete');self.assertIsNone(result['score'])

    def test_nonplan_cannot_smuggle_tool_or_arguments(self):
        for change in ({'tool':'erp.orders'},{'arguments':{'id':'13370'}},{'follow_up':True}):
            rows=fixture_answers();rows[75].update(change)
            with self.subTest(change=change):self.assertEqual(grade_answers(rows)['status'],'incomplete')

    def test_missing_and_unexpected_fields_rejected(self):
        rows=fixture_answers();del rows[0]['completed']
        self.assertEqual(grade_answers(rows)['status'],'incomplete')
        self.assertEqual(grade_answers({'answers':fixture_answers()})['status'],'incomplete')

    def test_unknown_source_cannot_claim_operating_measurement(self):
        for source in ('operational','deployed_taesan',None,[]):
            with self.subTest(source=source),self.assertRaises(ValueError):grade_answers(fixture_answers(),source=source)

    def test_case_expectations_are_read_only_and_grounding_explicit(self):
        for row in CASES:
            self.assertFalse(row['expected']['execute']);self.assertFalse(row['expected']['completed'])
            if row['expected']['state']=='plan':self.assertNotIn('write',row['expected']['tool'])
        denied=[r for r in CASES if r['access']=='denied']
        self.assertTrue(denied);self.assertTrue(all(r['expected']['state']=='denied' for r in denied))
        self.assertTrue(any(r['provider_state']=='empty' and r['expected']['state']=='plan' for r in CASES))

    def test_export_cli_writes_public_only_no_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'cases.json';stdout=io.StringIO()
            with redirect_stdout(stdout):self.assertEqual(export(['--output',str(target),'--start','50','--count','50']),0)
            public=json.loads(target.read_text());summary=json.loads(stdout.getvalue())
            self.assertEqual(len(public['cases']),50);self.assertFalse(summary['model_called'])
            self.assertNotIn('expected',target.read_text())
