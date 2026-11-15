"""Read-only bridge from the existing conversation core to project adapters.

Tokens are request-local server credentials, never tool arguments. A reader
Principal represents the freshly authenticated manager scope, not a newly
provisioned customer employee or authority to modify orders/payments.
"""
from dataclasses import dataclass
import datetime as dt
import json

from taesan_core.core import AccessDenied, CoreError, Evidence, Field, Principal, Registry, Result, Tool, digest, encoded
from taesan_core.commerce import Companies, Company
from .project_adapters import ERP_STATES, SHOP_STATES, STAGES, SOURCE_COMMITS, ProjectAdapters
from .store import Conflict, PermissionDenied


PROJECT_COMPANIES = {
    'bellavita-erp': ('bellavita', '벨라비타'),
    'bellavita-shop': ('bellavita', '벨라비타'),
    'hope-sunrise': ('hope-sunrise', '희망일출'),
    'funeral-association': ('funeral-association', '장례지도사협회'),
}
TOOL_ACTIONS = {
    'bellavita-erp': {'erp.orders': 'orders.list'},
    'bellavita-shop': {'shop.orders': 'orders.list'},
    'hope-sunrise': {'hope.products': 'products.list', 'hope.inquiries': 'inquiries.list'},
    'funeral-association': {'association.access': 'access.status', 'association.obituary': 'obituary.get'},
}


def read_companies():
    """Reuse company contracts only for these registered read tools.

    This local registry does not change any commerce company configuration or
    enable an order, payment, mutation, employee session, or live connector.
    """
    companies = Companies()
    for tenant, label in sorted(set(PROJECT_COMPANIES.values())):
        companies.register(Company(tenant, label, enabled=True))
    return companies


def principal_for_token(store, registry, token, project):
    """Freshly resolve one exact manager project to a deliberately reader-only identity."""
    if registry.store is not store or project not in PROJECT_COMPANIES:
        raise ValueError('registered conversation project required')
    with store._transaction() as c:
        configuration = registry._get(c, token, project)
        actor = store._authenticate(c, token)
        company = configuration['config']['company']
        if company != PROJECT_COMPANIES[project][0]:
            raise Conflict('conversation company binding changed')
        principal = Principal(company, digest({'actor':actor['token_hash'],'project':project}), frozenset({'reader'}))
    read_companies().for_principal(principal)
    return principal


@dataclass(frozen=True)
class QueryField(Field):
    """Keep provider query bounds while using the existing core field contract."""
    query_kind: str = 'text'

    def validate(self, value):
        value = super().validate(value)
        if self.query_kind == 'id' and (len(value)>150 or '\x00' in value):
            raise CoreError('bounded business reference required')
        if self.query_kind == 'date':
            try:
                if dt.date.fromisoformat(value).isoformat() != value:
                    raise ValueError('noncanonical date')
            except (ValueError, TypeError):
                raise CoreError('ISO business date required') from None
        if self.query_kind == 'limit' and not 1 <= value <= 100:
            raise CoreError('result limit must be 1 to 100')
        return value


def _fields(project, action):
    if action == 'access.status': return {}
    fields = {'id': QueryField('번호', required=action=='obituary.get', query_kind='id')}
    if action == 'obituary.get': return fields
    if project == 'bellavita-erp':
        fields['date'] = QueryField('날짜', required=False, query_kind='date')
    choices = (ERP_STATES if project=='bellavita-erp' else SHOP_STATES if project=='bellavita-shop'
               else {'draft','published'} if action=='products.list' else STAGES)
    fields['status'] = Field('상태', required=False, choices=tuple(sorted(choices)))
    fields['limit'] = QueryField('표시 개수', kind='integer', required=False, query_kind='limit')
    return fields


def result_from_adapter(report, configuration, query=None):
    """Adapter facts retain their state; a missing source never becomes zero rows."""
    if type(report) is not dict:
        return Result('uncertain', {'provider_state':'ambiguous'})
    state = report.get('state')
    mapped = {'ok':'ok','empty':'empty','not_configured':'unavailable',
              'unavailable':'unavailable','permission_denied':'denied','ambiguous':'uncertain'}
    status = mapped.get(state,'uncertain')
    config = configuration['config']
    source = {'kind':config['source_kind'], 'ref':config['source_ref'], 'branch':config['branch'],
              'contract_commit':SOURCE_COMMITS.get(report.get('project')),
              'reference':report.get('reference'), 'observed_at':report.get('observed_at')}
    data = {'query':dict(query) if query is not None else {}, 'project':report.get('project'), 'company':report.get('company'), 'action':report.get('action'),
            'provider_state':state, 'items':report.get('items',[]) if status in {'ok','empty'} else [],
            'source':source, 'operational_change':False, 'source_content_trusted_as_instruction':False}
    for key in ('total_matched','truncated'):
        if key in report: data[key]=report[key]
    evidence = None
    count = None
    if status in {'ok','empty'}:
        count = report.get('count')
        if type(count) is not int or count < 0 or (status=='empty') != (count==0):
            return Result('uncertain', {'provider_state':'ambiguous'})
        try:
            evidence = Evidence(report['project'], digest({'project':report['project'],
                'action':report['action'],'reference':report['reference'],'observed_at':report['observed_at']}),
                report['observed_at'])
        except (CoreError, KeyError):
            return Result('uncertain', {'provider_state':'ambiguous'})
    try:
        # Defensive copy and existing core size bound. Oversized evidence is not
        # silently cut down and then described as a complete source response.
        data = json.loads(encoded(data))
    except CoreError:
        return Result('uncertain', {'provider_state':'response_too_large'})
    return Result(status,data,evidence,count)


def build_registry(adapters, token, project, config_fingerprint):
    """Build request-local read tools over existing adapters and fresh authorization.

    The caller must invoke principal_for_token and check the current config on
    every incoming turn, including Core.handle idempotent-cache hits. Do not keep
    this token-bound registry as a process-wide shared conversational registry.
    """
    if not isinstance(adapters, ProjectAdapters) or project not in TOOL_ACTIONS:
        raise ValueError('registered project adapters required')
    principal = principal_for_token(adapters.store, adapters.registry, token, project)
    configuration = adapters.registry.get(token, project)
    if configuration['fingerprint'] != config_fingerprint:
        raise Conflict('conversation project configuration changed')
    companies = read_companies()

    def fence(supplied):
        if supplied != principal:
            raise AccessDenied('conversation principal does not match authenticated request')
        fresh = principal_for_token(adapters.store, adapters.registry, token, project)
        if fresh != supplied:
            raise AccessDenied('conversation identity changed')
        companies.for_principal(fresh)
        current = adapters.registry.get(token, project)
        if current['fingerprint'] != config_fingerprint:
            raise Conflict('conversation project configuration changed')

    registry = Registry()
    for tool_name, action in TOOL_ACTIONS[project].items():
        fields = _fields(project, action)
        def execute(supplied, arguments, action=action):
            fence(supplied)
            report = adapters.read(token, project, action, arguments)
            fence(supplied)
            return result_from_adapter(report, configuration, arguments)
        registry.register(Tool(tool_name, fields, execute, kind='read',
            roles=frozenset({'reader'}), context_fields=frozenset(fields), enabled=True,
            version='project-read-v1'))
    return registry
