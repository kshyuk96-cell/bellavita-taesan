"""Bounded read-only receipts for official GitHub Codex replies.

This verifies review/receipt facts, not execution, sandbox readiness, or access to
other ChatGPT Cloud windows. A host supplies authenticated GitHub observations;
no request can select a callback, credential, repository, or raw instruction.
"""
from dataclasses import asdict, dataclass
import json
import math
import re
from .agent_directory import _reject_known_tokens
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, _reject_credentials, _sha

BOT='chatgpt-codex-connector[bot]'
_ENV_URL='https://chatgpt.com/codex/cloud/settings/environments'
_MARKER='<!-- codex-review-state:v1 '
_REPO=re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')


def _positive(value):
    if type(value) is not int or value<1 or value>2**63-1:raise ValueError('positive receipt identifier required')


def _clock(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('actual receipt clock required')


def _login(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}(?:\[bot\])?',value):
        raise ValueError('exact GitHub author required')


@dataclass(frozen=True)
class ReviewComment:
    id:int
    author:str
    body:str
    created_at:float

    def __post_init__(self):
        _positive(self.id);_login(self.author);_clock(self.created_at)
        if type(self.body) is not str or len(self.body.encode())>8192:raise ValueError('bounded comment required')


@dataclass(frozen=True)
class ReviewReaction:
    comment_id:int
    author:str
    content:str

    def __post_init__(self):
        _positive(self.comment_id);_login(self.author)
        if self.content not in {'eyes','+1','-1','heart','hooray','confused','laugh','rocket'}:raise ValueError('typed reaction required')


@dataclass(frozen=True)
class ReviewSnapshot:
    project:str
    repository:str
    branch:str
    pull_request:int
    head_sha:str
    comments:tuple
    reactions:tuple
    observed_at:float

    def __post_init__(self):
        name(self.project);_sha(self.head_sha);_positive(self.pull_request);_clock(self.observed_at)
        if type(self.repository) is not str or len(self.repository)>201 or not _REPO.fullmatch(self.repository):raise ValueError('exact GitHub repository required')
        if type(self.branch) is not str or not self.branch or len(self.branch)>128:raise ValueError('exact source branch required')
        if type(self.comments) is not tuple or len(self.comments)>100 or not all(isinstance(x,ReviewComment) for x in self.comments):
            raise ValueError('bounded typed comments required')
        if len({c.id for c in self.comments})!=len(self.comments):raise ValueError('duplicate comment receipt')
        if sum(len(x.body.encode()) for x in self.comments)>200000:raise ValueError('bounded total receipt size required')
        if type(self.reactions) is not tuple or len(self.reactions)>200 or not all(isinstance(x,ReviewReaction) for x in self.reactions):
            raise ValueError('bounded typed reactions required')


@dataclass(frozen=True)
class ReviewProbe:
    repository:str
    branch:str
    pull_request:int
    expected_head:str
    instruction_comment_id:int
    observe:object
    bot_login:str=BOT
    read_only:bool=True

    def __post_init__(self):
        if type(self.repository) is not str or len(self.repository)>201 or not _REPO.fullmatch(self.repository):raise ValueError('exact GitHub repository required')
        if type(self.branch) is not str or not self.branch or len(self.branch)>128:raise ValueError('source branch required')
        _positive(self.pull_request);_positive(self.instruction_comment_id);_sha(self.expected_head);_login(self.bot_login)
        if not callable(self.observe) or self.read_only is not True:raise ValueError('trusted read-only receipt probe required')


def _json_pairs(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('duplicate review state field')
        result[key]=value
    return result


def _review(body):
    # Marker is bounded by the comment contract. Do not search unbounded JSON or
    # infer results from model prose. Multiple/conflicting receipts are unknown.
    if body.count(_MARKER)!=1:return None
    start=body.index(_MARKER)+len(_MARKER);end=body.find(' -->',start)
    if end<0 or end-start>4096:return None
    try:
        value=json.loads(body[start:end],object_pairs_hook=_json_pairs,
                         parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid number')))
        if type(value) is not dict or 'code_review' not in value:return None
        review=value['code_review']
        if type(review) is not dict or set(review)!={'commit_sha','has_any_findings','has_unresolved_findings','status'}:return None
        _sha(review['commit_sha'])
        if review['status'] not in ('completed','in_progress','failed'):return None
        flags=(review['has_any_findings'],review['has_unresolved_findings'])
        if review['status']=='completed' and any(type(v) is not bool for v in flags):return None
        if review['status']!='completed' and any(v is not None and type(v) is not bool for v in flags):return None
        if review['has_unresolved_findings'] is True and review['has_any_findings'] is False:return None
        return review
    except (ValueError,TypeError,KeyError,RecursionError):return None


class CloudReviewReceipts:
    def __init__(self,store,registry,probes=None):
        if registry.store is not store:raise ValueError('shared registered store required')
        self.store,self.registry=store,registry;self.probes=dict(probes or {})
        for project,probe in self.probes.items():
            name(project)
            if not isinstance(probe,ReviewProbe):raise ValueError('typed receipt probe required')

    def inspect(self,token,project,*,max_age=300):
        name(project)
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('bounded receipt age required')
        before=self.registry.get(token,project);config=before['config']
        result={'project':project,'state':'not_configured','review_verified':False,
            'development_execution_verified':False,'sandbox_verified':False,
            'existing_cloud_windows_connected':False,'operational_change':False,
            'raw_comment_retained':False,'checked_at':self.store._now()}
        probe=self.probes.get(project)
        if probe is None:return result
        if config['source_kind']!='github' or (config['source_ref'],config['branch'])!=(probe.repository,probe.branch):
            raise Conflict('receipt probe source binding changed')
        error=None;observed=None
        try:observed=probe.observe(project)
        except PermissionError:error='permission_denied'
        except Exception:error='unavailable'
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=before['fingerprint']:raise Conflict('receipt configuration changed during collection')
            if error:return {**result,'state':error}
            if not isinstance(observed,ReviewSnapshot):return {**result,'state':'unknown'}
            if (observed.project,observed.repository,observed.branch,observed.pull_request)!=(project,probe.repository,probe.branch,probe.pull_request):
                return {**result,'state':'unknown'}
            if not 0<=self.store._now()-observed.observed_at<=max_age:return {**result,'state':'stale'}
            try:
                _reject_credentials(asdict(observed),token);_reject_known_tokens(c,asdict(observed))
            except ValueError:return {**result,'state':'unknown'}
        result.update(repository=probe.repository,pull_request=probe.pull_request,head_sha=observed.head_sha,observed_at=observed.observed_at)
        if observed.head_sha!=probe.expected_head:return {**result,'state':'head_changed'}
        if any(x.created_at>observed.observed_at for x in observed.comments):return {**result,'state':'unknown'}
        bot_comments=[x for x in observed.comments if x.author==probe.bot_login]
        summaries=[];malformed=False
        for comment in bot_comments:
            if _MARKER in comment.body:
                review=_review(comment.body)
                if review is None:malformed=True
                else:summaries.append((comment,review))
        if malformed:return {**result,'state':'unknown'}
        current=[(c,r) for c,r in summaries if r['commit_sha']==probe.expected_head]
        if current:
            # Same-second competing states are contradictory, not completion.
            latest_time=max(c.created_at for c,r in current)
            latest=[(c,r) for c,r in current if c.created_at==latest_time]
            if len({json.dumps(r,sort_keys=True) for c,r in latest})!=1:return {**result,'state':'unknown'}
            comment,review=latest[-1]
            state='review_completed' if review['status']=='completed' else 'review_in_progress' if review['status']=='in_progress' else 'review_failed'
            if state=='review_completed' and review['has_any_findings']:state='review_findings'
            return {**result,'state':state,'review_verified':review['status']=='completed',
                'has_any_findings':review['has_any_findings'],'has_unresolved_findings':review['has_unresolved_findings'],
                'receipt_comment_id':comment.id}
        if summaries:return {**result,'state':'review_for_other_head'}
        if any('create an environment for this repo' in x.body and _ENV_URL in x.body for x in bot_comments):
            return {**result,'state':'environment_required'}
        if any(x.comment_id==probe.instruction_comment_id and x.author==probe.bot_login and x.content=='eyes' for x in observed.reactions):
            return {**result,'state':'acknowledged'}
        if bot_comments:return {**result,'state':'execution_unverified'}
        if any(x.id==probe.instruction_comment_id for x in observed.comments):return {**result,'state':'queued'}
        return {**result,'state':'no_receipt'}
