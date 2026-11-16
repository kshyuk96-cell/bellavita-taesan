import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from manager_bridge.hope_batch03 import EXPECTED,contract,evaluate,customer_report
from manager_bridge.hope_registered_runtime import HopeProfile,HopeRegistry,HopeArtifacts
from manager_bridge.hope_service_runtime import ActualServiceProfile
from manager_bridge.store import Store,Conflict,NotFound
from taesan_core.hope_tour_qa import GLOSSARY
SOURCE='a'*40


def evidence():
    ids=(41,42,43,55,56,57,58,59,60)
    raw=dict(schema='hope-actual-service-batch03/v1',scope='actual_service_source_local_sqlite_simulator',source_revision=SOURCE,realPG=False,operational_authorization=False,customer_acceptance_approved=False,
        items=[dict(id=n,title='test',actual=deepcopy(EXPECTED[n])) for n in ids],artifacts={})
    body='<section>'+''.join('<details class="review-term"><summary>'+v+'('+k+')</summary><p>설명</p></details>' for k,v in GLOSSARY.items())+'</section>'
    raw['artifacts']['terms_pages']=[dict(path=p,html=body) for p in ['/review/','/review/features','/review/app','/review/policy']]
    caps={'hope':[150000,100000,500000],'happy':[200000,150000,700000],'premium':[250000,180000,1000000]}
    raw['artifacts']['bpMatrix']=[dict(grade=g,category=c,total=1000000,DP=0,BP=amount,SP=0,card=1000000-amount) for g,amounts in caps.items() for c,amount in zip(['domestic','overseas','culture'],amounts)]
    baseline=dict(source_revision='b'*40,artifacts=dict(terms_pages=[dict(path=p['path'],html='<body>DP only</body>') for p in raw['artifacts']['terms_pages']]))
    host={n:deepcopy(EXPECTED[n]) for n in range(48,55)}
    return raw,baseline,host


class Batch03Tests(unittest.TestCase):
    def test_browser_cannot_be_promoted(self):
        raw,old,host=evidence();host[45]=host[46]=True
        result=evaluate(raw,SOURCE,contract(SOURCE),host,old)
        self.assertEqual(result['counts']['unmeasured'],2);self.assertFalse(result['admission']['admitted'])

    def test_independent_bp_oracle_rejects_false_success(self):
        raw,old,host=evidence();raw['artifacts']['bpMatrix'][0]['BP']+=1
        result=evaluate(raw,SOURCE,contract(SOURCE),host,old)
        self.assertEqual(result['counts']['fail'],1)

    def test_required_bp_combination_cannot_be_duplicated(self):
        raw,old,host=evidence();raw['artifacts']['bpMatrix'][-1]=raw['artifacts']['bpMatrix'][0]
        self.assertEqual(evaluate(raw,SOURCE,contract(SOURCE),host,old)['counts']['fail'],1)

    def test_worker_cannot_change_source_case_set_or_contract(self):
        raw,old,host=evidence()
        for attack in ('source','missing','duplicate','contract'):
            value=deepcopy(raw);trusted=contract(SOURCE)
            if attack=='source':value['source_revision']='c'*40
            elif attack=='missing':value['items'].pop()
            elif attack=='duplicate':value['items'][-1]=value['items'][0]
            else:trusted['scenarios']['batch03-59']['expected']={}
            with self.subTest(attack=attack),self.assertRaises(Conflict):evaluate(value,SOURCE,trusted,host,old)

    def test_original_and_candidate_not_interchangeable(self):
        raw,old,host=evidence();old['source_revision']=SOURCE
        with self.assertRaises(Conflict):evaluate(raw,SOURCE,contract(SOURCE),host,old)

    def test_customer_format_never_calls_source_tests_full_pass(self):
        raw,old,host=evidence();result=evaluate(raw,SOURCE,contract(SOURCE),host,old)
        report=customer_report(raw,result,'c'*40,dict(defects=[]))
        self.assertEqual(len(report['scenarios']),38);self.assertEqual(report['summary']['counts']['PASS'],0)
        self.assertIsNone(report['summary']['deployed_revision']);self.assertFalse(report['summary']['customer_acceptance_approved'])


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory());self.root=Path(self.tmp)
        self.source=self.root/'source';self.source.mkdir();(self.source/'README').write_text('test source, never executed')
        for args in (['init','-q'],['add','.'],['-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture']):subprocess.run(['git',*args],cwd=self.source,check=True,stdout=subprocess.DEVNULL)
        self.sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.source,text=True).strip()
        self.adapters=self.root/'adapter';self.adapters.mkdir()
        for name in ('run-batch03.mjs','d1-local.mjs','fixture-bridge.mjs'):(self.adapters/name).write_text('// not executed')
        self.profile=HopeProfile('hope','v1',ActualServiceProfile(self.source,self.sha,'node@sha256:'+'b'*64),self.adapters)
        self.store=Store(self.root/'store.sqlite');self.owner=self.store.add_token('owner','owner',['*']);self.store.register_project(self.owner,'hope','fixture/hope')
        self.worker=self.store.add_token('worker','worker',['hope']);self.registry=HopeRegistry(self.store,[self.profile])
        self.manifest=dict(project='hope',adapter_release='v1',source_revision=self.sha,execution_profile_digest=self.profile.fingerprint,max_seconds=90)

    def test_owner_registration_revocation_and_no_worker_provision(self):
        with self.assertRaises(Conflict):self.registry.resolve(self.manifest)
        with self.assertRaises(PermissionError):self.registry.provision(self.worker,self.profile)
        self.registry.provision(self.owner,self.profile);self.assertIs(self.registry.resolve(self.manifest),self.profile)
        self.registry.provision(self.owner,self.profile,enabled=False)
        with self.assertRaises(Conflict):self.registry.resolve(self.manifest)

    def test_runtime_mutation_fences_existing_approval_and_release(self):
        self.registry.provision(self.owner,self.profile);(self.adapters/'d1-local.mjs').write_text('// changed')
        with self.assertRaises(Conflict):self.registry.resolve(self.manifest)
        with self.assertRaises(Conflict):self.registry.provision(self.owner,self.profile)

    def test_source_dirty_fences_execution(self):
        self.registry.provision(self.owner,self.profile);(self.source/'README').write_text('changed')
        with self.assertRaises(Conflict):self.registry.resolve(self.manifest)

    def test_artifact_path_cannot_be_selected_without_project_access(self):
        from manager_bridge.qa_jobs import QAJobs
        raw,old,host=evidence();trusted=contract(SOURCE);jobs=QAJobs(self.store)
        approval=jobs.approve(self.owner,project='hope',source_revision=SOURCE,contract=trusted,coverage=['batch03-41'],adapter_release='v1')
        job=jobs.receive(self.owner,approval,'req','task')
        repo=HopeArtifacts(self.store)
        with self.assertRaises(Conflict):repo.collect(self.owner,job['id'],self.adapters)
        self.store.register_project(self.owner,'outside','fixture/outside');outside=self.store.add_token('outside','worker',['outside'])
        with self.assertRaises(NotFound):repo.list(outside,job['id'])

if __name__=='__main__':unittest.main()
