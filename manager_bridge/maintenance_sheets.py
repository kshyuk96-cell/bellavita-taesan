"""Read-only sheet compatibility rehearsal. No Google requests or sheet writes.

Snapshot values/formulas remain ephemeral. Registered host observers must verify
read authority and propose the after snapshot against the same source version.
A compatible result is a pending approval package, never execution approval.
"""
from dataclasses import asdict, dataclass, field
import math
from .maintenance_registry import name, receipt
from .store import Conflict, _reject_credentials, canonical_json, digest


def _headers(value):
    if type(value) is not tuple or len(value)>300 or any(type(v) is not str or not v.strip() or len(v)>100 for v in value):
        raise ValueError('bounded explicit headers required')
    return value


@dataclass(frozen=True)
class SheetRow:
    key: str | None
    values: tuple = field(repr=False)
    formulas: tuple = field(default=(),repr=False)

    def __post_init__(self):
        if self.key is not None and (type(self.key) is not str or len(self.key)>128):raise ValueError('stable row identity required')
        if type(self.values) is not tuple or len(self.values)>300:raise ValueError('bounded row values required')
        for value in self.values:
            if value is not None and type(value) not in (str,int,float,bool):raise ValueError('scalar sheet values required')
            if type(value) is str and len(value)>2000:raise ValueError('bounded sheet value required')
            if type(value) is float and not math.isfinite(value):raise ValueError('finite sheet value required')
        if type(self.formulas) is not tuple or (self.formulas and len(self.formulas)!=len(self.values)):
            raise ValueError('paired formula cells required')
        if any(v is not None and (type(v) is not str or not v.startswith('=') or len(v)>2000) for v in self.formulas):
            raise ValueError('formula cells required')


@dataclass(frozen=True)
class SheetSnapshot:
    company: str
    project: str
    spreadsheet: str
    tab: str
    source_version: str
    headers: tuple
    rows: tuple = field(repr=False)
    reference: str
    observed_at: float

    def __post_init__(self):
        for value in (self.company,self.project,self.spreadsheet,self.tab):name(value)
        if type(self.source_version) is not str or len(self.source_version)>128:raise ValueError('source revision required')
        _headers(self.headers)
        if type(self.rows) is not tuple or len(self.rows)>10000 or any(not isinstance(r,SheetRow) or len(r.values)!=len(self.headers) for r in self.rows):
            raise ValueError('bounded shape-matched rows required')
        receipt(self.reference)
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:raise ValueError('read observation time required')


def compare_sheets(before,after,expected_headers):
    """Compare synthetic/proposed snapshots. Source row order may move; IDs may not."""
    if not isinstance(before,SheetSnapshot) or not isinstance(after,SheetSnapshot):raise ValueError('observed snapshots required')
    _headers(expected_headers)
    reasons=[]
    if not before.headers or not after.headers or not expected_headers:reasons.append('missing-schema-header')
    scope=lambda s:(s.company,s.project,s.spreadsheet,s.tab)
    if scope(before)!=scope(after):reasons.append('source-scope-mismatch')
    if not before.source_version.strip() or before.source_version!=after.source_version:reasons.append('source-version-mismatch')
    if len(set(expected_headers))!=len(expected_headers):reasons.append('duplicate-expected-header')
    if len(set(before.headers))!=len(before.headers) or len(set(after.headers))!=len(after.headers):reasons.append('duplicate-header')
    if after.headers!=expected_headers:reasons.append('unexpected-schema')
    if after.headers[:len(before.headers)]!=before.headers:reasons.append('protected-column-position-changed')
    keys=lambda s:[r.key for r in s.rows]
    for snapshot in (before,after):
        values=keys(snapshot)
        if any(not key or not key.strip() for key in values):reasons.append('missing-stable-row-identity')
        if len(set(values))!=len(values):reasons.append('duplicate-row-identity')
    if not reasons:
        old={r.key:r for r in before.rows};new={r.key:r for r in after.rows}
        if not old.keys()<=new.keys():reasons.append('original-row-missing')
        for key,row in old.items():
            if key not in new:continue
            candidate=new[key];n=len(before.headers)
            # Canonical JSON keeps boolean TRUE/FALSE distinct from 1/0 and blank.
            if canonical_json(row.values)!=canonical_json(candidate.values[:n]):reasons.append('protected-cell-value-changed')
            old_formula=row.formulas or (None,)*n
            new_formula=(candidate.formulas or (None,)*len(after.headers))[:n]
            if old_formula!=new_formula:reasons.append('protected-formula-changed')
    package={'before_digest':digest(asdict(before)),'after_digest':digest(asdict(after)),
             'expected_schema_digest':digest(expected_headers),'before_rows':len(before.rows),'after_rows':len(after.rows),
             'reasons':sorted(set(reasons)),'state':'review' if reasons else 'compatible',
             'observed_empty':not before.rows and not after.rows,'operational_change':False,
             'approval_state':'pending_approval','production_write_authorized':False}
    package['fingerprint']=digest(package)
    return package


@dataclass(frozen=True)
class SheetProvider:
    company: str
    spreadsheet: str
    tab: str
    expected_headers: tuple
    observe: object

    def __post_init__(self):
        for value in (self.company,self.spreadsheet,self.tab):name(value)
        _headers(self.expected_headers)
        if len(set(self.expected_headers))!=len(self.expected_headers) or not callable(self.observe):raise ValueError('trusted sheet provider required')


class SheetChecks:
    def __init__(self,store,registry,providers=None,*,max_age=300):
        if registry.store is not store:raise ValueError('shared project store required')
        self.store,self.registry=store,registry
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('bounded observation age required')
        self.max_age=max_age;self.providers=dict(providers or {})
        for key,provider in self.providers.items():
            if type(key) is not tuple or len(key)!=2 or not isinstance(provider,SheetProvider):raise ValueError('project-bound provider required')
            name(key[0]);name(key[1])

    def compare(self,token,project,binding_id):
        name(binding_id);_reject_credentials(binding_id,token)
        config=self.registry.get(token,project)
        provider=self.providers.get((project,binding_id))
        if provider is None:return {'project':project,'state':'not_configured','operational_change':False}
        if provider.company!=config['config']['company']:
            return {'project':project,'state':'permission_denied','operational_change':False}
        snapshots=[]
        for phase in ('before','after'):
            current=self.registry.get(token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('project configuration changed')
            error='unknown'
            try:
                value=provider.observe(project,binding_id,phase)
            except PermissionError:
                value=None;error='permission_denied'
            except Exception:
                value=None;error='unavailable'
            # Authenticate even when a provider raises; do not return to a revoked caller.
            current=self.registry.get(token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('project configuration changed')
            if value is None:return {'project':project,'state':error,'operational_change':False}
            if not isinstance(value,SheetSnapshot):return {'project':project,'state':'unknown','operational_change':False}
            if (value.company,value.project,value.spreadsheet,value.tab)!=(provider.company,project,provider.spreadsheet,provider.tab):
                return {'project':project,'state':'permission_denied','operational_change':False}
            _reject_credentials(value.reference,token)
            if not 0<=self.store._now()-value.observed_at<=self.max_age:
                return {'project':project,'state':'stale','operational_change':False}
            snapshots.append(value)
        result=compare_sheets(*snapshots,provider.expected_headers)
        result.update(project=project,config_fingerprint=config['fingerprint'])
        result['fingerprint']=digest(result)
        return result
