"""Cost-stop advice from explicitly reported state; no billing or delete API.

A failed test or connectivity attempt is not a final governance-failure decision.
This module cannot verify provider state, cancel resources or promise a refund.
"""
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation
import math
import re

TERMINAL={'failed','abandoned','not_needed'}


@dataclass(frozen=True)
class ReportedHostState:
    outcome:str
    resource_state:str
    extra_paid_resources:str
    observed_at:float
    preservation_state:str='unknown'

    def __post_init__(self):
        if self.outcome not in {'ongoing','blocked',*TERMINAL}:raise ValueError('explicit governance outcome required')
        if self.resource_state not in {'active','stopped','deleted','unknown'}:raise ValueError('reported provider resource state required')
        if self.extra_paid_resources not in {'present','none','unknown'}:raise ValueError('explicit additional resource observation required')
        if self.preservation_state not in {'preserved','not_preserved','unknown'}:raise ValueError('explicit preservation observation required')
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:raise ValueError('finite reported observation time required')


def review_host_cost(state,*,now,quoted_hourly_usd='0.036',projection_hours=24,max_age_seconds=3600):
    if type(state) is not ReportedHostState:raise ValueError('typed reported host state required')
    if type(now) not in (int,float) or not math.isfinite(now) or now<=0:raise ValueError('finite review time required')
    if type(projection_hours) is not int or not 1<=projection_hours<=720:raise ValueError('bounded projection window required')
    if type(max_age_seconds) is not int or not 1<=max_age_seconds<=86400:raise ValueError('bounded observation age required')
    if type(quoted_hourly_usd) is not str or not re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{1,6})?',quoted_hourly_usd):raise ValueError('bounded quoted rate required')
    try:rate=Decimal(quoted_hourly_usd)
    except InvalidOperation:raise ValueError('finite quoted hourly rate required') from None
    if not rate.is_finite() or not 0<rate<=100:raise ValueError('finite positive bounded hourly rate required')
    current=0<=now-state.observed_at<=max_age_seconds
    terminal=state.outcome in TERMINAL
    reason='monitor_reported_cost';actions=[]
    # An explicit final outcome always retains the stop-cost reminder, even when
    # stale provider observations prevent determining which assets still exist.
    if terminal:actions.append('advise_stopping_unneeded_host_costs')
    if not current or state.resource_state=='unknown':
        reason='provider_state_needs_review';actions.append('verify_current_provider_resources_and_billing')
    elif state.resource_state=='deleted':
        reason='resource_reported_deleted'
        actions.append('verify_final_provider_charges')
    elif terminal:
        reason='stop_costs_recommended';actions.append('review_droplet_deletion_after_preservation_and_owner_approval')
    elif state.outcome=='blocked':
        reason='blocked_host_cost_review';actions.append('resolve_connection_path_or_review_stopping_host_costs')
    else:actions.append('review_quoted_rate_and_actual_provider_usage')
    if state.extra_paid_resources!='none':actions.append('review_remaining_chargeable_resources')
    if terminal and state.preservation_state!='preserved':actions.append('preserve_required_evidence_and_recovery_material_before_deletion')
    return {'scope':'reported_host_cost_advice_only','reason':reason,'actions':actions,
        'explicit_final_outcome':terminal,'reported_observation_current':current,
        'powered_off_may_still_be_billable':state.resource_state=='stopped',
        'projection':{'hours':projection_hours,'quoted_hourly_usd':str(rate),
            'if_one_droplet_remains_billable_usd':str(rate*projection_hours),
            'assumption':'one_droplet_billable_for_entire_projection_window',
            'estimate_only':True,'actual_charges_verified':False,'taxes_and_extras_included':False,
            'provider_minimums_or_monthly_caps_applied':False},
        'provider_state_independently_verified':False,'all_costs_stopped_verified':False,
        'refund_guaranteed':False,'resource_deletion_authorized':False,
        'automatic_deletion':False,'messages_sent':False}
