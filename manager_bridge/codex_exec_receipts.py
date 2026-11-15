"""Bounded, read-only interpretation of Codex CLI transport receipts.

A successful CLI process or model turn is not a successful command, test suite,
or verified artifact. Inputs must come from a registered host transport. Nothing
here executes text, reads credentials, retains source output, or approves work.
"""
import json
from uuid import UUID

MAX_EVENTS = 2000
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
NON_COMMAND_ITEMS = frozenset({'agent_message', 'reasoning'})


def _thread(value):
    if type(value) is not str:
        raise ValueError('exact registered thread UUID required')
    try:
        valid = str(UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('exact registered thread UUID required')
    return value


def _command(value):
    return type(value) is str and 0 < len(value) <= 8192 and bool(value.strip()) and '\x00' not in value


def inspect_exec_events(events, expected_thread, expected_command=None, *, process_exit_code=None):
    """Return observed transport facts only, never model-proclaimed test success.

    expected_thread and expected_command are trusted host registrations, not
    model/request fields. A command must exactly match its registered complete
    CLI string, including wrappers; no shell parsing or alias normalization is
    performed. A command exit 0 is still not independent test/artifact proof.
    """
    _thread(expected_thread)
    if expected_command is not None and not _command(expected_command):
        raise ValueError('bounded exact registered command required')
    if process_exit_code is not None and (type(process_exit_code) is not int or not -255 <= process_exit_code <= 255):
        raise ValueError('actual process exit code required')
    base = {'thread_id': expected_thread, 'state': 'unknown', 'reason': 'malformed_receipt',
            'process_exit_code': process_exit_code, 'dialogue_completed': False,
            'command_attempts': 0, 'command_successes': 0, 'command_failures': 0,
            'testcase_count': None, 'tests_passed': None,
            'independently_verified': False, 'operational_change': False,
            'existing_cloud_windows_connected': False, 'raw_content_retained': False}
    def result(state, reason, **facts):
        return {**base, **facts, 'state': state, 'reason': reason}
    if type(events) is not list or not events or len(events) > MAX_EVENTS:
        return result('unknown', 'invalid_event_batch')
    try:
        size = len(json.dumps(events, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError, OverflowError, RecursionError):
        return result('unknown', 'invalid_event_batch')
    if size > MAX_RECEIPT_BYTES:
        return result('unknown', 'receipt_too_large')
    phase = 'initial'
    pending = {}
    completed = set()
    attempts = successes = failures = messages = 0
    for event in events:
        if type(event) is not dict or type(event.get('type')) is not str:
            return result('unknown', 'malformed_event')
        if 'thread_id' in event and event['thread_id'] != expected_thread:
            return result('unknown', 'thread_mismatch')
        kind = event['type']
        if kind == 'thread.started':
            if phase != 'initial' or event.get('thread_id') != expected_thread:
                return result('unknown', 'invalid_thread_sequence')
            phase = 'thread'
        elif kind == 'turn.started':
            if phase != 'thread':
                return result('unknown', 'invalid_turn_sequence')
            phase = 'turn'
        elif kind in {'turn.failed', 'error'}:
            if phase not in {'thread', 'turn'}:
                return result('unknown', 'invalid_failure_sequence')
            return result('incomplete', 'turn_failed', command_attempts=attempts,
                          command_successes=successes, command_failures=failures)
        elif kind == 'turn.completed':
            if phase != 'turn':
                return result('unknown', 'invalid_turn_sequence')
            if pending:
                return result('incomplete', 'unfinished_items', command_attempts=attempts,
                              command_successes=successes, command_failures=failures)
            phase = 'completed'
        elif kind in {'item.started', 'item.updated', 'item.completed'}:
            if phase != 'turn':
                return result('unknown', 'item_outside_turn')
            item = event.get('item')
            if (type(item) is not dict or type(item.get('id')) is not str
                    or not 1 <= len(item['id']) <= 200 or type(item.get('type')) is not str):
                return result('unknown', 'malformed_item')
            key, item_kind = item['id'], item['type']
            if key in completed:
                return result('unknown', 'duplicate_completed_item')
            if item_kind not in NON_COMMAND_ITEMS | {'command_execution'}:
                return result('unknown', 'unsupported_item_type')
            if key in pending and pending[key] != item_kind:
                return result('unknown', 'item_identity_changed')
            if item_kind == 'command_execution':
                if not _command(item.get('command')):
                    return result('unknown', 'malformed_command')
                if expected_command is None:
                    return result('unknown', 'command_not_bound')
                if item['command'] != expected_command:
                    return result('unknown', 'command_mismatch')
                if 'aggregated_output' in item and type(item['aggregated_output']) is not str:
                    return result('unknown', 'malformed_command_output')
                if kind == 'item.completed':
                    if key not in pending:
                        return result('unknown', 'command_completion_without_start')
                    code, status = item.get('exit_code'), item.get('status')
                    if type(code) is not int or not -255 <= code <= 255 or status not in {'completed', 'failed'}:
                        return result('unknown', 'invalid_command_completion')
                    if (code == 0) != (status == 'completed'):
                        return result('unknown', 'contradictory_command_completion')
                    successes += code == 0
                    failures += code != 0
                elif item.get('status') != 'in_progress' or item.get('exit_code') is not None:
                    return result('unknown', 'invalid_command_progress')
            elif item_kind == 'agent_message':
                if type(item.get('text')) is not str:
                    return result('unknown', 'malformed_agent_message')
                if kind == 'item.completed' and item['text'].strip():messages += 1
            if kind == 'item.started':
                if key in pending:
                    return result('unknown', 'duplicate_item_start')
                pending[key] = item_kind
                if item_kind == 'command_execution':
                    attempts += 1
            elif kind == 'item.updated':
                if key not in pending:
                    return result('unknown', 'update_without_start')
            else:
                pending.pop(key, None)
                completed.add(key)
        else:
            return result('unknown', 'unsupported_event_type')
    facts = {'command_attempts': attempts, 'command_successes': successes, 'command_failures': failures}
    if phase != 'completed':
        return result('incomplete', 'turn_not_completed', **facts)
    if process_exit_code is None:
        return result('incomplete', 'process_not_completed', **facts)
    if process_exit_code != 0:
        return result('incomplete', 'process_failed', **facts)
    if failures:
        return result('execution_failed', 'command_failed', dialogue_completed=bool(messages), **facts)
    if attempts:
        return result('execution_observed', 'matching_commands_exit_zero', dialogue_completed=bool(messages), **facts)
    if expected_command is not None:
        return result('incomplete', 'expected_command_not_observed', dialogue_completed=bool(messages), **facts)
    if not messages:
        return result('incomplete', 'missing_agent_reply', **facts)
    return result('dialogue_completed', 'dialogue_only', dialogue_completed=True, **facts)
