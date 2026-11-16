"""Owner-provisioned Hope adapter registry, fenced engine and immutable job artifacts."""
from dataclasses import dataclass
from pathlib import Path
import base64
import hashlib
import json
import re
from .hope_service_runtime import ActualServiceProfile,execute_actual_service,_docker_env
from .project_runtime import _directory
from .qa_jobs import QAJobs
from .qa_runtime import QAInterrupted
from .store import Conflict,NotFound,digest,canonical_json
import subprocess


@dataclass(frozen=True)
class HopeProfile:
    project: str
    release: str
    service: ActualServiceProfile
    adapter_root: Path
    timeout: int = 90

    def __post_init__(self):
        if not isinstance(self.service,ActualServiceProfile) or type(self.timeout) is not int or not 1<=self.timeout<=120:
            raise ValueError('bounded actual-service profile required')
        if not re.fullmatch('[a-zA-Z0-9_.-]{1,100}',self.project) or not re.fullmatch('[a-zA-Z0-9_.-]{1,100}',self.release):raise ValueError('registered identifiers required')
        object.__setattr__(self,'adapter_root',_directory(self.adapter_root))
    @property
    def revision(self):return self.service.revision
    def source_check(self):return self.service.source_check()
    def manifest(self):
        files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(self.adapter_root.glob('*.mjs')) if not p.is_symlink()}
        if not {'run-batch03.mjs','d1-local.mjs','fixture-bridge.mjs'}<=files.keys() or any(p.is_symlink() for p in self.adapter_root.glob('*.mjs')):raise Conflict('missing or symlinked adapter')
        return dict(schema='hope-registered-profile/v1',project=self.project,release=self.release,source_revision=self.revision,
            source=str(self.service.source),adapter_root=str(self.adapter_root),adapter_files=files,image=self.service.image,
            timeout=self.timeout,entrypoint='run-batch03.mjs',network='none',cgroup_memory_enforced=False)
    @property
    def fingerprint(self):return digest(self.manifest())


class HopeRegistry:
    """Trusted config supplies paths; HTTP tasks may never supply executable paths."""
    def __init__(self,store,profiles):
        self.store=store;self.profiles={}
        for profile in profiles:
            if not isinstance(profile,HopeProfile) or (profile.project,profile.release) in self.profiles:raise ValueError('unique typed profiles required')
            self.profiles[(profile.project,profile.release)]=profile
        with store._transaction() as c:c.execute('CREATE TABLE IF NOT EXISTS hope_adapter_registry(project TEXT,release TEXT,fingerprint TEXT,enabled INTEGER,PRIMARY KEY(project,release))')
    def provision(self,token,profile,*,enabled=True):
        if self.profiles.get((profile.project,profile.release)) is not profile or type(enabled) is not bool:raise ValueError('host config only')
        profile.source_check();fingerprint=profile.fingerprint
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner',));self.store._project_access(c,actor,profile.project)
            prior=c.execute('SELECT fingerprint FROM hope_adapter_registry WHERE project=? AND release=?',(profile.project,profile.release)).fetchone()
            if prior and prior['fingerprint']!=fingerprint:raise Conflict('release is immutable; register a new version')
            c.execute('INSERT INTO hope_adapter_registry VALUES(?,?,?,?) ON CONFLICT(project,release) DO UPDATE SET enabled=excluded.enabled',(profile.project,profile.release,fingerprint,int(enabled)))
        return dict(project=profile.project,release=profile.release,enabled=enabled,execution_profile_digest=fingerprint)
    def resolve(self,manifest):
        profile=self.profiles.get((manifest.get('project'),manifest.get('adapter_release')))
        if not profile:raise Conflict('adapter not registered on host')
        with self.store._transaction() as c:row=c.execute('SELECT * FROM hope_adapter_registry WHERE project=? AND release=?',(profile.project,profile.release)).fetchone()
        if not row or not row['enabled'] or row['fingerprint']!=profile.fingerprint or manifest.get('execution_profile_digest')!=profile.fingerprint or manifest.get('source_revision')!=profile.revision or manifest.get('max_seconds',0)<profile.timeout:
            raise Conflict('unapproved, revoked or changed profile')
        profile.source_check();return profile


