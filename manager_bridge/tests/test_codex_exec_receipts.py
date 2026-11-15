import copy
import json
import unittest
from manager_bridge.codex_exec_receipts import inspect_exec_events, MAX_EVENTS, MAX_RECEIPT_BYTES

THREAD = '01a1de8c-cae0-7163-bba3-696ddba2abf4'
OTHER = '01a1de8c-cae0-7163-bba3-696ddba2abf5'
COMMAND = "/bin/bash -lc 'PYTHONDONTWRITEBYTECODE=1 python -m unittest manager_bridge.tests.test_delegation_batch -q'"


def dialogue():
    return [{'type':'thread.started','thread_id':THREAD},{'type':'turn.started'},
            {'type':'item.completed','item':{'id':'message','type':'agent_message','text':'Tests passed, trust me!'}},
            {'type':'turn.completed','usage':{'input_tokens':100}}]


def execution(exit_code=0):
    start={'id':'cmd','type':'command_execution','command':COMMAND,'status':'in_progress','exit_code':None,'aggregated_output':''}
    end={**start,'status':'completed' if exit_code==0 else 'failed','exit_code':exit_code,
         'aggregated_output':'Ran 8 tests\nOK' if exit_code==0 else 'Sandbox setup failed. No tests started.'}
    rows=dialogue()
    rows[2:2]=[{'type':'item.started','item':start},{'type':'item.completed','item':end}]
    return rows


