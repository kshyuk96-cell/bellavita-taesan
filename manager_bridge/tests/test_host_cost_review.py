from dataclasses import replace
import unittest
from unittest.mock import patch
from manager_bridge.host_cost_review import ReportedHostState,review_host_cost
from scripts import review_governance_host_cost as cli


class HostCostReviewTests(unittest.TestCase):
    def setUp(self):self.state=ReportedHostState('ongoing','active','unknown',1000)
    def review(self,**changes):return review_host_cost(replace(self.state,**changes),now=1001)
    def test_terminal_outcomes_always_advise_cost_stop(self):
        for outcome in ('failed','abandoned','not_needed'):
            self.assertIn('advise_stopping_unneeded_host_costs',self.review(outcome=outcome)['actions'])
    def test_stopped_is_not_free(self):
        v=self.review(resource_state='stopped',outcome='failed')
        self.assertTrue(v['powered_off_may_still_be_billable']);self.assertFalse(v['all_costs_stopped_verified'])
    def test_deleted_with_extra_resources_still_needs_review(self):
        self.assertIn('review_remaining_chargeable_resources',self.review(resource_state='deleted',extra_paid_resources='present')['actions'])
    def test_blocked_is_not_final_failure(self):
        v=self.review(outcome='blocked');self.assertEqual('blocked_host_cost_review',v['reason']);self.assertFalse(v['explicit_final_outcome'])
    def test_stale_or_future_provider_observation_cannot_prove_state(self):
        for observed in (1,1002):
            v=review_host_cost(replace(self.state,observed_at=observed,outcome='failed'),now=1001,max_age_seconds=10)
            self.assertFalse(v['reported_observation_current']);self.assertIn('advise_stopping_unneeded_host_costs',v['actions'])
            self.assertIn('verify_current_provider_resources_and_billing',v['actions'])
    def test_preservation_before_delete_advice(self):
        v=self.review(outcome='failed');self.assertIn('preserve_required_evidence_and_recovery_material_before_deletion',v['actions'])
        self.assertFalse(v['resource_deletion_authorized']);self.assertFalse(v['automatic_deletion'])
    def test_projection_decimal_is_conditional_not_actual_bill(self):
        v=self.review();self.assertEqual('0.864',v['projection']['if_one_droplet_remains_billable_usd'])
        self.assertTrue(v['projection']['estimate_only']);self.assertFalse(v['projection']['actual_charges_verified'])
    def test_invalid_rates_times_and_bounds_denied(self):
        for rate in ('NaN','Infinity','-1','0','not-money','1e-999999','0_1'):
            with self.assertRaises(ValueError):review_host_cost(self.state,now=1001,quoted_hourly_usd=rate)
        for now in (True,float('nan'),0):
            with self.assertRaises(ValueError):review_host_cost(self.state,now=now)
        with self.assertRaises(ValueError):review_host_cost(self.state,now=1001,projection_hours=True)
    def test_refund_and_live_billing_never_inferred(self):
        v=self.review(resource_state='deleted',extra_paid_resources='none',preservation_state='preserved')
        for k in ('refund_guaranteed','all_costs_stopped_verified','provider_state_independently_verified','messages_sent'):self.assertFalse(v[k])
    def test_cli_does_not_contact_provider(self):
        with patch('socket.socket',side_effect=AssertionError('network used')),patch('builtins.print') as printed:
            self.assertEqual(0,cli.main(['--outcome','blocked','--resource-state','active','--observed-at','1000']))
            self.assertNotIn('delete-resource',str(printed.call_args))
