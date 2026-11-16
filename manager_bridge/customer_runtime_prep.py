"""Prepare locked dependency copies, never launch customer or lifecycle code.

Only explicit trusted host paths and an exact committed source SHA are accepted.
The original checkout is observed before and after; installation occurs in a new
private copy. Manifests attest files prepared, not sandbox tests or provider QA.
"""
import base64
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler
from .continuous_host import _observe, _git
from .store import Conflict, digest, _sha

NODE = '/opt/codex/runtimes/codex-primary-runtime/dependencies/node/bin/node'
NPM = '/opt/codex/runtimes/codex-primary-runtime/dependencies/node/bin/npm'


def _sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def _physical(path,*,directory=True):
    result=Path(path).absolute()
    if '..' in result.parts or any(p.is_symlink() for p in (result,*result.parents)):
        raise ValueError('physical trusted host path required')
    if directory and not result.is_dir():raise ValueError('existing host directory required')
    return result


def _json_input(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size>4*1024*1024:
        raise Conflict('bounded physical dependency metadata required')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise Conflict('duplicate dependency metadata key')
            result[key]=value
        return result
    try:return json.loads(path.read_text(),object_pairs_hook=pairs)
    except (ValueError,UnicodeError) as exc:raise Conflict('malformed dependency metadata') from exc


def _dependency_metadata(value):
    if type(value) is not dict:raise Conflict('dependency metadata object required')
    if value.get('workspaces') or value.get('link') or value.get('inBundle') or value.get('overrides') or value.get('bundledDependencies') or value.get('bundleDependencies'):
        raise Conflict('workspace/link/bundled/override dependencies unsupported')
    for field in ('dependencies','devDependencies','optionalDependencies','peerDependencies'):
        deps=value.get(field,{})
        if type(deps) is not dict:raise Conflict('registry dependency map required')
        for package,spec in deps.items():
            if (type(package) is not str or not re.fullmatch(r'(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+',package)
                    or type(spec) is not str or not re.fullmatch(r'[A-Za-z0-9.*+<>=~^| -]{1,200}',spec)):
                raise Conflict('only pinned registry dependency version metadata supported')


def validate_locked_registry_inputs(package_path,lock_path):
    """Validate the ENTIRE lock, including omitted dev packages, before npm runs.

    Registry flags alone do not constrain lockfile resolved/file/git targets.
    This deliberately rejects aliases, workspaces, bundles and overrides too.
    """
    package=_json_input(Path(package_path));lock=_json_input(Path(lock_path))
    _dependency_metadata(package)
    if type(lock) is not dict or type(lock.get('lockfileVersion')) is not int or lock['lockfileVersion'] not in (2,3):
        raise Conflict('npm package-map lock version 2 or 3 required')
    packages=lock.get('packages')
    if type(packages) is not dict or '' not in packages or not 1<=len(packages)<=5000:
        raise Conflict('bounded exact npm package map required')
    if lock.get('dependencies'):
        # v2 may contain a second resolution graph; reject rather than leave an
        # unvalidated alternate target available to the installer.
        raise Conflict('alternate npm resolution graph unsupported')
    summaries=[]
    for location,entry in packages.items():
        if type(location) is not str or type(entry) is not dict:raise Conflict('exact lock package entry required')
        _dependency_metadata(entry)
        if location=='':
            if 'resolved' in entry or 'integrity' in entry:raise Conflict('root package cannot declare a fetched target')
            for field in ('dependencies','devDependencies','optionalDependencies','peerDependencies'):
                if package.get(field,{})!=entry.get(field,{}):raise Conflict('package and lock root dependency metadata differ')
            continue
        component=r'node_modules/(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+'
        if not re.fullmatch(component+r'(?:/'+component+r')*',location) or any(p in ('.','..') for p in location.split('/')):
            raise Conflict('nonregistry lock package location rejected')
        if type(entry.get('version')) is not str or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?',entry['version']):
            raise Conflict('exact registry package version required')
        url=entry.get('resolved');integrity=entry.get('integrity')
        if type(url) is not str or not 1<=len(url)<=1000 or any(ch.isspace() for ch in url):raise Conflict('exact registry tarball target required')
        parsed=urlsplit(url)
        if (parsed.scheme!='https' or parsed.netloc!='registry.npmjs.org' or parsed.query or parsed.fragment
                or not parsed.path.endswith('.tgz') or not re.fullmatch(r'/[A-Za-z0-9@_./+-]+',parsed.path)
                or any(p in ('.','..') for p in parsed.path.split('/'))):
            raise Conflict('only credential-free HTTPS npm registry tarballs allowed')
        if type(integrity) is not str or not 1<=len(integrity)<=512:raise Conflict('fetched package integrity required')
        for item in integrity.split():
            match=re.fullmatch(r'(sha1|sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})',item)
            if not match:raise Conflict('valid fetched package integrity required')
            try:decoded=base64.b64decode(match[2],validate=True)
            except ValueError as exc:raise Conflict('malformed package integrity') from exc
            if len(decoded)!={'sha1':20,'sha256':32,'sha384':48,'sha512':64}[match[1]]:raise Conflict('package integrity digest length invalid')
        if not integrity.split():raise Conflict('fetched package integrity required')
        summaries.append({'location':location,'resolved':url,'integrity':integrity})
    return {'fetched_packages_validated':len(summaries),'all_packages_including_dev_validated':True,'resolution_digest':digest(summaries)}


def inspect_dependency_tree(root):
    """Hash every actual regular file; links/special files are never attested."""
    root=_physical(root);files=[];packages=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError('dependency tree must contain physical files only')
        if path.is_dir():continue
        if not path.is_file():raise ValueError('special dependency file rejected')
        relative=path.relative_to(root).as_posix()
        files.append({'path':relative,'sha256':_sha256(path),'bytes':path.stat().st_size})
        if path.name=='package.json':
            package=json.loads(path.read_text())
            if type(package.get('name')) is str and type(package.get('version')) is str:
                packages.append({'path':relative,'name':package['name'],'version':package['version']})
    installed=packages
    installed_lock=root/'.package-lock.json'
    if installed_lock.is_file():
        lock=json.loads(installed_lock.read_text())
        installed=[]
        for location,entry in sorted(lock.get('packages',{}).items()):
            if location and type(entry) is dict and type(entry.get('version')) is str:
                installed.append({'name':entry.get('name') or location.rsplit('node_modules/',1)[-1],
                                  'version':entry['version'],'location':location,'integrity':entry.get('integrity')})
    return {'file_count':len(files),'bytes':sum(f['bytes'] for f in files),'files':files,
            'tree_digest':digest(files),'packages':packages,'installed_packages':installed,'sbom_root':digest(installed)}


def _verify_committed_dependency_inputs(source,expected_sha,copied):
    hashes={}
    for filename in ('package.json','package-lock.json'):
        raw=_git(source,'show',expected_sha+':'+filename)
        path=copied/filename
        if not path.is_file() or path.is_symlink() or path.read_bytes()!=raw:
            raise Conflict('archived dependency input differs from exact committed blob')
        hashes[filename]=hashlib.sha256(raw).hexdigest()
    return hashes


def prepare_locked_node(source,expected_sha,target,*,node=NODE,npm=NPM,omit_dev=True,timeout=240):
    """Run npm ci with lifecycle scripts disabled in a fresh committed-tree copy.

    Never reuse a prior target; each installation gets its own cache, config and
    log. A failure retains its diagnostic copy but cannot produce ready evidence.
    Caller authorizes package registry access; no customer service is started.
    """
    _sha(expected_sha);source=_physical(source);target=_physical(target,directory=False)
    if type(omit_dev) is not bool or type(timeout) is not int or not 1<=timeout<=900:raise ValueError('bounded explicit dependency preparation required')
    if target.exists() or target==source or source in target.parents or target in source.parents:
        raise ValueError('new separate dependency preparation directory required')
    before=_observe(source)
    if before['sha']!=expected_sha or not before['tracked_and_visible_untracked_clean']:
        raise Conflict('exact clean committed source required')
    node=_physical(node,directory=False);npm=Path(npm).absolute()
    if not node.is_file() or not npm.is_file():raise ValueError('installed trusted Node/npm required')
    env={'PATH':str(node.parent)+':/usr/bin:/bin','LANG':'C.UTF-8',
         'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1','GIT_TERMINAL_PROMPT':'0',
         'GIT_NO_LAZY_FETCH':'1','GIT_NO_REPLACE_OBJECTS':'1'}
    # Package downloads use the managed egress proxy, never npm auth or HOME config.
    # Proxy values are passed only to the installer and are never serialized.
    for key in ('HTTP_PROXY','HTTPS_PROXY','NO_PROXY','http_proxy','https_proxy','no_proxy','NODE_EXTRA_CA_CERTS','SSL_CERT_FILE'):
        if key in os.environ:env[key]=os.environ[key]
    target.mkdir(parents=True,mode=0o700);os.chmod(target,0o700)
    copied=target/'source';copied.mkdir(mode=0o700)
    archive=target/'committed-source.tar'
    with archive.open('wb') as output:
        result=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(source),'archive','--format=tar',expected_sha],env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.PIPE,timeout=30)
    if result.returncode or archive.stat().st_size>128*1024*1024:raise Conflict('bounded committed source archive failed')
    with tarfile.open(archive) as bundle:bundle.extractall(copied,filter='data')
    if (copied/'node_modules').exists() or (copied/'node_modules').is_symlink() or (copied/'.npmrc').exists() or (copied/'.npmrc').is_symlink():raise Conflict('source dependency/config override rejected')
    lock=copied/'package-lock.json';package=copied/'package.json'
    if not lock.is_file() or not package.is_file() or lock.is_symlink() or package.is_symlink():raise Conflict('committed npm lock and package manifest required')
    committed_inputs=_verify_committed_dependency_inputs(source,expected_sha,copied)
    validation=validate_locked_registry_inputs(package,lock)
    lock_digest=_sha256(lock);package_digest=_sha256(package)
    config=target/'empty.npmrc';config.write_text('');os.chmod(config,0o600)
    global_config=target/'global-empty.npmrc';global_config.write_text('');os.chmod(global_config,0o600)
    command=[str(npm),'ci','--ignore-scripts','--no-audit','--no-fund','--registry=https://registry.npmjs.org',
             '--userconfig='+str(config),'--globalconfig='+str(global_config),'--cache='+str(target/'cache')]
    if omit_dev:command.append('--omit=dev')
    log=target/'npm-ci.log'
    with log.open('wb') as output:
        installed=subprocess.run(command,cwd=copied,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,timeout=timeout)
    if _observe(source)!=before:raise Conflict('original source changed during dependency preparation')
    if _sha256(lock)!=lock_digest or _sha256(package)!=package_digest:raise Conflict('npm modified committed dependency inputs')
    if installed.returncode:raise Conflict('dependency installation failed; inspect private npm-ci.log')
    dependencies=target/'dependencies';dependencies.mkdir(mode=0o700)
    original_modules=copied/'node_modules';modules=dependencies/'node_modules'
    if original_modules.exists():
        # npm .bin links are materialized only after proving all targets stay in
        # the installed dependency tree; runner mounts a link-free physical copy.
        for path in original_modules.rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(original_modules.resolve()):
                raise Conflict('dependency link escapes installation tree')
        shutil.copytree(original_modules,modules,symlinks=False)
    else:modules.mkdir()
    tree=inspect_dependency_tree(modules)
    version=subprocess.run([str(node),'--version'],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
    if version.returncode:raise Conflict('Node binary version observation failed')
    npm_version=subprocess.run([str(npm),'--version','--userconfig='+str(config),'--globalconfig='+str(global_config)],cwd=target,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
    if npm_version.returncode:raise Conflict('npm version observation failed')
    manifest={'schema_version':'customer-runtime-dependencies/v1','source_sha':expected_sha,
              'source_root':str(source),'copied_source_root':str(copied),'dependency_root':str(modules),
              'lock_sha256':lock_digest,'package_sha256':package_digest,'committed_input_hashes':committed_inputs,'lock_validation':validation,'npm_command':command,
              'lifecycle_scripts_executed':False,'omit_dev':omit_dev,'log_sha256':_sha256(log),
              'node':{'path':str(node),'version':version.stdout.decode().strip(),'sha256':_sha256(node)},
              'npm':{'path':str(npm),'version':npm_version.stdout.decode().strip(),'sha256':_sha256(npm.resolve())},
              'dependencies':tree,'customer_tests_executed':False,'sandbox_verified':False,
              'database_started':False,'public_network_listener_started':False,'operational_authorized':False}
    (target/'dependency-manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    return manifest


def prepare_locked_registry_package(source,expected_sha,target,package_name,version):
    """Extract one exact registry/SRI-pinned prerequisite without executing it.

    The whole source lock is validated first, including omitted packages. No
    package manager, lifecycle hook, compiler or customer module is executed.
    """
    _sha(expected_sha);source=_physical(source);target=_physical(target,directory=False)
    if target.exists() or target==source or source in target.parents or target in source.parents:
        raise ValueError('new separate package preparation directory required')
    before=_observe(source)
    if before['sha']!=expected_sha or not before['tracked_and_visible_untracked_clean']:
        raise Conflict('exact clean committed source required')
    package_path=source/'package.json';lock_path=source/'package-lock.json'
    validation=validate_locked_registry_inputs(package_path,lock_path)
    if type(package_name) is not str or not re.fullmatch(r'(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+',package_name):raise ValueError('exact locked package name required')
    lock=_json_input(lock_path);entry=lock['packages'].get('node_modules/'+package_name)
    if not entry or entry.get('version')!=version:raise Conflict('exact source-locked package version required')
    lock_hash=_sha256(lock_path);package_hash=_sha256(package_path)
    target.mkdir(parents=True,mode=0o700);os.chmod(target,0o700)
    archive=target/'registry-package.tgz';total=0
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise Conflict('registry redirect rejected before follow')
    opener=build_opener(NoRedirect())
    with opener.open(entry['resolved'],timeout=30) as response,archive.open('wb') as output:
        # A redirect could leave the validated registry. Never accept one.
        if response.geturl()!=entry['resolved']:raise Conflict('registry redirect rejected')
        for chunk in iter(lambda:response.read(1024*1024),b''):
            total+=len(chunk)
            if total>32*1024*1024:raise Conflict('bounded registry tarball required')
            output.write(chunk)
    for integrity in entry['integrity'].split():
        algorithm,expected=integrity.split('-',1);observed=hashlib.new(algorithm)
        with archive.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):observed.update(chunk)
        if not hmac.compare_digest(observed.digest(),base64.b64decode(expected,validate=True)):
            raise Conflict('registry tarball integrity mismatch')
    extracted=target/'extracted';extracted.mkdir(mode=0o700)
    with tarfile.open(archive,'r:gz') as bundle:
        members=bundle.getmembers()
        if len(members)>10000 or sum(member.size for member in members)>64*1024*1024:raise Conflict('bounded extracted prerequisite required')
        if any(not (member.isfile() or member.isdir()) for member in members):raise Conflict('registry package links/special files rejected')
        bundle.extractall(extracted,filter='data')
    root=extracted/'package';tree=inspect_dependency_tree(root)
    manifest_package=_json_input(root/'package.json')
    if manifest_package.get('name')!=package_name or manifest_package.get('version')!=version:
        raise Conflict('registry package identity differs from locked prerequisite')
    if _observe(source)!=before or _sha256(lock_path)!=lock_hash or _sha256(package_path)!=package_hash:
        raise Conflict('original source/lock changed during prerequisite preparation')
    result={'schema_version':'source-locked-registry-prerequisite/v1','source_sha':expected_sha,
            'source_root':str(source),'lock_sha256':lock_hash,'package_sha256':package_hash,
            'lock_validation':validation,'name':package_name,'version':version,'package_root':str(root),
            'resolved':entry['resolved'],'integrity':entry['integrity'],'integrity_verified':True,
            'tarball_sha256':_sha256(archive),'files':tree,'lifecycle_scripts_executed':False,
            'package_code_executed':False,'customer_tests_executed':False,'operational_authorized':False}
    (target/'package-manifest.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return result


def inspect_optional_prerequisites():
    """Observe installed tools only; do not start Docker, databases or browsers."""
    def binary(name):return shutil.which(name)
    return {'postgres':binary('postgres'),'psql':binary('psql'),'chromium':binary('chromium'),
            'docker_client':binary('docker'),'database_started':False,'browser_started':False,
            'daemon_access_verified':False,'runtime_ready':False}
