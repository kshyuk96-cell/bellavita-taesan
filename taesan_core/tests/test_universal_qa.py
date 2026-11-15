import unittest
from taesan_core.core import CoreError
from taesan_core.qa_fixtures import build_suite, adapters, REQUIRED_COVERAGE
from taesan_core.universal_qa import Adapter, Scenario, Risk, run_scenarios, fingerprint, build_contract
from manager_bridge.qa_admission import admit_qa_report

SHA = 'a'*40


class UniversalQATests(unittest.TestCase):
    def run_suite(self, suite, registrations):
        return run_scenarios(suite, registrations, source_revision=SHA, seed=42,
                             required_coverage=REQUIRED_COVERAGE)

    def test_reproducible_semantically_unique_full_suite(self):
        first, second = build_suite(), build_suite()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3006)
        self.assertEqual(len({fingerprint([s.adapter,s.inputs]) for s in first}),3006)
        result = self.run_suite(first, adapters())
        self.assertEqual(result['counts']['pass'],3006)
        self.assertEqual(result['counts']['asserted'],3006)
        self.assertTrue(admit_qa_report(result,expected_source_revision=SHA,required_coverage=REQUIRED_COVERAGE,expected_contract=build_contract(first,adapters()))['admitted'])

    def test_independent_oracles_kill_specific_defects(self):
        suite = build_suite()
        for mutant, affected in [('allow_zero_quantity','order'),('skip_tenant','scope'),
                                  ('replay_unknown','control'),('truncate_won','formula')]:
            with self.subTest(mutant=mutant):
                report = self.run_suite(suite, adapters(mutant))
                failures = [r for r in report['scenarios'] if r['status']=='fail']
                self.assertTrue(failures)
                self.assertEqual({r['adapter'] for r in failures},{affected})
                self.assertFalse(admit_qa_report(report,expected_source_revision=SHA,required_coverage=REQUIRED_COVERAGE,expected_contract=build_contract(suite,adapters(mutant)))['admitted'])

    def test_missing_and_crashing_adapters_are_not_passes(self):
        suite = build_suite(per_group=20)
        def crash(data):
            raise RuntimeError('synthetic outage')
        report = self.run_suite(suite,[Adapter('order','v1',crash)])
        self.assertEqual(report['counts']['blocked'],20)
        self.assertEqual(report['counts']['unmeasured'],46)
        self.assertEqual(report['counts']['asserted'],0)
        self.assertFalse(admit_qa_report(report,expected_source_revision=SHA,required_coverage=REQUIRED_COVERAGE,expected_contract=build_contract(suite,adapters()))['admitted'])

    def test_false_evidence_cannot_assert(self):
        suite = build_suite(per_group=4)
        report = self.run_suite(suite,[Adapter('order','v1',lambda _: {'actual':'accepted','evidence':{}})])
        self.assertEqual(report['counts']['asserted'],0)
        self.assertEqual(report['counts']['blocked'],4)

    def test_trusted_contract_is_detached_from_report_and_adapter_mutation(self):
        suite=build_suite(per_group=4)
        contract=build_contract(suite,adapters())
        before=fingerprint(contract)
        report=self.run_suite(suite,adapters())
        report['scenarios'][0]['risk']['priority']='P3'
        report['scenarios'][0]['inputs'].clear()
        self.assertEqual(fingerprint(contract),before)
        # A trusted adapter may still be buggy: its input writes cannot rewrite
        # a scenario's separately retained expected state.
        example=Scenario('mutation','mutation','group','coverage',Risk('P1','impact','likely','manual'),
                         {'value':1},{'value':1},'spec','initial value')
        def mutate(data):
            data['value']=2
            return {'actual':data,'evidence':{'fixture':True}}
        report=self.run_suite([example],[Adapter('mutation','v1',mutate)])
        self.assertEqual(report['scenarios'][0]['status'],'fail')
        self.assertEqual(example.inputs,{'value':1})
        original=Scenario('closure','closure','group','coverage',Risk('P1','impact','likely','manual'),
                          {},{'value':1},'spec','initial value')
        manifest=build_contract([original],[Adapter('closure','v1',lambda _: None)])
        def closure(_):
            original.expected['value']=9
            return {'actual':{'value':9},'evidence':{'fixture':True}}
        report=self.run_suite([original],[Adapter('closure','v1',closure)])
        self.assertEqual(report['scenarios'][0]['expected'],{'value':1})
        self.assertEqual(report['scenarios'][0]['status'],'fail')
        self.assertEqual(manifest['scenarios']['closure']['expected'],{'value':1})

    def test_duplicate_and_invalid_contracts_rejected(self):
        suite = build_suite(per_group=4)
        with self.assertRaises(CoreError):
            self.run_suite([suite[0],suite[0]],adapters())
        with self.assertRaises(CoreError):
            Risk('P0','','likely','irreversible')
        with self.assertRaises(CoreError):
            Scenario('case','adapter','group','coverage',Risk('P1','impact','likely','manual'),{},0,'spec','state',{})


if __name__ == '__main__':
    unittest.main()
