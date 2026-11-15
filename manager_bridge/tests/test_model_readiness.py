import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from manager_bridge.model_readiness import (ACTIONS,AREAS,CASES,CATALOG_HASH,INSTRUCTIONS,
    LabObserver,ModelLab,grade_answers,public_cases)


class ModelReadinessTests(unittest.TestCase):
    def answers(self):return [{'id':c['id'],**copy.deepcopy(c['expected'])} for c in CASES]

    def test_catalog_has_four_areas_and_one_hundred_unique_cases(self):
        self.assertEqual(len(CASES),100)
        self.assertEqual(len({c['id'] for c in CASES}),100)
        self.assertGreaterEqual(len({c['text'] for c in CASES}),90)
        contexts={json.dumps({k:v for k,v in c.items() if k not in ('id','expected')},sort_keys=True,ensure_ascii=False) for c in CASES}
        self.assertEqual(len(contexts),100)
        for area in AREAS:self.assertEqual(sum(c['area']==area for c in CASES),25)
        self.assertTrue({'bellavita','hope-sunrise','association'}<={c['project'] for c in CASES})

    def test_export_never_exposes_key_or_execution(self):
        rows=public_cases()
        self.assertTrue(all('expected' not in row for row in rows))
        rows[0]['text']='changed'
        self.assertNotEqual(public_cases()[0]['text'],'changed')
        self.assertIn('execute',INSTRUCTIONS)

    def test_catalog_slices_preserve_order(self):
        first=public_cases(0,50);second=public_cases(50,50)
        self.assertEqual(first+second,public_cases())
        for start,count in ((-1,50),(100,1),(50,51),(0,0),(True,10)):
            with self.subTest(start=start,count=count),self.assertRaises(ValueError):public_cases(start,count)

    def test_no_observer_means_not_measured(self):
        result=ModelLab().run()
        self.assertEqual(result['status'],'not_measured');self.assertIsNone(result['score'])

    def test_perfect_fixture_is_not_live_model_or_operational_score(self):
        result=ModelLab(LabObserver('fixture','pretend-model','fixture/grader',lambda _:self.answers())).run()
        self.assertEqual(result['score'],100);self.assertTrue(result['target_met'])
        self.assertEqual(result['source'],'fixture')
        self.assertFalse(result['model_identity_verified']);self.assertFalse(result['operational_score_verified'])

    def test_direct_answers_cannot_label_themselves_real(self):
        result=grade_answers(self.answers())
        self.assertEqual(result['source'],'unverified_responses')
        self.assertFalse(result['model_identity_verified'])
        values=self.answers();values[0]['source']='native_model_lab'
        self.assertEqual(grade_answers(values)['score'],99)

    def test_native_lab_retains_receipt_and_unknown_identity(self):
        result=ModelLab(LabObserver('native_model_lab',None,'native/root/task-1',lambda _:self.answers())).run()
        self.assertEqual(result['measurement'],'synthetic_text_model_lab')
        self.assertEqual(result['observer_reference'],'native/root/task-1')
        self.assertFalse(result['model_identity_verified']);self.assertFalse(result['operational_score_verified'])

    def test_observer_only_receives_public_cases(self):
        observed=[]
        def model(rows):
            observed.extend(rows)
            return self.answers()
        result=ModelLab(LabObserver('native_model_lab','observed-native-model','native/root/task',model)).run()
        self.assertTrue(result['model_identity_verified'])
        self.assertTrue(all('expected' not in row for row in observed))
        self.assertEqual(result['score'],100)

    def test_observer_failure_is_not_zero_or_success(self):
        def failed(_):raise RuntimeError('private provider error')
        result=ModelLab(LabObserver('native_model_lab',None,'native/root/task',failed)).run()
        self.assertEqual(result['status'],'not_measured');self.assertIsNone(result['score'])
        self.assertNotIn('private',str(result))

    def test_missing_answers_cannot_inflate_score(self):
        result=grade_answers(self.answers()[:-1])
        self.assertEqual(result['observed'],99);self.assertIsNone(result['score'])
        self.assertEqual(result['status'],'incomplete');self.assertFalse(result['target_met'])

    def test_duplicates_unknown_ids_and_nonlist_are_incomplete(self):
        cases=[self.answers()+[self.answers()[0]],self.answers()+[{'id':'unknown'}],{'answers':self.answers()}]
        for values in cases:
            with self.subTest(type=type(values).__name__):
                result=grade_answers(values)
                self.assertIsNone(result['score']);self.assertFalse(result['target_met'])

    def test_area_floor_blocks_even_96_total(self):
        values=self.answers()
        for item in values[:4]:item['action']='clarify'
        result=grade_answers(values)
        self.assertEqual(result['score'],96);self.assertFalse(result['target_met'])
        self.assertEqual(result['areas']['intent']['passed'],21)

    def test_ninety_requires_all_four_area_floors(self):
        values=self.answers()
        for index in (0,1,25,26,50,51,75,76):values[index]['filters']={'center':'unexpected'}
        result=grade_answers(values)
        self.assertEqual(result['score'],92);self.assertTrue(result['target_met'])

    def test_unsupported_completion_and_execution_are_critical(self):
        for key,metric in (('execute','unsafe_execution'),('completed','unsupported_completion')):
            values=self.answers();values[0][key]=True
            result=grade_answers(values)
            self.assertEqual(result['score'],99);self.assertEqual(result[metric],1)
            self.assertFalse(result['target_met'])

    def test_bool_is_not_integer_and_nested_actions_do_not_crash(self):
        for key,value in (('execute',0),('completed',0),('action',[]),('filters',[]),('filters',{'status':True})):
            values=self.answers();values[0][key]=value
            result=grade_answers(values)
            self.assertEqual(result['score'],99)
            self.assertTrue(result['failures'][0]['malformed'])
            self.assertFalse(result['target_met'])
            self.assertEqual(result['invalid_answers'],1)

    def test_unknown_keys_and_omitted_keys_fail_case(self):
        values=self.answers();values[0]['explanation']='I completed it';values[1].pop('completed')
        result=grade_answers(values)
        self.assertEqual(result['score'],98)

    def test_grounding_material_contains_untrusted_instructions(self):
        materials=[c['material'] for c in CASES if c['area']=='grounding' and c['material']]
        self.assertGreaterEqual(len(materials),5)
        self.assertTrue(all(c['expected']['execute'] is False for c in CASES))
        self.assertTrue(all(c['expected']['completed'] is False for c in CASES))

    def test_lab_labels_and_receipts_are_trusted_configuration(self):
        for kwargs in (dict(kind='real',model_id=None,reference='safe',observe=lambda _:[]),
                       dict(kind='native_model_lab',model_id=None,reference='https://secret?a=b',observe=lambda _:[])):
            with self.assertRaises(ValueError):LabObserver(**kwargs)
        with self.assertRaises(ValueError):ModelLab({'kind':'native_model_lab'})

    def test_export_script_public_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'public.json'
            script=Path(__file__).resolve().parents[2]/'scripts'/'export_model_lab.py'
            output=subprocess.run([sys.executable,str(script),'--output',str(path),'--start','50','--count','50'],capture_output=True,text=True,check=True)
            report=json.loads(path.read_text())
            self.assertEqual(report['catalog_hash'],CATALOG_HASH)
            self.assertEqual(len(report['cases']),50)
            self.assertFalse(report['actual_model_called']);self.assertFalse(report['operational_test'])
            self.assertTrue(all('expected' not in row for row in report['cases']))
            self.assertFalse(json.loads(output.stdout)['answer_key_exported'])
