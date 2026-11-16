"""Prepare trusted customer host bindings; never read credentials or run customer code.

Packets are prerequisites, not executable workers. The completed private config
is consumed by continuous_host.load_host and its existing ProjectHost transports.
"""
import hashlib
import json
from pathlib import Path
import re
from .agent_directory import _reject_known_tokens
from .continuous_host import _observe
from .fleet_codex import _uuid
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, canonical_json, digest, _sha, _reject_credentials


def _path(value):
    if type(value) is not str or not value.startswith('/'):raise ValueError('absolute host path required')
    path=Path(value)
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('physical host path required')
    return path


def _overlap(a,b):return a==b or a in b.parents or b in a.parents


class CustomerWorkerProfiles:
    def __init__(self,store,registry,core=None):
        if registry.store is not store or (core is not None and core.store is not store):raise ValueError('shared core and registry required')
        self.store,self.registry,self.core=store,registry,core
        self.catalog=json.loads(Path(__file__).with_name('maintenance_projects.json').read_text())

    def prepare(self,token,project,source,*,base_sha,checks,evidence_files,paths,profiles,session_id=None):
        """Inspect exact source and selected requirements evidence; return pending host entry.

        All paths/settings are trusted host inputs, never customer/model arguments.
        Token/profile contents and authentication state are intentionally unobserved.
        """
        _reject_credentials({'project':project,'source':str(source),'checks':checks,'files':evidence_files,'paths':paths,'profiles':profiles,'session_id':session_id},token)
        if project not in self.catalog:raise ValueError('registered customer catalog project required')
        config=self.registry.get(token,project);_sha(base_sha)
        if session_id is not None:_uuid(session_id)
        source=_path(str(source));observation=_observe(source)
        if observation['sha']!=base_sha or not observation['tracked_and_visible_untracked_clean']:
            raise Conflict('exact clean source HEAD required')
        if type(checks) is not dict or not 1<=len(checks)<=30:raise ValueError('fixed bounded command set required')
        for key,argv in checks.items():
            name(key)
            if type(argv) is not list or not 1<=len(argv)<=50 or any(type(x) is not str or not x or len(x)>4000 for x in argv):raise ValueError('fixed command argv required')
        if type(paths) is not dict or set(paths)!={'candidates','runtime','developer_token_file','reviewer_token_file'}:raise ValueError('exact isolated host path references required')
        fixed={k:_path(v) for k,v in paths.items()}
        if type(profiles) is not dict or set(profiles)!={'developer','reviewer'}:raise ValueError('distinct developer and reviewer profiles required')
        copied={};homes=[]
        for role,setting in profiles.items():
            if (type(setting) is not dict or not {'binary','codex_home'}<=set(setting)
                    or set(setting)-{'binary','codex_home','require_origin','execution_timeout'}):raise ValueError('fixed Codex transport settings required')
            _path(setting['binary']);homes.append(_path(setting['codex_home']))
            if 'require_origin' in setting and type(setting['require_origin']) is not bool:raise ValueError('explicit origin policy required')
            if 'execution_timeout' in setting and (type(setting['execution_timeout']) is not int or not 1<=setting['execution_timeout']<=3600):raise ValueError('bounded transport timeout required')
            copied[role]=dict(setting)
        protected=[*fixed.values(),*homes,self.store.path]
        if any(_overlap(source,path) for path in protected):raise ValueError('source must be separate from credentials/runtime/candidates/state/profiles')
        if _overlap(homes[0],homes[1]) or fixed['developer_token_file']==fixed['reviewer_token_file']:raise ValueError('independent role credentials and profiles required')
        roots=[fixed['runtime'],fixed['candidates'],*homes]
        if any(_overlap(a,b) for i,a in enumerate(roots) for b in roots[i+1:]):raise ValueError('isolated runtime, candidates and role profiles required')
        if any(root==f or root in f.parents for root in roots for f in (fixed['developer_token_file'],fixed['reviewer_token_file'],self.store.path)):
            raise ValueError('private credentials and database must be outside execution/profile roots')
        if type(evidence_files) is not list or not 1<=len(evidence_files)<=50 or len(set(evidence_files))!=len(evidence_files):raise ValueError('bounded explicit requirements evidence required')
        evidence=[]
        for relative in evidence_files:
            if type(relative) is not str or not re.fullmatch(r'(README(?:\.[A-Za-z0-9_-]+)?|package\.json|requirements\.txt|pyproject\.toml|tests/[A-Za-z0-9_./-]+\.(?:py|js|mjs|ts))',relative):raise ValueError('only explicit readme/package/test evidence allowed')
            path=source/relative
            if '..' in Path(relative).parts or any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file() or path.stat().st_size>128000:raise ValueError('bounded physical requirements evidence required')
            content=path.read_bytes();content.decode('utf-8')
            evidence.append({'path':relative,'sha256':hashlib.sha256(content).hexdigest(),'bytes':len(content)})
        if _observe(source)!=observation:raise Conflict('source changed during requirements inspection')
        repository=config['config']['source_ref'] if config['config']['source_kind']=='github' else 'sites/'+config['config']['source_ref']
        entry={'project':project,'repository':repository,'source':str(source),**{k:str(v) for k,v in fixed.items()},
               'developer_id':project+'-developer','reviewer_id':project+'-reviewer','checks':checks,**copied}
        if session_id is not None:entry['session_id']=session_id
        pending=['role_memberships','private_role_tokens','codex_profile_authentication','independent_runtime','capacity_limits','qa_contract','source_connector','private_load_host_validation']
        if session_id is None:pending.append('registered_session_or_approved_fleet_template')
        manifest={'project':project,'base_sha':base_sha,'repository':repository,'config_fingerprint':config['fingerprint'],
                  'commandset_digest':digest(checks),'inspected_files':evidence,'requirements_scope':'selected_files_only',
                  'all_requirements_known':False,'tests_executed':0}
        result={'project':project,'requirements_manifest':manifest,'requirements_digest':digest(manifest),
                'continuous_host_project':entry,'loader':'manager_bridge.continuous_host.load_host',
                'pending_prerequisites':pending,'authentication_verified':False,'execution_ready':False,
                'maintenance_enabled':config['config']['maintenance_enabled'],'operational_authorized':False}
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('configuration changed during profile preparation')
            _reject_known_tokens(c,result)
        return result

    def bind_roles(self,owner,project,developer_credential_hash,reviewer_credential_hash,manager_hash):
        """Atomically apply ThreeTier registration invariants using existing hashed identities."""
        if self.core is None:raise ValueError('existing ThreeTier core required')
        if project not in self.catalog:raise ValueError('catalog customer required')
        wanted=[(project+'-developer',developer_credential_hash,'developer',project+'-developer-agent'),
                (project+'-reviewer',reviewer_credential_hash,'reviewer',project+'-reviewer-agent'),
                (project+'-manager',manager_hash,'taesan',None)]
        if len({x[1] for x in wanted})!=3:raise Conflict('three independent identities required')
        from .three_tier import _hash
        for _,credential,_,_ in wanted:_hash(credential)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,owner);self.store._role(actor,('owner',))
            config=self.core._config(c,owner,project,('owner',));prepared=[]
            for member,credential,role,agent in wanted:
                live=self.core._live(c,credential,project)
                if live['role']!=('manager' if role=='taesan' else 'worker'):raise PermissionDenied('worker/manager role mismatch')
                needed='development' if role=='developer' else 'test' if role=='reviewer' else None
                if needed and needed not in json.loads(live['capabilities']):raise PermissionDenied('required worker capability missing')
                others=c.execute('SELECT role FROM hierarchy_members WHERE project=? AND subject=? AND member_id!=?',(project,live['subject'],member)).fetchall()
                if any(x['role']!=role for x in others) or any(x[3]==live['subject'] for x in prepared):raise Conflict('independent role subjects required')
                existing=c.execute('SELECT * FROM hierarchy_members WHERE project=? AND member_id=?',(project,member)).fetchone()
                values=(credential,live['subject'],role,agent,config['fingerprint'])
                if existing and tuple(existing[k] for k in ('credential_hash','subject','role','agent_id','config_fingerprint'))!=values:
                    if c.execute("SELECT 1 FROM hierarchy_tasks WHERE project=? AND state!='accepted' AND (developer_id=? OR reviewer_id=?)",(project,member,member)).fetchone():raise Conflict('active hierarchy membership cannot change')
                prepared.append((project,member,*values))
            for row in prepared:c.execute('INSERT OR REPLACE INTO hierarchy_members VALUES (?,?,?,?,?,?,?)',row)
            self.store._role(self.store._authenticate(c,owner),('owner',))
            if self.core._config(c,owner,project,('owner',))['fingerprint']!=config['fingerprint']:
                raise Conflict('configuration changed during atomic role binding')
            for _,credential,_,_ in wanted:self.core._live(c,credential,project)
            return {'project':project,'members':[{'member_id':r[1],'role':r[4]} for r in prepared],
                    'connection_verified':False,'maintenance_changed':False,'operational_authorized':False}
