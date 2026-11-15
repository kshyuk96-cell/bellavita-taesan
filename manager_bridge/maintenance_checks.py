"""Approved fixed regression plans and read-only dependency proposals.

The default has no execution provider. LocalFixtureExecutor is exclusively a test
fixture runner, not a security sandbox for arbitrary project code. Real source
must use a host-registered isolated executor; it must not receive runtime secrets.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import time

from .maintenance_registry import name, receipt, scoped
from .regression_selection import RegressionSelector
from .store import Conflict, NotFound, PermissionDenied, _reject_credentials, _sha, canonical_json, digest, _task_id


def _private_path(value):
    path=Path(value).absolute()
    if '..' in path.parts:
        raise ValueError('parent path traversal forbidden')
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('symlink checkout forbidden')
    return path


def _git_source(checkout):
    env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_GLOBAL':'/dev/null',
         'GIT_CONFIG_SYSTEM':'/dev/null','GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1',
         'GIT_ALLOW_PROTOCOL':''}
    def git(*argv):
        result=subprocess.run(['/usr/bin/git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-c','core.untrackedCache=false','-C',str(checkout),*argv],env=env,
                              capture_output=True,text=True,timeout=10)
        if result.returncode or len(result.stdout)>1024*1024:
            raise ValueError('source observation unavailable')
        return result.stdout.strip()
    sha=git('rev-parse','HEAD')
    _sha(sha)
    # No task-provided revision expression, hooks, or transport is evaluated.
    if git('status','--porcelain','--untracked-files=all'):
        raise Conflict('disposable checkout must be clean')
    return sha


@dataclass(frozen=True)
class CheckHost:
    checkout:object
    source_sha:str
    selector:RegressionSelector
    executor:object=None
    timeout:int=30
    observe_source:object=None

    def __post_init__(self):
        path=_private_path(self.checkout)
        if not path.is_dir():raise ValueError('registered disposable checkout required')
        _sha(self.source_sha)
        if not isinstance(self.selector,RegressionSelector):raise ValueError('registered selector required')
        if self.executor is not None and not callable(self.executor):raise ValueError('isolated executor required')
        if self.observe_source is not None and not callable(self.observe_source):raise ValueError('source observer required')
        if type(self.timeout) is not int or not 1<=self.timeout<=300:raise ValueError('bounded check timeout required')
        # Defensive selector copy. Registration changes require a new plan/approval.
        copied=RegressionSelector(dict(self.selector.checks),dict(self.selector.mapping),
                                  required_checks=self.selector.required,full_checks=self.selector.full)
        object.__setattr__(self,'checkout',path)
        object.__setattr__(self,'selector',copied)

    def observed_sha(self):
        _private_path(self.checkout)
        value=(self.observe_source(self.checkout) if self.observe_source else _git_source(self.checkout))
        _sha(value)
        return value

    def binding(self):
        return digest({'checkout':str(self.checkout),'source_sha':self.source_sha,
                       'checks':self.selector.checks,'mapping':self.selector.mapping,
                       'required':self.selector.required,'full':self.selector.full,'timeout':self.timeout})


class LocalFixtureExecutor:
    """Execute explicitly marked synthetic fixtures only, with a minimal environment.

    No sandbox bypass or operational source execution. The marker is a host-trusted
    fixture convention, not a protection against malicious source. Real check hosts
    must register an actual sandbox executor instead. Output is never returned/logged.
    """
    def __init__(self,fixture_root):
        self.root=_private_path(fixture_root)
        if not self.root.is_dir():raise ValueError('fixture root required')

    def __call__(self,check_name,argv,checkout,timeout):
        name(check_name)
        checkout=_private_path(checkout)
        _private_path(self.root)
        if not checkout.is_relative_to(self.root) or checkout==self.root:
            raise PermissionDenied('fixture outside registered root')
        marker=checkout/'.taesan-synthetic-fixture'
        if marker.is_symlink() or not marker.is_file() or marker.read_text().strip()!='synthetic-only':
            raise PermissionDenied('synthetic fixture marker required')
        if type(argv) not in (tuple,list) or not argv or not Path(argv[0]).is_absolute() or any(type(v) is not str or '\x00' in v for v in argv):
            raise ValueError('fixed argv required')
        env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8',
             'PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1',
             'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null',
             'GIT_TERMINAL_PROMPT':'0','TAESAN_FIXTURE_ONLY':'1'}
        start=time.monotonic()
        process=subprocess.Popen(tuple(argv),cwd=checkout,env=env,start_new_session=True,
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        timed_out=False
        try:
            code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True
            os.killpg(process.pid,signal.SIGKILL)
            code=process.wait()
        finally:
            # A successful parent must not leave background descendants behind.
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        return {'passed':code==0 and not timed_out,'timed_out':timed_out,
                'reference':'fixture-check/'+digest({'name':check_name,'argv':list(argv),'code':code}),
                'duration_seconds':round(time.monotonic()-start,6)}


class Checks:
    def __init__(self,store,registry,hosts=None):
        self.store,self.registry=store,registry
        self.hosts=dict(hosts or {})
        for project,host in self.hosts.items():
            name(project)
            if not isinstance(host,CheckHost):raise ValueError('registered check host required')
        with store._transaction() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS maintenance_check_plans (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, request_key TEXT NOT NULL,
                plan TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                approval TEXT, result TEXT, created REAL NOT NULL,
                UNIQUE(project,request_key));
            CREATE TABLE IF NOT EXISTS maintenance_check_reservations (
                repository TEXT PRIMARY KEY,plan_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS maintenance_check_parents (
                plan_id TEXT PRIMARY KEY,parent_task_id TEXT);''')

    def _row(self,c,token,key,roles=('owner','manager','worker')):
        row=c.execute('SELECT * FROM maintenance_check_plans WHERE id=?',(key,)).fetchone()
        if row is None:raise NotFound('check plan not found')
        scoped(self.store,c,token,row['project'],roles)
        return row

    def _current(self,c,token,project,host):
        config=self.registry._get(c,token,project,('owner','manager','worker'))
        if host.observed_sha()!=host.source_sha:raise Conflict('registered source SHA changed')
        return config

    def propose(self,token,project,request_key,changed_paths):
        name(request_key)
        host=self.hosts.get(project)
        if host is None:
            with self.store._transaction() as c:scoped(self.store,c,token,project,('owner','manager'))
            return {'project':project,'state':'not_configured','commands_executed':False}
        selection=host.selector.plan(changed_paths)
        with self.store._transaction() as c:
            scoped(self.store,c,token,project,('owner','manager'))
            config=self._current(c,token,project,host)
            plan={'project':project,'company':config['config']['company'],
                  'config_fingerprint':config['fingerprint'],'source_sha':host.source_sha,
                  'host_binding':host.binding(),'selection':selection}
            _reject_credentials(plan,token)
            fingerprint=digest(plan)
            old=c.execute('SELECT * FROM maintenance_check_plans WHERE project=? AND request_key=?',(project,request_key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise Conflict('check request key changed')
                return {'id':old['id'],'fingerprint':fingerprint,'state':old['state'],'plan':plan}
            key=secrets.token_hex(16)
            c.execute('INSERT INTO maintenance_check_plans VALUES (?,?,?,?,?,?,NULL,NULL,?)',
                      (key,project,request_key,canonical_json(plan),fingerprint,'pending_approval',self.store._now()))
            return {'id':key,'fingerprint':fingerprint,'state':'pending_approval','plan':plan}

    def approve(self,token,key,fingerprint):
        with self.store._transaction() as c:
            row=self._row(c,token,key,('owner',))
            if row['fingerprint']!=fingerprint:raise Conflict('exact plan approval required')
            if row['state'] not in ('pending_approval','approved'):raise Conflict('plan is no longer approvable')
            plan=json.loads(row['plan']);host=self.hosts.get(row['project'])
            if host is None:raise Conflict('check host disconnected')
            config=self._current(c,token,row['project'],host)
            if config['fingerprint']!=plan['config_fingerprint'] or host.binding()!=plan['host_binding']:
                raise Conflict('configuration changed; propose again')
            c.execute("UPDATE maintenance_check_plans SET state='approved',approval=? WHERE id=?",(fingerprint,key))
        return {'id':key,'state':'approved','operational_approval':False}

    def _core_guard(self,c,token,plan,repository,parent_task_id):
        """Coordinate one repository reservation domain with existing Store tasks.

        parent_task_id is a trusted server dispatch context, never an approval or
        an escape for managers. Only the assigned current worker can nest checks.
        """
        actor=self.store._authenticate(c,token)
        parent=None
        if parent_task_id is not None:
            _task_id(parent_task_id)
            if actor['role']!='worker':raise PermissionDenied('only assigned worker may nest checks')
            parent=self.store._get(c,actor,parent_task_id)
            manifest=json.loads(parent['manifest'])
            self.store._validate_execution(c,parent,check_source_age=False)
            approval=json.loads(parent['approval']) if parent['approval'] else {}
            if (parent['state']!='running' or parent['worker_token_hash']!=actor['token_hash']
                    or parent['worker_subject']!=actor['subject']
                    or parent['lease_expires'] is None or parent['lease_expires']<=self.store._now()):
                raise PermissionDenied('active assigned parent lease required')
            if (parent['project']!=plan['project'] or manifest['project']!=plan['project']
                    or manifest['repository']!=repository or manifest['base_sha']!=plan['source_sha']
                    or manifest['kind'] not in actor['capabilities']):
                raise PermissionDenied('parent task source or project mismatch')
            if (digest(manifest)!=parent['manifest_digest']
                    or approval.get('version')!=parent['version']
                    or approval.get('manifest_digest')!=parent['manifest_digest']):
                raise PermissionDenied('exact approved parent manifest required')
            selected={check['name'] for check in plan['selection']['checks']}
            if not selected<=set(manifest['checks']):
                raise PermissionDenied('selected checks outside approved parent scope')
            policy=c.execute('SELECT * FROM bridge_task_policy WHERE task=?',(parent_task_id,)).fetchone()
            if policy:
                from .coordination import Coordination
                if (approval.get('policy_digest')!=policy['policy_digest']
                        or not Coordination.dependencies_ready(c,json.loads(policy['policy']))):
                    raise PermissionDenied('parent policy or dependencies no longer valid')
        rows=c.execute("SELECT t.* FROM bridge_tasks t WHERE "
            "state IN ('running','reported','needs_verification') OR "
            "(state='cancelled' AND worker_subject IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id))").fetchall()
        for row in rows:
            if row['id']==parent_task_id:continue
            if json.loads(row['manifest'])['repository']==repository:return False
        return True

    def run(self,token,key,*,parent_task_id=None):
        with self.store._transaction() as c:
            row=self._row(c,token,key)
            actor=self.store._authenticate(c,token)
            if actor['role']=='worker' and 'test' not in actor['capabilities']:
                raise PermissionDenied('worker test capability required')
            plan=json.loads(row['plan'])
            repository=self.store._project_access(c,actor,row['project'])
            recorded_parent=c.execute('SELECT parent_task_id FROM maintenance_check_parents WHERE plan_id=?',(key,)).fetchone()
            if recorded_parent and recorded_parent['parent_task_id']!=parent_task_id:
                raise Conflict('check execution belongs to a different parent context')
            if not self._core_guard(c,token,plan,repository,parent_task_id):
                return {'id':key,'state':'busy','commands_executed':False}
            if row['result']:return json.loads(row['result'])
            if row['state']=='running':return {'id':key,'state':'uncertain','retry_performed':False}
            if row['state']!='approved' or row['approval']!=row['fingerprint']:
                raise PermissionDenied('owner approval required')
            host=self.hosts.get(row['project'])
            if host is None or host.executor is None:
                return {'id':key,'state':'not_configured','commands_executed':False}
            plan=json.loads(row['plan']);config=self._current(c,token,row['project'],host)
            if config['fingerprint']!=plan['config_fingerprint'] or host.binding()!=plan['host_binding']:
                raise Conflict('source or registered check configuration changed')
            if not config['config']['maintenance_enabled']:
                return {'id':key,'state':'disabled','commands_executed':False}
            reservation=c.execute('SELECT plan_id FROM maintenance_check_reservations WHERE repository=?',(repository,)).fetchone()
            if reservation and reservation['plan_id']!=key:
                return {'id':key,'state':'busy','commands_executed':False}
            c.execute('INSERT OR IGNORE INTO maintenance_check_reservations VALUES (?,?)',(repository,key))
            c.execute('INSERT OR IGNORE INTO maintenance_check_parents VALUES (?,?)',(key,parent_task_id))
            # The transaction reserves before execution; process loss is never permission to replay.
            c.execute("UPDATE maintenance_check_plans SET state='running' WHERE id=?",(key,))
        results=[];state='passed'
        for check in plan['selection']['checks']:
            try:
                with self.store._transaction() as c:
                    self._row(c,token,key)
                    if not self._core_guard(c,token,plan,repository,parent_task_id):
                        raise Conflict('another core task owns this repository')
                    current=self._current(c,token,plan['project'],host)
                    if current['fingerprint']!=plan['config_fingerprint'] or host.binding()!=plan['host_binding']:
                        raise Conflict('configuration changed during validation')
                result=host.executor(check['name'],tuple(check['argv']),host.checkout,host.timeout)
                if type(result) is not dict or set(result)!={'passed','timed_out','reference','duration_seconds'}:
                    raise ValueError('invalid executor result')
                if type(result['passed']) is not bool or type(result['timed_out']) is not bool:
                    raise ValueError('strict check outcome required')
                receipt(result['reference']);_reject_credentials(result['reference'],token)
                if type(result['duration_seconds']) not in (int,float) or not math.isfinite(result['duration_seconds']) or result['duration_seconds']<0:
                    raise ValueError('invalid timing')
                results.append({'name':check['name'],**result})
                if not result['passed'] or result['timed_out']:state='failed'
                with self.store._transaction() as c:
                    self._row(c,token,key)
                    if not self._core_guard(c,token,plan,repository,parent_task_id):
                        raise Conflict('another core task owns this repository')
                    current=self._current(c,token,plan['project'],host)
                    if current['fingerprint']!=plan['config_fingerprint'] or host.binding()!=plan['host_binding']:
                        raise Conflict('source changed during check')
            except Exception:
                state='uncertain';break
        output={'id':key,'project':plan['project'],'state':state,'checks':results,
                'source_sha':plan['source_sha'],'config_fingerprint':plan['config_fingerprint'],
                'plan_fingerprint':row['fingerprint'],'parent_task_id':parent_task_id,'completed_at':self.store._now(),
                'operational_change':False,'retry_performed':False}
        with self.store._transaction() as c:
            # Persist even after revoked access, but do not reveal the report to a revoked caller.
            c.execute('UPDATE maintenance_check_plans SET state=?,result=? WHERE id=?',
                      (state,canonical_json(output),key))
            if state in ('passed','failed'):
                c.execute('DELETE FROM maintenance_check_reservations WHERE plan_id=?',(key,))
        with self.store._transaction() as c:
            self._row(c,token,key)
        return output

    def get(self,token,key):
        with self.store._transaction() as c:
            row=self._row(c,token,key,('owner','manager'))
            return {'id':row['id'],'project':row['project'],'state':row['state'],
                    'fingerprint':row['fingerprint'],'plan':json.loads(row['plan']),
                    'result':json.loads(row['result']) if row['result'] else None}


@dataclass(frozen=True)
class DependencySnapshot:
    project:str
    source_sha:str
    manifest_digest:str
    lock_digest:str
    advisories:tuple
    reference:str
    observed_at:float

    def __post_init__(self):
        name(self.project);_sha(self.source_sha);receipt(self.reference)
        for value in (self.manifest_digest,self.lock_digest):
            if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('manifest and lock digests required')
        if type(self.advisories) is not tuple or len(self.advisories)>200:raise ValueError('bounded advisories required')
        for item in self.advisories:
            if type(item) is not dict or set(item)!={'package','current','proposed','severity','evidence'}:
                raise ValueError('exact advisory required')
            if type(item['package']) is not str or len(item['package'])>150 or not re.fullmatch(r'(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?',item['package']):
                raise ValueError('exact package identifier required')
            for key in ('current','proposed'):
                if type(item[key]) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+:-]{0,100}',item[key]):
                    raise ValueError('resolved package version required')
            if item['severity'] not in ('critical','high','normal','unknown'):raise ValueError('advisory severity required')
            receipt(item['evidence'])
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('observation time required')
        object.__setattr__(self,'advisories',tuple(dict(a) for a in self.advisories))


class DependencyReview:
    def __init__(self,store,registry,providers=None,*,max_age=3600):
        self.store,self.registry=store,registry
        self.providers=dict(providers or {})
        if type(max_age) is not int or not 1<=max_age<=86400:raise ValueError('bounded advisory age required')
        self.max_age=max_age
        with store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS maintenance_dependency_reports (project TEXT PRIMARY KEY, config_fingerprint TEXT NOT NULL, report TEXT NOT NULL, recorded REAL NOT NULL)')
        for key,value in self.providers.items():
            name(key)
            if not callable(value):raise ValueError('trusted dependency observer required')

    def inspect(self,token,project,source_sha):
        _sha(source_sha)
        config=self.registry.get(token,project)
        callback=self.providers.get(project)
        if callback is None:return {'project':project,'state':'not_measured','packages_changed':False}
        try:
            observed=callback(project,source_sha)
            if not isinstance(observed,DependencySnapshot) or observed.project!=project or observed.source_sha!=source_sha:
                raise ValueError('dependency scope mismatch')
            age=self.store._now()-observed.observed_at
            if not 0<=age<=self.max_age:raise ValueError('dependency evidence stale')
            _reject_credentials({'reference':observed.reference,'advisories':observed.advisories},token)
            result={'project':project,'state':'proposal' if observed.advisories else 'observed_no_findings',
                    'source_sha':source_sha,'config_fingerprint':config['fingerprint'],'observed_at':observed.observed_at,'manifest_digest':observed.manifest_digest,'lock_digest':observed.lock_digest,
                    'advisories':list(observed.advisories),'reference':observed.reference,
                    'packages_changed':False,'tests_passed':False,'approval_required':True}
        except Exception:
            result={'project':project,'state':'not_measured','packages_changed':False}
        current=self.registry.get(token,project)
        if current['fingerprint']!=config['fingerprint']:raise Conflict('project configuration changed')
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('project configuration changed')
            c.execute('INSERT OR REPLACE INTO maintenance_dependency_reports VALUES (?,?,?,?)',
                      (project,config['fingerprint'],canonical_json(result),self.store._now()))
        return result
