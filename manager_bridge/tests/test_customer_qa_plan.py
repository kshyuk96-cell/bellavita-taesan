"""Read-only customer-source inspection and synthetic security-boundary tests.

No customer test code is imported or executed by this suite.
"""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge import customer_qa_plan as qa
from manager_bridge.store import Store,Conflict,PermissionDenied
from manager_bridge.maintenance_registry import Registry


class CustomerQAPlanTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source=self.root/'source';self.source.mkdir()
        self.project='bellavita-shop'
        # Text-only synthetic source fixture: the assertion strings are NEVER run.
        paths={row['file'] for row in qa.CATALOG[self.project]}
        for path in paths:
            target=self.source/path;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('\n'.join(row['marker'] for row in qa.CATALOG[self.project] if row['file']==path))
        (self.source/'README.md').write_text('Synthetic inspection fixture only\n')
        (self.source/'unrelated.txt').write_text('pinned\n')
        self.git('init');self.git('add','.');self.git('commit','-m','Synthetic text-only QA source')
        self.sha=self.git('rev-parse','HEAD')
        self.store=Store(self.root/'state.db',clock=lambda:1000)
        self.registry=Registry(self.store)
        self.owner=self.store.add_token('owner','owner',['*'])
        config=dict(company='fixture',name='Synthetic',source_kind='github',source_ref='fixture/shop',branch='main',services=[],maintenance_enabled=False)
        self.fingerprint=self.registry.register(self.owner,self.project,config)['fingerprint']
        self.manager=self.store.add_token('manager','manager',[self.project])

    def git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
                 '-c','user.name=Fixture','-c','user.email=fixture@invalid','-C',str(self.source),*args],
                 env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'},stderr=subprocess.DEVNULL,text=True).strip()

    def plan(self,**changes):
        values=dict(expected_source_sha=self.sha,config_fingerprint=self.fingerprint)
        values.update(changes)
        return qa.inspect_customer_qa_plan(self.project,self.source,**values)

    def test_frozen_oracles_and_formulas_have_no_execution_or_pass_claim(self):
        plan=self.plan();data=plan.payload
        self.assertTrue(data['definition_ready']);self.assertEqual([],data['blockers'])
        self.assertEqual('not_run',data['execution_status']);self.assertEqual([],data['observed_coverage'])
        self.assertFalse(data['business_admitted']);self.assertFalse(data['customer_ui_verified'])
        vat=next(row for row in data['formulas'] if row['id']=='vat-taxable-split')
        self.assertEqual([85000,8500],vat['expected']);self.assertEqual('unmeasured',vat['status'])
        modified=plan.payload;modified['commands'].clear()
        self.assertTrue(plan.review_commands)
        self.assertEqual(self.sha,next(iter(plan.expected_contract['scenarios'].values()))['inputs']['source_revision'])

    def test_owner_can_register_exact_mechanical_commands_without_business_acceptance(self):
        plan=self.plan()
        with self.assertRaises(PermissionDenied):
            qa.register_customer_review_commands(self.manager,self.registry,plan,contract_version='1',provider_identity='fixture')
        registered=qa.register_customer_review_commands(self.owner,self.registry,plan,contract_version='1',provider_identity='fixture')
        self.assertEqual(self.sha,registered.payload['approved_base_sha'])
        self.assertEqual(set(plan.review_commands),set(registered.payload['checks']))
        self.assertFalse(plan.payload['business_admitted'])
        with self.store._transaction() as c:
            self.assertEqual(plan.plan_digest,c.execute('SELECT plan_digest FROM customer_review_command_plans').fetchone()[0])

    def test_missing_business_oracle_is_explicit_but_mechanical_registration_separate(self):
        path=self.source/'cash_receipts.py';path.write_text('Tax rule deliberately unavailable\n')
        self.git('add','.');self.git('commit','-m','Remove synthetic oracle declaration');self.sha=self.git('rev-parse','HEAD')
        plan=self.plan()
        self.assertIn('required_formula_oracle_not_available:vat-taxable-split',plan.blockers)
        self.assertFalse(plan.payload['definition_ready'])
        self.assertTrue(plan.payload['mechanical_registration_ready'])
        qa.register_customer_review_commands(self.owner,self.registry,plan,contract_version='1',provider_identity='fixture')
        self.assertFalse(plan.payload['business_admitted'])

    def test_source_pin_stale_dirty_and_missing_files_fail_closed(self):
        for changes in ({'expected_source_sha':'0'*40},{'source_status':'stale'},{'source_status':'not_available'}):
            plan=self.plan(**changes)
            self.assertFalse(plan.payload['mechanical_registration_ready'])
            with self.assertRaises(Conflict):qa.register_customer_review_commands(self.owner,self.registry,plan,contract_version='1',provider_identity='fixture')
        (self.source/'unrelated.txt').write_text('changed\n')
        self.assertIn('tracked_source_modified',self.plan().blockers)

    def test_source_change_during_inspection_is_caught_by_final_fence(self):
        original=qa._git;changed=[False]
        def read(root,*args):
            output=original(root,*args)
            if args[0]=='show' and not changed[0]:
                (self.source/'unrelated.txt').write_text('concurrent change\n');changed[0]=True
            return output
        with patch.object(qa,'_git',side_effect=read):plan=self.plan()
        self.assertIn('source_changed_during_inspection',plan.blockers)
        self.assertFalse(plan.payload['mechanical_registration_ready'])

    def test_git_fsmonitor_and_filter_configuration_never_execute(self):
        marker=self.root/'executed-marker';script=self.root/'monitor.sh'
        script.write_text('#!/bin/sh\ntouch '+shlex.quote(str(marker))+'\n');script.chmod(0o700)
        self.git('config','core.fsmonitor',str(script))
        self.assertTrue(self.plan().payload['definition_ready']);self.assertFalse(marker.exists())
        self.git('config','filter.probe.clean',str(script))
        plan=self.plan()
        self.assertFalse(plan.payload['mechanical_registration_ready']);self.assertFalse(marker.exists())

    def test_symlink_root_file_ancestor_and_large_file_are_refused(self):
        alias=self.root/'alias';alias.symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(ValueError):qa.inspect_customer_qa_plan(self.project,alias,expected_source_sha=self.sha,config_fingerprint=self.fingerprint)
        leaf=self.source/'README.md';leaf.unlink();leaf.symlink_to(self.root/'outside')
        (self.root/'outside').write_text('outside source')
        self.assertIn('source_file_not_available:README.md',self.plan().blockers)
        leaf.unlink();leaf.write_text('x'*(128*1024+1))
        self.assertIn('source_file_not_available:README.md',self.plan().blockers)

    def test_registration_rechecks_source_after_freeze(self):
        plan=self.plan();(self.source/'unrelated.txt').write_text('after freeze\n')
        with self.assertRaisesRegex(Conflict,'source plan changed'):
            qa.register_customer_review_commands(self.owner,self.registry,plan,contract_version='1',provider_identity='fixture')


