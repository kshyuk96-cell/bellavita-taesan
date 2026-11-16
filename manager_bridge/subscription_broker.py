"""Host-only managed subscription credential rotation; never API keys or login UI.

All transports using a target profile must hold ``profile_guard`` for metadata
and model execution. A crash or partial copy quarantines the entire generation;
this module deliberately has no automatic recovery/replay or rollback to old
refresh tokens. Raw auth bytes remain only in private host files and memory.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, canonical_json, digest, _reject_credentials
from .three_tier import ThreeTier

MAX_AUTH_BYTES=65536
_CREDENTIAL_FIELDS=('access_token','refresh_token','id_token','account_id')


def _credential_strings(tokens):
    # SDK metadata may contain nested dictionaries/lists and ordinary words.
    # Only the schema's actual opaque credentials are secret-string sentinels;
    # the original auth bytes, including unknown metadata, are copied unchanged.
    return tuple(tokens[key] for key in _CREDENTIAL_FIELDS)


def _physical(value, directory=True):
    path=Path(value).absolute()
    # Linux treats multiple leading slashes as the same root.
    path=Path('/'+str(path).lstrip('/'))
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('physical private subscription path required')
    info=path.stat()
    if info.st_uid!=os.getuid() or info.st_mode&0o077 or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('owned private subscription path required')
    if not directory and info.st_nlink!=1:raise ValueError('unshared private subscription file required')
    return path


def _home_digest(home):
    path=_physical(home);info=path.stat()
    return digest({'device':info.st_dev,'inode':info.st_ino})


def _read(home, *, missing=False):
    home=_physical(home);path=home/'auth.json'
    if path.is_symlink():raise ValueError('physical private subscription path required')
    if missing and not path.exists():return None
    _physical(path,False)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if info.st_size>MAX_AUTH_BYTES or info.st_nlink!=1 or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('bounded private subscription auth required')
        data=stream.read(MAX_AUTH_BYTES+1)
    if len(data)>MAX_AUTH_BYTES:raise ValueError('bounded private subscription auth required')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate subscription auth field')
            result[key]=value
        return result
    try:value=json.loads(data,object_pairs_hook=pairs)
    except (ValueError,UnicodeError):raise ValueError('managed subscription auth required') from None
    tokens=value.get('tokens') if type(value) is dict else None
    if (type(value) is not dict or value.get('auth_mode')!='chatgpt' or value.get('OPENAI_API_KEY')
            or type(tokens) is not dict or any(type(tokens.get(k)) is not str or not tokens[k] for k in _CREDENTIAL_FIELDS)):
        raise ValueError('managed subscription auth required; external/API-key modes unsupported')
    return data, hashlib.sha256(data).hexdigest(), hashlib.sha256(tokens['account_id'].encode()).hexdigest()


def _atomic(home,data,expected):
    home=_physical(home)
    before=_read(home,missing=True)
    if (before[1] if before else None)!=expected:raise Conflict('target subscription auth changed')
    fd,path=tempfile.mkstemp(prefix='.taesan-auth-',dir=home)
    try:
        with os.fdopen(fd,'wb') as out:
            os.fchmod(out.fileno(),0o600);out.write(data);out.flush();os.fsync(out.fileno())
        current=_read(home,missing=True)
        if (current[1] if current else None)!=expected:raise Conflict('target subscription auth changed')
        os.replace(path,home/'auth.json')
        directory=os.open(home,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(path):os.unlink(path)


@dataclass(frozen=True)
class SubscriptionTarget:
    project:str
    member_id:str
    role:str
    home:Path
    def __post_init__(self):
        name(self.project);name(self.member_id)
        if self.role not in ('developer','reviewer'):raise ValueError('registered coding role required')
        object.__setattr__(self,'home',_physical(self.home))


class CodexSubscriptionRefresher:
    """Registered native metadata-only operation, using the installed app-server.

    ``host`` must be a dedicated CodexHost whose home is the canonical profile.
    No thread creation, turn/start, API-key login, or new browser login exists.
    """
    def __init__(self,host,project):
        from .fleet_codex import CodexHost
        if not isinstance(host,CodexHost):raise ValueError('registered Codex host required')
        host._binding(project);self.host,self.project=host,project
    def __call__(self,canonical_home):
        from .fleet_codex import _Rpc
        if _physical(canonical_home)!=self.host.home:raise ValueError('canonical subscription host required')
        _read(canonical_home)
        with _Rpc(self.host,self.project) as rpc:
            value=rpc.request('account/read',{'refreshToken':True})
            account=value.get('account') if type(value) is dict else None
            if type(account) is not dict or account.get('type')!='chatgpt':
                raise Conflict('managed subscription unavailable')
        return {'state':'authenticated','account_type':'chatgpt'}


class SubscriptionBroker:
    def __init__(self,core,broker_ref,canonical_home,targets,*,refresh_callback=None):
        if not isinstance(core,ThreeTier):raise ValueError('registered hierarchy core required')
        name(broker_ref)
        if type(targets) not in (tuple,list) or not 1<=len(targets)<=200 or any(type(t) is not SubscriptionTarget for t in targets):
            raise ValueError('bounded explicit subscription targets required')
        if len({(t.project,t.member_id) for t in targets})!=len(targets):raise ValueError('unique registered targets required')
        if refresh_callback is not None and not callable(refresh_callback):raise ValueError('trusted native refresh callback required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.ref,self.home,self.targets,self.refresh_callback=broker_ref,_physical(canonical_home),tuple(targets),refresh_callback
        homes=[self.home,*[t.home for t in targets]]
        if len({_home_digest(h) for h in homes})!=len(homes) or any(a==b or a in b.parents or b in a.parents for i,a in enumerate(homes) for b in homes[i+1:]):
            raise ValueError('distinct non-overlapping subscription profiles required')
        if any(h==self.store.path or h in self.store.path.parents for h in homes):raise ValueError('control database outside subscription profiles required')
        self.lock_path=self.home/'.taesan-subscription.lock'
        with self._lock(True):
            auth=_read(self.home)
            snapshots=self._targets(auth[2])
            binding=digest({'canonical':str(self.home),'targets':[(t.project,t.member_id,t.role,str(t.home)) for t in self.targets]})
            self.binding=binding
            with self.store._transaction() as c:
                from .agent_directory import _reject_known_tokens
                _reject_known_tokens(c,{'broker_ref':self.ref,'canonical_home':str(self.home),'targets':[(t.project,t.member_id,str(t.home)) for t in self.targets]})
                _reject_credentials(self.ref,*_credential_strings(json.loads(auth[0])['tokens']))
                c.execute('CREATE TABLE IF NOT EXISTS subscription_broker_state (ref TEXT PRIMARY KEY,home_digest TEXT UNIQUE,binding TEXT,generation INTEGER,state TEXT,canonical_digest TEXT,target_digests TEXT,updated REAL)')
                c.execute('CREATE TABLE IF NOT EXISTS subscription_broker_requests (broker_ref TEXT,request_key TEXT,actor_hash TEXT,expected_generation INTEGER,receipt TEXT,PRIMARY KEY(broker_ref,request_key))')
                c.execute('CREATE TABLE IF NOT EXISTS subscription_profile_owners (home_digest TEXT PRIMARY KEY,broker_ref TEXT NOT NULL,profile_key TEXT NOT NULL,UNIQUE(broker_ref,profile_key))')
                # The old schema retained only hashes, not enough to recover
                # another broker's path ownership safely. Never silently choose
                # a winner while any unknown legacy registration exists.
                for existing in c.execute('SELECT ref,target_digests FROM subscription_broker_state WHERE ref!=?',(self.ref,)):
                    count=c.execute('SELECT count(*) FROM subscription_profile_owners WHERE broker_ref=?',(existing['ref'],)).fetchone()[0]
                    if count!=1+len(json.loads(existing['target_digests'])):
                        raise Conflict('legacy subscription profile ownership requires explicit migration')
                # Preflight every role before state insertion, including an
                # existing broker's canonical home used as our target or reverse.
                for home,profile in [(_home_digest(self.home),'canonical'),*[(_home_digest(t.home),t.project+':'+t.member_id) for t in self.targets]]:
                    owner=c.execute('SELECT broker_ref,profile_key FROM subscription_profile_owners WHERE home_digest=?',(home,)).fetchone()
                    if owner and (owner['broker_ref'],owner['profile_key'])!=(self.ref,profile):raise Conflict('subscription profile already belongs to another broker')
                old=c.execute('SELECT * FROM subscription_broker_state WHERE ref=?',(self.ref,)).fetchone()
                if old:
                    if old['binding']!=binding:raise Conflict('subscription target binding changed')
                else:c.execute('INSERT INTO subscription_broker_state VALUES (?,?,?,0,?,?,?,?)',(self.ref,digest(str(self.home)),binding,'unverified',auth[1],canonical_json(snapshots),self.store._now()))
                claims=[(_home_digest(self.home),'canonical'),*[(_home_digest(t.home),t.project+':'+t.member_id) for t in self.targets]]
                current_claims={(r['home_digest'],r['profile_key']) for r in c.execute('SELECT home_digest,profile_key FROM subscription_profile_owners WHERE broker_ref=?',(self.ref,))}
                if current_claims and current_claims!=set(claims):raise Conflict('legacy subscription profile ownership requires explicit migration')
                for home,profile in claims:
                    owner=c.execute('SELECT broker_ref,profile_key FROM subscription_profile_owners WHERE home_digest=?',(home,)).fetchone()
                    if owner and (owner['broker_ref'],owner['profile_key'])!=(self.ref,profile):
                        raise Conflict('subscription profile already belongs to another broker')
                for home,profile in claims:
                    c.execute('INSERT OR IGNORE INTO subscription_profile_owners VALUES (?,?,?)',(home,self.ref,profile))

    @contextmanager
    def _lock(self,exclusive):
        _physical(self.home)
        fd=os.open(self.lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1 or not stat.S_ISREG(info.st_mode):raise ValueError('private subscription lock required')
            try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
            except BlockingIOError:raise Conflict('subscription profiles busy') from None
            yield
        finally:os.close(fd)

    def _targets(self,identity):
        result={}
        for target in self.targets:
            value=_read(target.home,missing=True)
            if value and value[2]!=identity:raise Conflict('subscription account binding changed')
            result[target.project+':'+target.member_id]=value[1] if value else None
        return result

    @staticmethod
    def _quarantined(c):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='governance_restore_marker'").fetchone() and c.execute('SELECT 1 FROM governance_restore_marker WHERE reconciliation_required=1 LIMIT 1').fetchone())

    def _auth(self,c,token,*,owner=False,idle=False):
        if self._quarantined(c):raise Conflict('restored subscription authority requires verification')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner',) if owner else ('owner','manager','worker'))
        bindings=[]
        for t in self.targets:
            config=self.core._config(c,token,t.project,('owner',) if owner else ('owner','manager','worker'))
            member=self.core._member(c,t.project,t.member_id,t.role,config)
            if idle and c.execute("SELECT 1 FROM hierarchy_tasks WHERE project=? AND state!='accepted' AND "+('developer_id=?' if t.role=='developer' else 'reviewer_id=?')+' LIMIT 1',(t.project,t.member_id)).fetchone():
                raise Conflict('registered subscription target in use')
            bindings.append((t.project,t.member_id,member['credential_hash'],member['subject'],config['fingerprint']))
        return actor,bindings

    def _public(self,c,value):
        from .agent_directory import _reject_known_tokens
        _reject_known_tokens(c,value)
        auth=json.loads(_read(self.home)[0])
        _reject_credentials(value,*_credential_strings(auth['tokens']))
        return value

    def _state(self,c):
        row=c.execute('SELECT * FROM subscription_broker_state WHERE ref=?',(self.ref,)).fetchone()
        if not row or row['binding']!=self.binding:raise Conflict('subscription broker binding changed')
        expected={(_home_digest(self.home),'canonical'),*[(_home_digest(t.home),t.project+':'+t.member_id) for t in self.targets]}
        actual={(r['home_digest'],r['profile_key']) for r in c.execute('SELECT home_digest,profile_key FROM subscription_profile_owners WHERE broker_ref=?',(self.ref,))}
        if actual!=expected:raise Conflict('subscription profile ownership changed')
        return row

    def status(self,token):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            for target in self.targets:self.registry._get(c,token,target.project)
            row=self._state(c)
            return self._public(c,{'broker_ref':self.ref,'generation':row['generation'],'state':row['state'],
                    'target_count':len(self.targets),'recovery_quarantined':self._quarantined(c),'api_key_fallback':False,'models_started':0})

    @contextmanager
    def profile_guard(self,token,project,member_id):
        matches=[t for t in self.targets if (t.project,t.member_id)==(project,member_id)]
        if len(matches)!=1:raise PermissionDenied('subscription target not registered')
        with self._lock(False):
            with self.store._transaction() as c:
                if self._quarantined(c):raise Conflict('restored subscription authority requires verification')
                actor=self.store._authenticate(c,token)
                self.store._role(actor,('owner','manager','worker'))
                config=self.core._config(c,token,project)
                member=self.core._member(c,project,member_id,matches[0].role,config)
                if actor['role']=='worker' and actor['token_hash']!=member['credential_hash']:raise PermissionDenied('assigned subscription profile required')
                row=self._state(c)
                if row['state']!='ready':raise Conflict('subscription generation requires reconciliation')
                canonical=_read(self.home);targets=self._targets(canonical[2])
                if canonical[1]!=row['canonical_digest'] or targets!=json.loads(row['target_digests']):raise Conflict('subscription files changed outside broker')
                initial=(config['fingerprint'],member['credential_hash'],member['subject'])
                result=self._public(c,{'broker_ref':self.ref,'generation':row['generation'],'project':project,'role':matches[0].role})
            try:
                yield result
            finally:
                try:
                    with self.store._transaction() as c:
                        if self._quarantined(c):raise Conflict('restored subscription authority requires verification')
                        actor=self.store._authenticate(c,token)
                        self.store._role(actor,('owner','manager','worker'))
                        current=self.core._config(c,token,project)
                        fresh=self.core._member(c,project,member_id,matches[0].role,current)
                        if actor['role']=='worker' and actor['token_hash']!=fresh['credential_hash']:raise PermissionDenied('assigned subscription profile required')
                        if (current['fingerprint'],fresh['credential_hash'],fresh['subject'])!=initial:raise Conflict('subscription authority changed during use')
                        latest=self._state(c)
                        canonical=_read(self.home)
                        if latest['state']!='ready' or latest['generation']!=result['generation'] or canonical[1]!=latest['canonical_digest'] or self._targets(canonical[2])!=json.loads(latest['target_digests']):raise Conflict('subscription changed during use; explicit reconciliation required')
                except BaseException:
                    with self.store._transaction() as c:c.execute("UPDATE subscription_broker_state SET state='uncertain',updated=? WHERE ref=?",(self.store._now(),self.ref))
                    raise

    def refresh_and_distribute(self,owner,expected_generation,request_key):
        name(request_key)
        if type(expected_generation) is not int or expected_generation<0:raise ValueError('exact subscription generation required')
        with self._lock(True):
            with self.store._transaction() as c:
                actor,bindings=self._auth(c,owner,owner=True,idle=True);row=self._state(c)
                from .agent_directory import _reject_known_tokens
                _reject_known_tokens(c,{'request_key':request_key})
                _reject_credentials(request_key,*_credential_strings(json.loads(_read(self.home)[0])['tokens']))
                old=c.execute('SELECT * FROM subscription_broker_requests WHERE broker_ref=? AND request_key=?',(self.ref,request_key)).fetchone()
                if old:
                    if old['actor_hash']!=actor['token_hash'] or old['expected_generation']!=expected_generation:raise Conflict('subscription request changed')
                    if row['state']!='ready' or row['generation']!=json.loads(old['receipt'])['generation']:raise Conflict('subscription replay superseded or uncertain')
                    canonical=_read(self.home)
                    if canonical[1]!=row['canonical_digest'] or self._targets(canonical[2])!=json.loads(row['target_digests']):raise Conflict('subscription files changed outside broker')
                    return self._public(c,json.loads(old['receipt']))
                if row['generation']!=expected_generation or row['state'] not in ('unverified','ready'):raise Conflict('subscription generation changed or requires reconciliation')
                before=_read(self.home);target_before=self._targets(before[2])
                if before[1]!=row['canonical_digest'] or target_before!=json.loads(row['target_digests']):raise Conflict('subscription files changed outside broker')
                if self.refresh_callback is None:raise Conflict('native subscription refresh not configured')
                c.execute("UPDATE subscription_broker_state SET state='refreshing',updated=? WHERE ref=?",(self.store._now(),self.ref))
            try:
                try:observed=self.refresh_callback(self.home)
                except Exception:raise Conflict('native subscription refresh unavailable') from None
                if observed!={'state':'authenticated','account_type':'chatgpt'}:raise Conflict('managed subscription refresh not verified')
                after=_read(self.home)
                if after[2]!=before[2]:raise Conflict('subscription account changed during refresh')
                with self.store._transaction() as c:
                    _,current=self._auth(c,owner,owner=True,idle=True)
                    if current!=bindings:raise Conflict('subscription authority changed during refresh')
                    if self._state(c)['state']!='refreshing':raise Conflict('subscription generation changed')
                    if self._targets(before[2])!=target_before:raise Conflict('target subscription auth changed during refresh')
                    c.execute("UPDATE subscription_broker_state SET state='distributing',updated=? WHERE ref=?",(self.store._now(),self.ref))
                for t in self.targets:
                    # The IMMEDIATE transaction fences concurrent role revocation
                    # through this local atomic replacement; no callback runs here.
                    with self.store._transaction() as c:
                        _,current=self._auth(c,owner,owner=True,idle=True)
                        if current!=bindings or self._state(c)['state']!='distributing':raise Conflict('subscription authority changed before profile distribution')
                        if _read(self.home)[1]!=after[1]:raise Conflict('canonical subscription auth changed during distribution')
                        _atomic(t.home,after[0],target_before[t.project+':'+t.member_id])
                with self.store._transaction() as c:
                    _,current=self._auth(c,owner,owner=True,idle=True)
                    if current!=bindings:raise Conflict('subscription authority changed during distribution')
                    final=_read(self.home);targets=self._targets(before[2])
                    if final[1]!=after[1] or any(value!=after[1] for value in targets.values()):raise Conflict('subscription files changed during distribution')
                    generation=expected_generation+1
                    receipt={'broker_ref':self.ref,'generation':generation,'state':'ready','profiles_updated':len(self.targets),
                             'refresh_requested':True,'auth_file_changed':before[1]!=after[1],
                             'api_key_fallback':False,'models_started':0,'observed_at':self.store._now()}
                    self._public(c,receipt)
                    c.execute("UPDATE subscription_broker_state SET generation=?,state='ready',canonical_digest=?,target_digests=?,updated=? WHERE ref=?",(generation,after[1],canonical_json(targets),self.store._now(),self.ref))
                    c.execute('INSERT INTO subscription_broker_requests VALUES (?,?,?,?,?)',(self.ref,request_key,actor['token_hash'],expected_generation,canonical_json(receipt)))
                return receipt
            except BaseException:
                with self.store._transaction() as c:c.execute("UPDATE subscription_broker_state SET state='uncertain',updated=? WHERE ref=?",(self.store._now(),self.ref))
                raise
