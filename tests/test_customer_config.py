import unittest
from customer_memory import CustomerMemory
from customer_orders import apply_config
from customer_admin import apply_changes

class CustomerConfigTests(unittest.TestCase):
    def setUp(self):
        self.m=CustomerMemory(':memory:');self.addCleanup(self.m.db.close)
        self.m.confirm_profile('a','모의고객','모의회사 대표','test');self.m.allow_room('r','test');self.m.bind('r','s','a','test')
    def test_profile_changes_audited_and_default_updated(self):
        self.m.confirm_profile('a','모의고객','새 모의회사 대표','test2')
        self.assertEqual(self.m.resolve('r','s').sender,'새 모의회사 대표')
        self.assertEqual(self.m.db.execute("SELECT COUNT(*) FROM customer_config_audit WHERE action='profile'").fetchone()[0],2)
    def test_room_revocation_removes_binding_and_reenable_does_not_restore(self):
        self.m.disable_room('r','test');self.m.allow_room('r','test')
        self.assertEqual(self.m.resolve('r','s').status,'review')
    def test_explicit_unbind_requires_expected_customer(self):
        with self.assertRaises(ValueError):self.m.unbind('r','s','other','test')
        self.assertEqual(self.m.resolve('r','s').status,'auto')
        self.m.unbind('r','s','a','test');self.assertEqual(self.m.resolve('r','s').status,'review')
    def test_import_failure_rolls_back_profiles_rooms_and_audit(self):
        config={'profiles':[{'id':'b','name':'모의B','sender':'모의협회','confirmed_by':'test'}],'rooms':[{'id':'new','confirmed_by':'test'}],'bindings':[{'room':'new','sender_id':'new','customer_id':'missing','confirmed_by':'test'}]}
        count=self.m.db.execute('SELECT COUNT(*) FROM customer_config_audit').fetchone()[0]
        with self.assertRaises(ValueError):apply_config(self.m,config)
        self.assertFalse(self.m.db.execute("SELECT 1 FROM customer_profiles WHERE id='b'").fetchone())
        self.assertFalse(self.m.db.execute("SELECT 1 FROM customer_order_rooms WHERE room='new'").fetchone())
        self.assertEqual(self.m.db.execute('SELECT COUNT(*) FROM customer_config_audit').fetchone()[0],count)
    def test_batch_changes_rollback_on_unknown_action(self):
        with self.assertRaises(ValueError):apply_changes(self.m,[{'operation':'disable_room','room':'r','confirmed_by':'test'},{'operation':'unknown'}])
        self.assertEqual(self.m.resolve('r','s').status,'auto')
    def test_removal_revokes_variants_and_history_without_reusing_identity(self):
        self.m.confirm_variant('v','a','모의협회','협회','test');self.m.record_history('a','synthetic-row','모의협회')
        self.m.remove_profile('a','test')
        for table in ('customer_profiles','customer_bindings','sender_variants','sender_evidence'):
            self.assertEqual(self.m.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(self.m.resolve('r','s').status,'review')