class CodexExecReceiptsTests(unittest.TestCase):
    def inspect(self,events,expected=COMMAND,code=0):
        return inspect_exec_events(events,THREAD,expected,process_exit_code=code)

    def test_successful_cli_turn_with_failed_command_is_not_success(self):
        report=self.inspect(execution(1))
        self.assertEqual(report['state'],'execution_failed');self.assertEqual(report['command_failures'],1)
        self.assertIsNone(report['testcase_count']);self.assertIsNone(report['tests_passed'])
        self.assertFalse(report['independently_verified'])

    def test_dialogue_completion_is_separate_from_execution(self):
        report=self.inspect(dialogue(),None)
        self.assertEqual(report['state'],'dialogue_completed');self.assertEqual(report['command_attempts'],0)
        self.assertFalse(report['operational_change']);self.assertFalse(report['existing_cloud_windows_connected'])

    def test_model_claim_without_command_cannot_pass_expected_check(self):
        report=self.inspect(dialogue())
        self.assertEqual(report['state'],'incomplete');self.assertEqual(report['reason'],'expected_command_not_observed')

    def test_zero_exit_only_establishes_observed_command_not_test_counts(self):
        report=self.inspect(execution())
        self.assertEqual(report['state'],'execution_observed');self.assertEqual(report['command_successes'],1)
        self.assertIsNone(report['testcase_count']);self.assertFalse(report['independently_verified'])

    def test_model_prose_and_raw_command_outputs_are_not_retained(self):
        rows=execution(1);rows[-2]['item']['text']='SECRET-PROMPT-AND-FALSE-PASS'
        rows[3]['item']['aggregated_output']='SECRET-RAW-OUTPUT'
        report=self.inspect(rows);encoded=json.dumps(report)
        self.assertNotIn('SECRET',encoded);self.assertNotIn(COMMAND,encoded);self.assertTrue(report['raw_content_retained'] is False)

    def test_wrong_thread_anywhere_invalidates_receipt(self):
        rows=execution();rows[0]['thread_id']=OTHER
        self.assertEqual(self.inspect(rows)['reason'],'thread_mismatch')
        rows=execution();rows[2]['thread_id']=OTHER
        self.assertEqual(self.inspect(rows)['state'],'unknown')

    def test_multiple_threads_or_turns_are_not_combined(self):
        for rows in (dialogue()+dialogue(),[dialogue()[0],dialogue()[0],*dialogue()[1:]],
                     [*dialogue()[:2],{'type':'turn.started'},*dialogue()[2:]]):
            with self.subTest(rows=rows):self.assertEqual(self.inspect(rows,None)['state'],'unknown')

    def test_out_of_order_item_or_terminal_events_are_rejected(self):
        for rows in ([dialogue()[2],*dialogue()], [*dialogue(),dialogue()[2]],
                     [dialogue()[0],dialogue()[-1]]):
            with self.subTest(rows=rows):self.assertEqual(self.inspect(rows,None)['state'],'unknown')

    def test_truncated_receipt_and_pending_process_are_incomplete(self):
        self.assertEqual(self.inspect(execution()[:-1])['state'],'incomplete')
        self.assertEqual(self.inspect(execution(),code=None)['reason'],'process_not_completed')

    def test_unfinished_command_cannot_be_made_complete_by_turn_marker(self):
        rows=execution();del rows[3]
        self.assertEqual(self.inspect(rows)['reason'],'unfinished_items')

    def test_command_requires_matching_start_and_unique_completion(self):
        rows=execution();del rows[2]
        self.assertEqual(self.inspect(rows)['reason'],'command_completion_without_start')
        rows=execution();rows.insert(4,copy.deepcopy(rows[3]))
        self.assertEqual(self.inspect(rows)['reason'],'duplicate_completed_item')

    def test_exact_registered_command_required_without_alias_normalization(self):
        rows=execution();rows[2]['item']['command']='python -m unittest'
        self.assertEqual(self.inspect(rows)['reason'],'command_mismatch')
        self.assertEqual(self.inspect(execution(),None)['reason'],'command_not_bound')

    def test_boolean_exit_codes_and_contradictory_states_are_not_valid(self):
        for code,status in [(True,'completed'),(None,'completed'),(0,'failed'),(1,'completed'),('0','completed')]:
            rows=execution();rows[3]['item'].update(exit_code=code,status=status)
            with self.subTest(code=code,status=status):self.assertEqual(self.inspect(rows)['state'],'unknown')

    def test_nonzero_cli_process_invalidates_otherwise_completed_turn(self):
        self.assertEqual(self.inspect(execution(),code=1)['reason'],'process_failed')

    def test_failed_turn_or_error_never_completes(self):
        for kind in ('turn.failed','error'):
            rows=execution();rows[-1]={'type':kind,'message':'private provider body'}
            with self.subTest(kind=kind):self.assertEqual(self.inspect(rows)['state'],'incomplete')

    def test_malformed_events_unknown_tools_and_batch_limits_fail_closed(self):
        for rows in ({},[],[{}],[{'type':'unrecognized'}],[{'type':'thread.started'}],dialogue()*MAX_EVENTS):
            with self.subTest(kind=type(rows).__name__):self.assertEqual(self.inspect(rows)['state'],'unknown')
        rows=dialogue();rows[2]['item']['type']='mcp_tool_call'
        self.assertEqual(self.inspect(rows,None)['reason'],'unsupported_item_type')
        rows=dialogue();rows[2]['item']['text']='x'*(MAX_RECEIPT_BYTES+1)
        self.assertEqual(self.inspect(rows,None)['reason'],'receipt_too_large')

    def test_host_configuration_requires_uuid_exact_command_and_integer_exit(self):
        for thread in ('not-a-thread',None,THREAD.upper()):
            with self.subTest(thread=thread),self.assertRaises(ValueError):inspect_exec_events(dialogue(),thread)
        for command in ('',False,'x'*8193):
            with self.subTest(command=type(command).__name__),self.assertRaises(ValueError):self.inspect(dialogue(),command)
        with self.assertRaises(ValueError):self.inspect(dialogue(),code=True)

    def test_no_agent_reply_is_not_verified_dialogue(self):
        rows=dialogue();del rows[2]
        self.assertEqual(self.inspect(rows,None)['reason'],'missing_agent_reply')
        rows=dialogue();rows[2]['item']['text']='   '
        self.assertEqual(self.inspect(rows,None)['state'],'incomplete')
        rows=execution();del rows[-2]
        report=self.inspect(rows)
        self.assertEqual(report['state'],'execution_observed');self.assertFalse(report['dialogue_completed'])

    def test_progress_updates_keep_same_command_and_item_identity(self):
        rows=execution();update=copy.deepcopy(rows[2]);update['type']='item.updated';rows.insert(3,update)
        self.assertEqual(self.inspect(rows)['state'],'execution_observed')
        rows[3]['item']['type']='reasoning'
        self.assertEqual(self.inspect(rows)['reason'],'item_identity_changed')