class HopeEngine:
    def __init__(self,registry,manifest,evaluator):
        self.registry,self.manifest,self.evaluator=registry,manifest,evaluator;self.raw=None
    def probe(self):
        try:
            profile=self.registry.resolve(self.manifest)
            code="const r={uid:process.getuid(),network:false,fs:false,spawn:false};try{await fetch('https://example.com',{signal:AbortSignal.timeout(500)})}catch{r.network=true}try{(await import('node:fs')).readFileSync('/etc/passwd')}catch{r.fs=true}try{(await import('node:child_process')).execFileSync('/bin/true')}catch{r.spawn=true}console.log(JSON.stringify(r));"
            result=subprocess.run(['docker','run','--rm','--pull','never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534',profile.service.image,'node','--permission','--input-type=module','-e',code],env=_docker_env(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10)
            verified=result.returncode==0 and json.loads(result.stdout)==dict(uid=65534,network=True,fs=True,spawn=True)
        except Exception:verified=False
        return dict(available=verified,isolation_verified=verified,kind='actual-fixed-confinement-probe',cgroup_memory_enforced=False)
    def execute(self,profile,heartbeat):
        before=profile.fingerprint
        def fenced():
            heartbeat()
            if self.registry.resolve(self.manifest).fingerprint!=before:raise QAInterrupted('profile_changed')
        self.raw=execute_actual_service(profile.service,profile.adapter_root,timeout=profile.timeout,entrypoint='run-batch03.mjs',heartbeat=fenced)
        report=self.evaluator(self.raw)
        return report,dict(schema='qa-execution-receipt/v1',profile_digest=before,source_revision=profile.revision,
            isolation_verified=self.raw.get('artifacts',{}).get('confinement',{}).get('verified') is True,
            network='none',process_exited=True,report_observation_digest=digest(report))


class HopeArtifacts:
    """Host imports reviewed report outputs; project ACL is checked on every read."""
    ALLOWED={'actual-source-evidence.json.gz','common-report.json.gz','trusted-contract.json.gz','customer-report.json','customer-report.docx','customer-report.xlsx','formula-report.json','batch-summary.json','source-candidate.patch','defect-retest.json'}
    def __init__(self,store):
        self.store=store;self.jobs=QAJobs(store)
        with store._transaction() as c:c.execute('CREATE TABLE IF NOT EXISTS hope_job_artifacts(job TEXT,name TEXT,sha TEXT,size INTEGER,data BLOB,PRIMARY KEY(job,name))')
    def collect(self,token,job,root):
        status=self.jobs.inspect(token,job)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner',))
        if status['job']['state'] not in ('accepted','blocked'):raise Conflict('terminal report required')
        root=_directory(root);files=sorted(root.iterdir());total=0;payload=[]
        for path in files:
            if path.name=='SHA256SUMS':continue
            if path.name not in self.ALLOWED or path.is_symlink() or not path.is_file():raise ValueError('fixed report files only')
            size=path.stat().st_size;total+=size
            if size>8*1024*1024 or total>24*1024*1024:raise ValueError('artifact limits')
            data=path.read_bytes();payload.append((job,path.name,hashlib.sha256(data).hexdigest(),len(data),data))
        if not payload:raise ValueError('empty result bundle')
        with self.store._transaction() as c:
            for row in payload:
                prior=c.execute('SELECT sha FROM hope_job_artifacts WHERE job=? AND name=?',row[:2]).fetchone()
                if prior and prior['sha']!=row[2]:raise Conflict('immutable artifact conflict')
                c.execute('INSERT OR IGNORE INTO hope_job_artifacts VALUES(?,?,?,?,?)',row)
        return self.list(token,job)
    def list(self,token,job):
        self.jobs.inspect(token,job)
        with self.store._transaction() as c:return [dict(r) for r in c.execute('SELECT name,sha,size FROM hope_job_artifacts WHERE job=? ORDER BY name',(job,))]
    def get(self,token,job,name):
        self.jobs.inspect(token,job)
        if name not in self.ALLOWED:raise NotFound('artifact missing')
        with self.store._transaction() as c:row=c.execute('SELECT * FROM hope_job_artifacts WHERE job=? AND name=?',(job,name)).fetchone()
        if not row:raise NotFound('artifact missing')
        if hashlib.sha256(row['data']).hexdigest()!=row['sha']:raise Conflict('stored evidence changed')
        return dict(name=name,sha=row['sha'],size=row['size'],encoding='base64',data=base64.b64encode(row['data']).decode())
