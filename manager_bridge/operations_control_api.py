"""Strict POST operations routes within the existing authenticated control API."""
from urllib.parse import urlsplit
from .operations_control import OperationsControl
from .store import NotFound

PREFIX='/v1/representative/runtime/governance/control/operations/'
ROUTES={
    'diagnostics':('diagnostics',('projects',),()),
    'workflow/trace':('workflow',('task_id',),('limit',)),
    'preflight':('inspect',('projects',),()),
    'workers':('workers',('projects',),()),
    'quality/baseline/draft':('baseline_draft',('project','request_key','contract'),()),
    'quality/baseline/current':('baseline_current',('project',),()),
    'quality/baseline/approve':('baseline_approve',('draft_id','draft_digest'),('expected_revision',)),
    'quality/report':('quality_delivery',('projects',),('limit','cursor','freshness_limit')),
    'quality/handoff':('handoff_packet',('task_id','role'),('candidate_digest',)),
    'host/report':('host_report',('projects',),()),
    'host/install':('install_host',('project','requirement'),('expected_revision',)),
    'host/refresh':('refresh_host',('project',),()),
    'recovery':('recovery_report',('projects',),('limit',)),
    'transition/status':('transition_status',('projects',),()),
    'transition/drain':('drain',('projects','expected_revisions','request_key'),('reason',)),
    'transition/resume':('resume',('projects','expected_revisions','request_key'),()),
    'links/inventory':('link_inventory',('projects',),()),
    'links/plan':('create_link_plan',('request_key','projects','expected_bindings','role_mappings','expected_host_map_digest','broker_refs'),()),
    'links/get':('get_link_plan',('plan_id',),()),
    'backup/report':('backup_report',(),('verify_latest',)),
    'backup/policy':('install_backup_policy',('policy',),('expected_revision',)),
    'backup/rehearse':('rehearse_backup',('backup_id','request_key'),()),
    'readiness':('readiness_report',('projects',),()),
    'exceptions':('exception_report',('projects','request_key'),('after','limit')),
    'projects/register':('register_projects',('request_key','registrations'),()),
    'owner-packet':('owner_packet',('request_key','projects'),('expected_fingerprints',)),
    'report':('report',('request_key','projects'),('after','limit')),
    'batches':('batches',('projects',),('cursor','limit')),
    'budget/report':('budget_report',('projects',),()),
    'budget/install':('install_budget',('project','policy'),('expected_revision',)),
    'budget/auth':('refresh_auth',('project',),()),
    'budget/reconcile':('reconcile_budget',('project','reservation_id'),()),
    'artifacts/register':('register_artifact',('project','kind','artifact_key'),('retention_seconds',)),
    'artifacts/get':('artifact',('reference',),()),
}


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative operations route required')
    if not url.path.startswith(PREFIX):return False
    route=url.path[len(PREFIX):]
    if method!='POST' or route not in ROUTES:return False
    if url.query:raise ValueError('POST query rejected')
    action,required,optional=ROUTES[route];args=_fields(body,required,optional)
    store=handler.server.store
    with store._transaction() as c:
        actor=store._authenticate(c,token)
        store._role(actor,('owner',) if route in ('budget/install','host/install','projects/register','transition/drain','transition/resume','links/inventory','links/plan','links/get','backup/report','backup/policy','backup/rehearse','quality/baseline/approve') else ('owner','manager'))
    operations=getattr(handler.server,'operations_control',None)
    if operations is None:raise NotFound('operations control not configured')
    if not isinstance(operations,OperationsControl) or operations.store is not store:raise ValueError('registered operations control required')
    result=getattr(operations,action)(token,**args)
    handler.respond(200,{'ok':True,'result':result})
    return True
