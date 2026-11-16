"""Synthetic protocol tests, not evidence of executing actual customer source.

Production harness execution must use the real namespace engine. Fixture outputs
below test admission/binding boundaries only; no customer modules are imported.
"""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from manager_bridge import customer_business_runner as runner
from manager_bridge.customer_qa_plan import CATALOG,inspect_customer_qa_plan
from manager_bridge.store import Conflict,digest
from manager_bridge.three_tier_runner import ReviewUnavailable


class FixtureObservationEngine:
    def __init__(self,body):self.body=body;self.calls=[]
    def probe(self):return {'available':True,'isolation_verified':True}
    def execute_snapshot(self,profile,work,outputs,checks,heartbeat):
        self.calls.append(checks);heartbeat()
        (outputs/'customer-business.json').write_text(json.dumps(self.body,allow_nan=False))
        return [{'name':name,'argv_digest':digest(list(argv)),'exit_code':0,'elapsed_seconds':0.001} for name,argv in checks]


class CustomerBusinessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source=self.root/'source';self.source.mkdir();self.runtime=self.root/'runtime';self.runtime.mkdir()
        for path in {case['file'] for case in CATALOG['bellavita-shop']}:
            target=self.source/path;target.parent.mkdir(exist_ok=True,parents=True)
            target.write_text('\n'.join(case['marker'] for case in CATALOG['bellavita-shop'] if case['file']==path))
        self.git('init');self.git('add','.');self.git('commit','-m','Synthetic protocol fixture only')
        self.sha=self.git('rev-parse','HEAD')
        self.plan=inspect_customer_qa_plan('bellavita-shop',self.source,expected_source_sha=self.sha,config_fingerprint='a'*64)
        runner.install_customer_harnesses(self.runtime)
        self.body=dict(schema_version='customer-business-actuals/v1',project='bellavita-shop',cases={
            key:dict(status='observed',actual=copy.deepcopy(row['expected']),evidence={'entrypoint':'synthetic-protocol-fixture'})
            for key,row in self.plan.expected_contract['scenarios'].items()})

    def git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
                    '-c','user.name=Fixture','-c','user.email=fixture@invalid','-C',str(self.source),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def execute(self,body=None):
        engine=FixtureObservationEngine(body if body is not None else self.body)
        target=runner.CustomerBusinessRunner(self.plan,runtime=self.runtime,repository='fixture/shop',engine=engine,clock=lambda:1000)
        return target.run('fixture-independent-reviewer',heartbeat=lambda:None)

    def test_synthetic_protocol_observations_are_compared_to_frozen_manager_oracles(self):
        result=self.execute();data=result.payload
        self.assertTrue(result.business_admitted)
        self.assertEqual(self.sha,data['source_revision']);self.assertEqual(self.plan.plan_digest,data['plan_digest'])
        self.assertEqual('fixture-independent-reviewer',data['reviewer_subject'])
        self.assertEqual('synthetic_only',data['scope']);self.assertFalse(data['customer_ui_verified'])
        self.assertFalse(data['operational_authorization'])
        self.assertEqual(len(self.body['cases']),data['actual_case_count'])
        for row in result.report['scenarios']:
            self.assertEqual('pass',row['status']);self.assertEqual(data['runtime_digest'],row['evidence']['runtime_digest'])
            self.assertEqual(data['candidate_digest'],row['evidence']['candidate_digest'])

    def test_harness_exit_success_cannot_fill_missing_case_or_formula(self):
        self.body['cases']={}
        result=self.execute();self.assertFalse(result.business_admitted)
        self.assertEqual(0,result.payload['actual_case_count'])
        self.assertTrue(all(row['status']=='blocked' for row in result.report['scenarios']))
        self.assertIn('required_business_formula_not_passed:vat-taxable-split',result.payload['admission']['reasons'])

    def test_actual_mismatch_cannot_be_overridden_by_other_passing_cases(self):
        self.body['cases']['vat-taxable-split']['actual']=[1,93499]
        result=self.execute();self.assertFalse(result.business_admitted)
        row=next(row for row in result.report['scenarios'] if row['id']=='vat-taxable-split')
        self.assertEqual('fail',row['status']);self.assertEqual([1,93499],row['actual'])

    def test_explicit_unavailable_stays_blocked_with_no_assertion(self):
        self.body['cases']['order-stage']={'status':'unavailable','reason':'fixture_not_prepared'}
        result=self.execute();row=next(row for row in result.report['scenarios'] if row['id']=='order-stage')
        self.assertEqual('blocked',row['status']);self.assertFalse(row['asserted'])
        self.assertEqual('fixture_not_prepared',result.payload['unavailable_cases']['order-stage'])

    def test_untrusted_verdict_unknown_case_duplicate_json_and_artifact_links_rejected(self):
        for mutation in (lambda b:b['cases']['server-price'].update(passed=True),
                         lambda b:b['cases'].update(unregistered={'status':'observed','actual':0,'evidence':{'fixture':True}})):
            body=copy.deepcopy(self.body);mutation(body)
            with self.assertRaises(ReviewUnavailable):self.execute(body)
        out=self.root/'artifact';out.write_text('{"project":"x","project":"x"}')
        with self.assertRaises(ReviewUnavailable):runner._parse_observations(out,'x',[])
        out.unlink();out.symlink_to(self.root/'outside');(self.root/'outside').write_text('{}')
        with self.assertRaises(ReviewUnavailable):runner._parse_observations(out,'x',[])

    def test_runtime_harness_changes_are_refused_before_execution(self):
        engine=FixtureObservationEngine(self.body)
        target=runner.CustomerBusinessRunner(self.plan,runtime=self.runtime,repository='fixture/shop',engine=engine)
        (self.runtime/'qa_harnesses'/'customer_shop.py').write_text('unregistered replacement')
        with self.assertRaises(Conflict):target.run('fixture-reviewer',heartbeat=lambda:None)
        self.assertEqual([],engine.calls)