class ActualRecoveredSourceInspectionTests(unittest.TestCase):
    SOURCES={
      'bellavita-shop':('/workspace/taesan-governance-portfolio-proof/sources/bellavita-shop','d5047f3345b757edfcfbd81a73c609c0392007dd'),
      'funeral-association':('/workspace/taesan-governance-portfolio-proof/sources/funeral-association','42cfeedee9e0fbc1ffc90603b86d4fb5ed0e9e84'),
      'bellavita-erp':('/workspace/taesan-rollout-source-recovery/bellavita-erp/.sites-checkout','13f78626e415c67ce2d07424f5ba5c1351666581'),
      'hope-sunrise':('/workspace/taesan-rollout-source-recovery/hope-sunrise/.sites-checkout','985a918f55f3cd2fbf0445d6ef38c761332577d2'),
    }
    def test_actual_customer_files_define_four_distinct_unmeasured_contracts(self):
        if not all(Path(root).is_dir() for root,_ in self.SOURCES.values()):self.skipTest('recovered customer sources unavailable')
        digests=set()
        for project,(root,sha) in self.SOURCES.items():
            with self.subTest(project=project):
                plan=qa.inspect_customer_qa_plan(project,root,expected_source_sha=sha,config_fingerprint='f'*64)
                self.assertEqual((),plan.blockers);self.assertTrue(plan.payload['definition_ready'])
                self.assertFalse(plan.payload['business_admitted']);self.assertEqual('not_run',plan.payload['execution_status'])
                self.assertTrue(all(row['status']=='unmeasured' for row in plan.payload['formulas']))
                digests.add(plan.plan_digest)
                if project!='bellavita-shop':
                    self.assertEqual('not_assessed',plan.payload['optional_assessments']['tax-components']['status'])
                    self.assertNotIn('tax-components',plan.payload['required_risk_groups'])
        self.assertEqual(4,len(digests))
