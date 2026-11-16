import unittest
from blogpay import BlogPay
class DeliveryPreservation(unittest.TestCase):
 def test_explicit_new_policy_compares_effective_delivery_not_old_read_only_flag(self):
  client=BlogPay('example','fixture')
  source={'shipSubNum':42,'isDelivery':0,'isDeliveryPrice':0,'isTodayShipUse':'N','todayShipTime':'00:00:00'}
  copy={**source,'isDeliveryPrice':1,'isTodayShipUse':None,'todayShipTime':None}
  self.assertTrue(client.same_settings(copy,source))
  for altered in ({'shipSubNum':43},{'isDelivery':1},{'enablePayMethod':0},{'eDeliBPrice':3000},{'isTodayShipUse':'Y'}):
   self.assertFalse(client.same_settings({**copy,**altered},source))
 def test_legacy_policy_differences_and_enabled_cutoffs_are_still_blocked(self):
  client=BlogPay('example','fixture')
  source={'shipSubNum':0,'isDeliveryPrice':0,'isTodayShipUse':'N'}
  self.assertFalse(client.same_settings({**source,'isDeliveryPrice':1},source))
  source={'shipSubNum':42,'isTodayShipUse':'Y','todayShipTime':'15:00:00'}
  self.assertFalse(client.same_settings({**source,'todayShipTime':'16:00:00'},source))
