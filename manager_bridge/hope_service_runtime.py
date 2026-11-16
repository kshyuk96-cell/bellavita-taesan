"""Pinned actual-service build and API execution in an explicit Docker profile.

No fallback to an unsandboxed host process. Never pushes or deploys the Site.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
from .project_runtime import _archive_source,_directory
from .source_rehearsal import observe_checkout
from .store import Conflict,digest

CORE=['club','club-travel','club-launch','club-integration','club-integration-crypto','club-admin','club-operations','orders']
EXPORTS=['handleReview','rvNewDocument','rvApply','rvView','rvAccounts','rvHash','rvSeedPrincipalScenario',
 'rvPrepareProduct','rvSelectedCatalog','rvRead','rvSession','rvCapabilities','clubBalances','clubPrincipalState',
 'clubPrincipalQuote','clubPrincipalRecords','ciLots','clubPlan','clubReconcile','coApply','ciSeal','ciOpen','ciQuote','quoteOrder']


@dataclass(frozen=True)
class ActualServiceProfile:
    source: Path
    revision: str
    image: str

    def __post_init__(self):
        object.__setattr__(self,'source',_directory(self.source))
        if not re.fullmatch('[a-f0-9]{40}',self.revision) or not re.fullmatch(r'node@sha256:[a-f0-9]{64}',self.image):
            raise ValueError('pinned service source and runtime image required')

    @property
    def base_sha(self):return self.revision

    def source_check(self):
        status=observe_checkout(self.source)
        if status['sha']!=self.revision or not status['tracked_and_visible_untracked_clean']:
            raise Conflict('actual service checkout changed')
        return status


def _docker_env():
    return {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':'/nonexistent','DOCKER_CONFIG':'/nonexistent',
            'DOCKER_HOST':'unix:///run/codex-docker/docker.sock'}


def prepare(profile,root,adapter_root):
    profile.source_check();source=root/'sut';source.mkdir()
    _archive_source(profile,source)
    manifest=json.loads((source/'.openai/hosting.json').read_text())
    if manifest.get('project_id')!='appgprj_6af881381bdc8191843d4e4f742948dd':raise Conflict('wrong Site source')
    parts=[];hashes={}
    paths=['dist/'+name+'.mjs' for name in CORE]+['dist/review-pages.mjs','dist/review-member-pages.mjs','server-src/review-server.mjs']
    for name in paths:
        raw=(source/name).read_bytes();hashes[name]=hashlib.sha256(raw).hexdigest()
        # Exact linking transform used by the service's own build-worker.mjs.
        parts.append(re.sub(r'^import .*;\n','',raw.decode(),flags=re.M).replace('export ',''))
    (root/'sut.bundle.mjs').write_text('\n'.join(parts)+'\nexport {'+','.join(EXPORTS)+'};\n')
    (root/'config.json').write_text(json.dumps(dict(source_revision=profile.revision,virtual_clock='2026-11-16T01:00:00.000Z')))
    for path in adapter_root.glob('*.mjs'):(root/path.name).write_bytes(path.read_bytes())
    for path in root.rglob('*'):path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)
    return dict(source_files=hashes,linking='same import/export removal as original build-worker; function bodies unchanged',
        input_source_revision=profile.revision,bundle_digest=hashlib.sha256((root/'sut.bundle.mjs').read_bytes()).hexdigest())


def execute_actual_service(profile,adapter_root,*,timeout=90,entrypoint="run.mjs",heartbeat=None,exchange=None,inventory_contract=None,role="main",business=None,business_contract=None,runtime_secrets=None):
    if entrypoint not in ("run.mjs","run-batch03.mjs","run-batch04.mjs","run-batch05.mjs"):raise ValueError("registered entrypoint required")
    if type(timeout) is not int or not 1<=timeout<=120:raise ValueError('bounded execution required')
    if entrypoint in ('run-batch04.mjs','run-batch05.mjs'):
        from .hope_shared_inventory import validate_contract,check_exchange
        allowed=('main','race-a','race-b') if entrypoint=='run-batch04.mjs' else ('setup','crash-before-hold','crash-after-hold','crash-before-store','crash-after-store','crash-before-response','recover','complete-unknown','race-a','race-b','main')
        if role not in allowed:raise ValueError('registered execution role required')
        inventory_pin=validate_contract(inventory_contract,profile.revision)
        exchange=check_exchange(exchange,inventory_pin)
    elif exchange is not None or inventory_contract is not None or role!='main':raise ValueError('shared data only for registered batch04 entrypoint')
    if entrypoint=='run-batch05.mjs':
        from .hope_persistent_review import check_business,business_pin
        business=check_business(business,business_contract,profile.revision);business_digest=business_pin(business_contract,profile.revision)
        if not isinstance(runtime_secrets,dict) or set(runtime_secrets)!={'REVIEW_DATA_KEY'} or not re.fullmatch('[A-Za-z0-9+/]{43}=',runtime_secrets['REVIEW_DATA_KEY']):raise ValueError('memory-only review key required')
    elif business is not None or business_contract is not None or runtime_secrets is not None:raise ValueError('business binding only for registered persistent entrypoint')
    profile.source_check()
    # Only an already-prepared pinned image; execution never pulls another image.
    image=subprocess.run(['docker','image','inspect',profile.image],env=_docker_env(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    if image.returncode:raise Conflict('approved Node image unavailable')
    with tempfile.TemporaryDirectory(prefix='hope-real-source-') as folder:
        root=Path(folder);pin=prepare(profile,root,Path(adapter_root))
        name='hope-qa-'+uuid.uuid4().hex
        if exchange is not None:
            cfg=json.loads((root/'config.json').read_text());cfg.update(inventory_contract=inventory_contract,inventory_digest=inventory_pin,role=role,execution_id=name)
            if business is not None:cfg.update(business_contract=business_contract,business_digest=business_digest)
            (root/'config.json').write_text(json.dumps(cfg));(root/'config.json').chmod(0o644)
        args=['docker','run','--rm','--name',name,'--pull','never','--network','none','--read-only',
          '--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534',
          '--tmpfs','/tmp:rw,nosuid,nodev,size=134217728,mode=1777',
          '--ulimit','cpu=35:35','--ulimit','nofile=256:256','--ulimit','nproc=128:128',
          '--ulimit','fsize=67108864:67108864','--mount','type=bind,source='+str(root)+',target=/input,readonly',
          profile.image,'node','--max-old-space-size=384','--permission',
          '--allow-fs-read=/input','--allow-fs-read=/tmp','--allow-fs-write=/tmp','/input/'+entrypoint]
        if exchange is not None:
            position=args.index(profile.image)
            args[position:position]=['--mount','type=bind,source='+str(exchange)+',target=/exchange']
            args[-1:-1]=['--allow-fs-read=/exchange','--allow-fs-write=/exchange']
        if business is not None:
            position=args.index(profile.image);args[position:position]=['--interactive','--mount','type=bind,source='+str(business)+',target=/business']
            args[-1:-1]=['--allow-fs-read=/business','--allow-fs-write=/business']
        started=time.monotonic()
        if heartbeat:heartbeat()
        try:
            with tempfile.TemporaryFile() as output:
                process=subprocess.Popen(args,env=_docker_env(),stdin=subprocess.PIPE if business is not None else subprocess.DEVNULL,stdout=output,stderr=subprocess.DEVNULL)
                if business is not None:
                    process.stdin.write(json.dumps(runtime_secrets).encode());process.stdin.close()
                while process.poll() is None:
                    if heartbeat:heartbeat()
                    if time.monotonic()-started>timeout or os.fstat(output.fileno()).st_size>8*1024*1024:
                        subprocess.run(['docker','rm','-f',name],env=_docker_env(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
                        process.wait(timeout=10);raise Conflict('actual service execution uncertain or limited')
                    time.sleep(0.1)
                if entrypoint=='run-batch05.mjs' and role.startswith('crash-') and process.returncode==86:
                    report=dict(schema='hope-persistent-interruption/v1',source_revision=profile.revision,role=role,interrupted=True,exit_code=86,operational_authorization=False)
                else:
                    if process.returncode:raise Conflict('isolated service harness failed; no successful evidence adopted')
                    output.seek(0);raw=output.read(8*1024*1024+1)
                    if len(raw)>8*1024*1024:raise Conflict('service evidence limit')
                    report=json.loads(raw)
            profile.source_check()
            if heartbeat:heartbeat()
            report['source_provenance']=pin
            report['runtime_provenance']=dict(image=profile.image,network='none',read_only=True,user=65534,
                node_heap_mib=384,cpu_rlimit_seconds=35,cgroup_memory_enforced=False,
                arbitrary_host_execution=False,container_name=name,execution_role=role,shared_fixture_mount=exchange is not None,persistent_business_mount=business is not None,process_exited=True,elapsed_seconds=round(time.monotonic()-started,3))
            return report
        finally:
            subprocess.run(['docker','rm','-f',name],env=_docker_env(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            if 'process' in locals():
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            # Recheck after the CLI exits: cancellation can race container creation.
            subprocess.run(['docker','rm','-f',name],env=_docker_env(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
