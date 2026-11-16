"""Versioned external tester claims, never execution or admission authority.

This adapter parses bounded digest-only references. The calling server owns
scope/authentication and the expected binding; external success claims cannot
change hierarchy acceptance or replace an independent trusted review.
"""
from dataclasses import asdict,dataclass
import hashlib
import json
import re
from .store import canonical_json,digest

SCHEMA_VERSION='external-tester-result/v1'
RECEIPT_SCHEMA='external-tester-receipt/v1'
CLASSIFICATION='externally_reported_unverified'
MAX_BYTES=64*1024
MAX_CASES=128
MAX_FORMULAS=128
STATUSES=frozenset(('pass','fail','blocked','unmeasured'))
COUNTS=frozenset(('total',*STATUSES))
_DIGEST=re.compile('[a-f0-9]{64}')
_SECRET=re.compile(r'(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|(?:AKIA|ASIA)[A-Z0-9]{16}|bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)',re.IGNORECASE)


def _hash(value):
    if type(value) is not str or not _DIGEST.fullmatch(value):raise ValueError('exact external reference digest required')


@dataclass(frozen=True)
class ExternalTesterBinding:
    project:str
    task_id:str
    task_version:int
    source_revision:str
    manifest_digest:str
    policy_digest:str
    policy_version:str
    config_fingerprint:str
    candidate_digest:str
    qa_contract_digest:str

    def __post_init__(self):
        if type(self.project) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',self.project):
            raise ValueError('exact project binding required')
        if type(self.task_id) is not str or not re.fullmatch('[a-f0-9]{32}',self.task_id):raise ValueError('exact task binding required')
        if type(self.task_version) is not int or not 1<=self.task_version<=2147483647:raise ValueError('exact task version required')
        if type(self.source_revision) is not str or not re.fullmatch('[a-f0-9]{40}',self.source_revision):raise ValueError('exact source binding required')
        if type(self.policy_version) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',self.policy_version):raise ValueError('exact policy version required')
        for value in (self.manifest_digest,self.policy_digest,self.config_fingerprint,self.candidate_digest,self.qa_contract_digest):_hash(value)
        if _SECRET.search(self.project) or _SECRET.search(self.policy_version):raise ValueError('credentials forbidden in external references')


def _bounded_tree(value):
    stack=[(value,0)];nodes=0;strings=[]
    while stack:
        item,depth=stack.pop();nodes+=1
        if depth>8 or nodes>2048:raise ValueError('external reference structure too large')
        if type(item) is dict:
            strings.extend(item.keys());stack.extend((child,depth+1) for child in item.values())
        elif type(item) is list:stack.extend((child,depth+1) for child in item)
        elif type(item) is str:strings.append(item)
        elif type(item) is not int:raise ValueError('unsupported external reference value')
    return strings


def _counts(value,rows,limit):
    if type(value) is not dict or set(value)!=COUNTS:raise ValueError('exact external result counts required')
    if any(type(count) is not int or not 0<=count<=limit for count in value.values()):raise ValueError('bounded integer external counts required')
    if value['total']!=len(rows) or sum(value[s] for s in STATUSES)!=value['total']:
        raise ValueError('external count total mismatch')
    if any(value[s]!=sum(row['status']==s for row in rows) for s in STATUSES):raise ValueError('external status counts mismatch')


def _rows(value,key,limit):
    if type(value) is not list or len(value)>limit:raise ValueError('bounded external references required')
    seen=set()
    for row in value:
        if type(row) is not dict or set(row)!={key,'status','evidence_digest'}:raise ValueError('digest-only external result reference required')
        _hash(row[key]);_hash(row['evidence_digest'])
        if type(row['status']) is not str or row['status'] not in STATUSES:raise ValueError('unknown external result status')
        if row[key] in seen:raise ValueError('duplicate external result reference')
        seen.add(row[key])


@dataclass(frozen=True)
class ExternalTesterReceipt:
    """Detached reference evidence; classification and authority are fixed locally."""
    payload_json:str
    transport_digest:str

    @property
    def payload(self):return json.loads(self.payload_json)
    @property
    def classification(self):return CLASSIFICATION
    @property
    def technical_accepted(self):return False
    @property
    def admission_authority(self):return False
    @property
    def receipt_digest(self):return digest(self.payload)

    def reference_evidence(self):
        value=self.payload
        return dict(schema_version=RECEIPT_SCHEMA,classification=CLASSIFICATION,
                    receipt_digest=self.receipt_digest,transport_digest=self.transport_digest,
                    binding=value['binding'],binding_digest=digest(value['binding']),
                    tester_digest=value['tester_digest'],run_digest=value['run_digest'],report_digest=value['report_digest'],
                    claimed_counts=value['counts'],claimed_formula_counts=value['formula_counts'],
                    technical_accepted=False,admission_authority=False,operational_authorization=False)


def parse_external_tester_result(payload,*,expected_binding,known_secrets=()):
    """Validate an external claim against independently obtained manager bindings.

    Accept raw UTF-8 bytes/text, preserving duplicate-key and byte-budget checks.
    Pass relevant live credentials through known_secrets before recording. This
    parser performs no authentication, artifact retrieval, tests or admission.
    """
    if type(expected_binding) is not ExternalTesterBinding:raise ValueError('trusted manager-owned expected binding required')
    if type(known_secrets) not in (tuple,list) or len(known_secrets)>256 or any(type(s) is not str or not 1<=len(s)<=4096 for s in known_secrets):
        raise ValueError('bounded known credential rejection list required')
    if type(payload) not in (str,bytes):raise ValueError('raw external JSON bytes or text required')
    try:
        raw=payload.encode('utf-8') if type(payload) is str else payload
        if len(raw)>MAX_BYTES:raise ValueError('external tester result too large')
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError('duplicate external JSON key')
                result[key]=value
            return result
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
                         parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite external value')))
    except (UnicodeError,RecursionError,json.JSONDecodeError) as exc:
        raise ValueError('malformed external tester JSON') from exc
    strings=_bounded_tree(value)
    if any(_SECRET.search(text) or any(secret in text for secret in known_secrets) for text in strings):
        raise ValueError('credentials forbidden in external references')
    fields={'schema_version','binding','tester_digest','run_digest','report_digest','counts','cases','formula_counts','formulas'}
    if type(value) is not dict or set(value)!=fields or value['schema_version']!=SCHEMA_VERSION:raise ValueError('unsupported external tester result schema')
    if type(value['binding']) is not dict or value['binding']!=asdict(expected_binding):raise ValueError('external tester binding mismatch')
    # Exact equality alone accepts True==1; reconstruct to enforce strict types.
    supplied=ExternalTesterBinding(**value['binding'])
    if asdict(supplied)!=asdict(expected_binding):raise ValueError('external tester binding mismatch')
    for key in ('tester_digest','run_digest','report_digest'):_hash(value[key])
    _rows(value['cases'],'case_digest',MAX_CASES);_rows(value['formulas'],'formula_digest',MAX_FORMULAS)
    _counts(value['counts'],value['cases'],MAX_CASES);_counts(value['formula_counts'],value['formulas'],MAX_FORMULAS)
    return ExternalTesterReceipt(canonical_json(value),hashlib.sha256(raw).hexdigest())
