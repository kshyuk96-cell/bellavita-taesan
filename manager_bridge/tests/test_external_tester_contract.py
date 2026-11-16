"""External claim parsing only. No tester implementation or execution."""
from dataclasses import asdict,replace
import copy
import json
import unittest
from manager_bridge.external_tester_contract import (
    ExternalTesterBinding,parse_external_tester_result,SCHEMA_VERSION,
    CLASSIFICATION,MAX_BYTES,MAX_CASES,MAX_FORMULAS,
)


class ExternalTesterContractTests(unittest.TestCase):
    def setUp(self):
        self.binding=ExternalTesterBinding('project','a'*32,7,'b'*40,'c'*64,'d'*64,'v2','e'*64,'f'*64,'1'*64)
        self.value=dict(schema_version=SCHEMA_VERSION,binding=asdict(self.binding),
            tester_digest='2'*64,run_digest='3'*64,report_digest='4'*64,
            counts=dict(total=2,**{'pass':1,'fail':0,'blocked':1,'unmeasured':0}),
            cases=[dict(case_digest='5'*64,status='pass',evidence_digest='6'*64),
                   dict(case_digest='7'*64,status='blocked',evidence_digest='8'*64)],
            formula_counts=dict(total=1,**{'pass':0,'fail':0,'blocked':0,'unmeasured':1}),
            formulas=[dict(formula_digest='9'*64,status='unmeasured',evidence_digest='a'*64)])

    def parse(self,value=None,**options):
        return parse_external_tester_result(json.dumps(self.value if value is None else value),expected_binding=self.binding,**options)

    def test_valid_reference_is_always_external_unverified_with_no_acceptance_authority(self):
        receipt=self.parse();reference=receipt.reference_evidence()
        self.assertEqual(CLASSIFICATION,receipt.classification)
        self.assertFalse(receipt.technical_accepted);self.assertFalse(receipt.admission_authority)
        for key in ('technical_accepted','admission_authority','operational_authorization'):self.assertIs(False,reference[key])
        self.assertEqual(self.value['binding'],reference['binding'])
        self.assertEqual(self.value['counts'],reference['claimed_counts'])
        self.assertEqual(self.value['formula_counts'],reference['claimed_formula_counts'])
        self.assertNotIn('admitted',reference)
        modified=receipt.payload;modified['cases'][0]['status']='fail'
        self.assertEqual('pass',receipt.payload['cases'][0]['status'])

    def test_all_pass_claim_still_cannot_mark_technical_accepted(self):
        value=copy.deepcopy(self.value);value['cases'][1]['status']='pass'
        value['counts'].update({'pass':2,'blocked':0})
        value['formulas'][0]['status']='pass';value['formula_counts'].update({'pass':1,'unmeasured':0})
        receipt=self.parse(value)
        self.assertFalse(receipt.technical_accepted);self.assertEqual(CLASSIFICATION,receipt.reference_evidence()['classification'])

    def test_every_task_source_manifest_policy_binding_is_exact(self):
        for key,current in asdict(self.binding).items():
            value=copy.deepcopy(self.value)
            value['binding'][key]=(current+1 if type(current) is int else ('other' if key in ('project','policy_version') else '0'*len(current)))
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'binding mismatch'):self.parse(value)
        value=copy.deepcopy(self.value);value['binding']['task_version']=True
        with self.assertRaises(ValueError):self.parse(value)
        with self.assertRaises(ValueError):parse_external_tester_result(json.dumps(self.value),expected_binding=self.value['binding'])

    def test_unknown_status_fields_raw_output_and_authority_flags_rejected(self):
        mutations=(lambda v:v.update(technical_accepted=True),lambda v:v.update(stdout='raw output forbidden'),
                   lambda v:v['cases'][0].update(actual=1),lambda v:v['cases'][0].update(status='accepted'),
                   lambda v:v['cases'][0].update(status='skipped'),lambda v:v['formulas'][0].update(status='approved'),
                   lambda v:v.update(schema_version='external-tester-result/v2'))
        for mutation in mutations:
            value=copy.deepcopy(self.value);mutation(value)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.parse(value)

    def test_counts_duplicates_and_excessive_reference_lists_rejected(self):
        mutations=(lambda v:v['counts'].update(total=3),lambda v:v['counts'].update({'pass':2}),
                   lambda v:v['formula_counts'].update({'unmeasured':-1}),
                   lambda v:v['cases'].append(copy.deepcopy(v['cases'][0])),
                   lambda v:v['formulas'].append(copy.deepcopy(v['formulas'][0])),
                   lambda v:v.update(cases=[v['cases'][0]]*(MAX_CASES+1)),
                   lambda v:v.update(formulas=[v['formulas'][0]]*(MAX_FORMULAS+1)))
        for mutation in mutations:
            value=copy.deepcopy(self.value);mutation(value)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.parse(value)

    def test_malformed_json_duplicate_keys_nonfinite_large_or_nested_input_rejected(self):
        raw=json.dumps(self.value)
        duplicate=raw.replace('"schema_version":','"schema_version":"external-tester-result/v1","schema_version":',1)
        nested=copy.deepcopy(self.value);nested['extra']=[[[[[[[[[0]]]]]]]]]
        for payload in (b'\xff',b'{',duplicate,' '* (MAX_BYTES+1),json.dumps(nested),raw.replace('"total": 2','"total": NaN',1)):
            with self.subTest(payload_type=type(payload)),self.assertRaises(ValueError):
                parse_external_tester_result(payload,expected_binding=self.binding)
        with self.assertRaises(ValueError):parse_external_tester_result(self.value,expected_binding=self.binding)

    def test_secret_shaped_and_known_credentials_rejected_without_echo(self):
        secret='sk-proj-do-not-persist-this-secret'
        value=copy.deepcopy(self.value);value['binding']['policy_version']=secret
        with self.assertRaises(ValueError) as caught:self.parse(value)
        self.assertNotIn(secret,str(caught.exception))
        known='2'*64
        with self.assertRaises(ValueError) as caught:self.parse(known_secrets=[known])
        self.assertNotIn(known,str(caught.exception))

    def test_zero_counts_have_no_observations_and_remain_unverified(self):
        value=copy.deepcopy(self.value)
        value['cases']=[];value['formulas']=[]
        value['counts']={key:0 for key in value['counts']};value['formula_counts']=dict(value['counts'])
        self.assertFalse(self.parse(value).technical_accepted)

    def test_canonical_receipt_identity_and_transport_digest_are_distinct(self):
        compact=parse_external_tester_result(json.dumps(self.value,separators=(',',':')).encode(),expected_binding=self.binding)
        spaced=self.parse()
        self.assertEqual(compact.receipt_digest,spaced.receipt_digest)
        self.assertNotEqual(compact.transport_digest,spaced.transport_digest)

    def test_binding_primitive_types_and_digest_shapes_are_validated(self):
        for changes in ({'task_version':0},{'task_version':True},{'source_revision':'branch-name'},
                        {'candidate_digest':'f'*63},{'policy_version':'Bearer secret-value'},
                        {'project':'../project'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):replace(self.binding,**changes)
