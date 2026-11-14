import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import agent

class ReleaseGuardTests(unittest.TestCase):
    def test_default_disabled_and_direct_writer_denied(self):
        self.assertIs(agent.ORDER_LEDGER_ENABLED,False)
        with patch.object(agent,'sheets_service',side_effect=AssertionError('must not connect')):
            with self.assertRaises(RuntimeError):agent.write_order('synthetic',{},'2099-01-01')
    def test_queue_does_not_start_or_poll(self):
        with patch.object(agent.threading,'Thread',side_effect=AssertionError('must not start')),patch.object(agent,'db',side_effect=AssertionError('must not poll')):
            agent.start_order_queue();agent.process_order_queue()
    def test_intake_stored_without_ledger_call_even_if_sheet_env_is_set(self):
        with tempfile.TemporaryDirectory() as root,patch.object(agent,'ROOT',Path(root)),patch.dict('os.environ',{'TAESAN_ORDER_SHEET_ID':'synthetic'}),patch.object(agent,'write_order',side_effect=AssertionError('must not write')):
            result=agent.run_order('주문 접수\n설치장소: 모의홀\n보내는분: 모의회사','synthetic')
            self.assertIn('보관',result['summary'])
            with agent.db() as c:self.assertEqual(c.execute('SELECT state FROM order_operations').fetchone()[0],'pending')
