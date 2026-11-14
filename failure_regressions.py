"""Private-safe failure counters and synthetic local regression replay.

Callers authenticate and authorize observation/review routes outside this module.
Only fixed error/task enums and tightly typed synthetic parameters are accepted.
Customer input, URLs, addresses, names, employee IDs and exception text are never
accepted or persisted. Runner callables are trusted local test adapters supplied
by the server; this module neither interprets code nor invokes external tools.
Replay describes local synthetic tests, never production quality or deployment.
Use the caller's SQLite transaction/context to commit.
"""
import hashlib
import json
import math
import time


ERROR_CODES = frozenset(('permission', 'not_found', 'rate_limit', 'timeout',
                        'network', 'schema', 'unknown', 'permission_revoked',
                        'unresolved_location', 'context_lost'))
TASK_KINDS = frozenset(('delivery_count', 'venue_rank', 'staff_report', 'photo',
                       'photo_followup', 'order_duplicate', 'stock', 'route',
                       'invitation', 'diagnostics'))
_CENTERS = frozenset(('all', 'guro', 'suwon', 'ulsan', 'cheongju', 'daejeon', 'gwangju'))
_STATES = frozenset(('all', 'complete', 'pending'))
_EXPECTED = {
    'intent': frozenset(('read', 'change', 'clarify', 'permission_denied')),
    'context': frozenset(('preserved', 'expired', 'isolated', 'clarify')),
    'grounding': frozenset(('permission_denied', 'unavailable', 'needs_review')),
    'locations': frozenset(('same', 'different', 'needs_review')),
}
DEFAULT_CASES = {
    'basement-F-vs-above': {'category': 'locations', 'parameters': {
        'left_floor': -3, 'right_floor': 3, 'left_notation': 'korean_f',
        'right_notation': 'f', 'hall_suffix': False}, 'expected': 'different'},
    'hall-suffix-review': {'category': 'locations', 'parameters': {
        'left_floor': 3, 'right_floor': 3, 'left_notation': 'korean',
        'right_notation': 'korean', 'hall_suffix': True}, 'expected': 'needs_review'},
    'permission-after-revocation': {'category': 'grounding', 'parameters': {
        'failure': 'permission_revoked'}, 'expected': 'permission_denied'},
    'timeout-is-not-zero': {'category': 'grounding', 'parameters': {
        'failure': 'timeout'}, 'expected': 'unavailable'},
}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _now(value):
    value = time.time() if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid observation time')
    return value


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS failure_observations (
        error_code TEXT NOT NULL, task_kind TEXT NOT NULL, occurrences INTEGER NOT NULL,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        PRIMARY KEY(error_code,task_kind))''')
    c.execute('''CREATE TABLE IF NOT EXISTS synthetic_regressions (
        case_id TEXT PRIMARY KEY, definition TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS synthetic_replays (
        replay_id INTEGER PRIMARY KEY AUTOINCREMENT, recorded REAL NOT NULL,
        passed INTEGER NOT NULL, failed INTEGER NOT NULL, errors INTEGER NOT NULL,
        unsupported INTEGER NOT NULL, outcomes TEXT NOT NULL)''')


def record_observation(c, error_code, task_kind, *, now=None):
    """Record an actual error CATEGORY only, never its raw payload."""
    if not isinstance(error_code, str) or error_code not in ERROR_CODES:
        raise ValueError('Unsupported error code')
    if not isinstance(task_kind, str) or task_kind not in TASK_KINDS:
        raise ValueError('Unsupported task kind')
    stamp = _now(now)
    c.execute('''INSERT INTO failure_observations VALUES (?,?,1,?,?)
        ON CONFLICT(error_code,task_kind) DO UPDATE SET
        occurrences=occurrences+1,first_seen=MIN(first_seen,excluded.first_seen),
        last_seen=MAX(last_seen,excluded.last_seen)''', (error_code, task_kind, stamp, stamp))


def observations(c):
    rows = c.execute('''SELECT error_code,task_kind,occurrences,first_seen,last_seen
        FROM failure_observations ORDER BY last_seen DESC,error_code,task_kind''').fetchall()
    keys = ('error_code', 'task_kind', 'occurrences', 'first_seen', 'last_seen')
    return [dict(zip(keys, row)) for row in rows]


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _validate_case(case):
    if not isinstance(case, dict) or set(case) != {'category', 'parameters', 'expected'}:
        raise ValueError('Only synthetic category, parameters and expected are accepted')
    category, params, expected = case['category'], case['parameters'], case['expected']
    if not isinstance(category, str) or category not in _EXPECTED or not isinstance(params, dict):
        raise ValueError('Unsupported synthetic category')
    if not isinstance(expected, str) or expected not in _EXPECTED[category]:
        raise ValueError('Unsupported synthetic expectation')
    valid = False
    if category == 'grounding':
        valid = set(params) == {'failure'} and params['failure'] in {
            'permission_revoked', 'timeout', 'network', 'schema', 'not_found', 'rate_limit'}
    elif category == 'intent':
        valid = (set(params) == {'requested_action', 'authorized'}
                 and params['requested_action'] in {'read', 'change'}
                 and type(params['authorized']) is bool)
    elif category == 'context':
        valid = (set(params) == {'date_offset', 'center', 'status', 'elapsed_seconds', 'same_owner'}
                 and _integer(params['date_offset'], 0, 30)
                 and params['center'] in _CENTERS and params['status'] in _STATES
                 and _integer(params['elapsed_seconds'], 0, 86400)
                 and type(params['same_owner']) is bool)
    elif category == 'locations':
        valid = (set(params) == {'left_floor', 'right_floor', 'left_notation', 'right_notation', 'hall_suffix'}
                 and all(_integer(params[key], -100, 100) and params[key] != 0
                         for key in ('left_floor', 'right_floor'))
                 and all(params[key] in {'korean', 'korean_f', 'f', 'b'}
                         for key in ('left_notation', 'right_notation'))
                 and type(params['hall_suffix']) is bool)
    if not valid:
        raise ValueError('Unsupported synthetic parameters; source text is not accepted')
    return json.loads(_json(case))


def register_case(c, case):
    """Accept reviewed synthetic enums/numbers, derive an ID without free text."""
    try:
        validated = _validate_case(case)
    except (TypeError, KeyError):
        raise ValueError('Unsupported synthetic parameters') from None
    definition = _json(validated)
    case_id = 'case-' + hashlib.sha256(definition.encode()).hexdigest()[:24]
    c.execute('INSERT OR IGNORE INTO synthetic_regressions VALUES (?,?)', (case_id, definition))
    return case_id


def seed_defaults(c):
    """Register known discovered failures as synthetic tests, not production proof."""
    for case_id, case in DEFAULT_CASES.items():
        definition = _json(_validate_case(case))
        c.execute('INSERT OR IGNORE INTO synthetic_regressions VALUES (?,?)', (case_id, definition))


def cases(c):
    return [{'case_id': row[0], **json.loads(row[1])}
            for row in c.execute('SELECT case_id,definition FROM synthetic_regressions ORDER BY case_id')]


def replay_cases(c, runners, *, now=None):
    """Execute registered local adapters; unsupported/error is never a pass.

    Each runner receives a fresh synthetic case dictionary including case_id and
    returns exactly one safe expectation enum. Unexpected output is a failed test,
    but no actual output or exception message is stored. Exceptions are errors.
    """
    if not isinstance(runners, dict):
        raise ValueError('Trusted local runner mapping required')
    stamp = _now(now)
    result = dict(passed=0, failed=0, errors=0, unsupported=0, outcomes=[])
    for case in cases(c):
        runner = runners.get(case['case_id'])
        if not callable(runner):
            state, field = 'unsupported', 'unsupported'
        else:
            try:
                actual = runner(json.loads(_json(case)))
                state, field = ('passed', 'passed') if type(actual) is str and actual == case['expected'] else ('failed', 'failed')
            except Exception:
                state, field = 'error', 'errors'
        result[field] += 1
        result['outcomes'].append({'case_id': case['case_id'], 'status': state})
    cursor = c.execute('''INSERT INTO synthetic_replays
        (recorded,passed,failed,errors,unsupported,outcomes) VALUES (?,?,?,?,?,?)''',
        (stamp, result['passed'], result['failed'], result['errors'], result['unsupported'], _json(result['outcomes'])))
    return {'replay_id': cursor.lastrowid, **result}


def summary(c):
    seen = c.execute('SELECT COALESCE(SUM(occurrences),0),COUNT(*) FROM failure_observations').fetchone()
    registered = c.execute('SELECT COUNT(*) FROM synthetic_regressions').fetchone()[0]
    latest = c.execute('''SELECT passed,failed,errors,unsupported FROM synthetic_replays
        ORDER BY replay_id DESC LIMIT 1''').fetchone()
    lines = [f'비식별 실패 관측 {seen[0]}회 · 유형/업무 조합 {seen[1]}개', f'가상 회귀 사례 {registered}개']
    if latest is None:
        lines.append('가상 회귀 재생은 아직 실행하지 않았어요.')
    else:
        lines.append(f'최근 가상 시험: 통과 {latest[0]} · 실패 {latest[1]} · 오류 {latest[2]} · 미연결 {latest[3]}')
    lines.append('실제 운영 점수·배포 완료를 뜻하지 않아요.')
    return '\n'.join(lines)
