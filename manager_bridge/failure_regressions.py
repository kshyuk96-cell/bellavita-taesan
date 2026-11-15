"""Typed failure receipts and reviewed regression selection; never raw customer input.

Collectors, fixture providers and lab observers are trusted host registrations.
A source event is not executable text, a worker claim is not a passing check, and
laboratory scores never replace operational Taesan measurements.
"""
from dataclasses import dataclass
import json
import math
import re

from .connection_readiness import SourcePin
from .model_readiness import AREAS, CASES, LabObserver, ModelLab
from .store import Conflict, NotFound, PermissionDenied, _project, _reject_credentials, _sha, canonical_json, digest

BASELINE = {'understanding': 62.5, 'judgment': 62.5, 'development': 75}
FAILURE_AREAS = {
    'intent_mismatch': 'intent', 'condition_mismatch': 'conditions',
    'context_mismatch': 'context', 'evidence_mismatch': 'grounding',
    'unauthorized_action': 'grounding', 'unsupported_completion': 'grounding',
    'tool_unavailable': 'grounding', 'fixture_failed': None,
}
CRITICAL_CODES = frozenset({'unauthorized_action', 'unsupported_completion'})
FIXTURE_CHECKS = frozenset({'authorization_regression', 'approval_regression',
    'receiver_regression', 'interruption_regression', 'source_change_regression',
    'adapter_regression'})
CASE_AREAS = {row['id']: row['area'] for row in CASES}


def _receipt(value):
    # No free-form IDs, URLs, prompts, image paths or personal details are retained.
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('opaque receipt digest required')


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('actual observation clock required')


@dataclass(frozen=True)
class FailureEvent:
    project: str
    source_sha: str
    code: str
    receipt_digest: str
    observed_at: float

    def __post_init__(self):
        _project(self.project); _sha(self.source_sha); _receipt(self.receipt_digest); _clock(self.observed_at)
        if type(self.code) is not str or self.code not in FAILURE_AREAS:
            raise ValueError('typed failure code required')


@dataclass(frozen=True)
class FixtureResult:
    project: str
    source_sha: str
    passed: int
    failed: int
    skipped: int
    checks: tuple
    receipt_digest: str
    observed_at: float

    def __post_init__(self):
        _project(self.project); _sha(self.source_sha); _receipt(self.receipt_digest); _clock(self.observed_at)
        if any(type(n) is not int or not 0 <= n <= 100000 for n in (self.passed, self.failed, self.skipped)):
            raise ValueError('strict measured fixture counts required')
        if type(self.checks) is not tuple or len(self.checks) > len(FIXTURE_CHECKS):
            raise ValueError('registered fixture checks required')
        if self.passed + self.failed < len(self.checks):
            raise ValueError('checks require actual observed fixture cases')
        seen = set()
        for item in self.checks:
            if (type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                    or item[0] not in FIXTURE_CHECKS or type(item[1]) is not bool or item[0] in seen):
                raise ValueError('typed unique fixture checks required')
            seen.add(item[0])


@dataclass(frozen=True)
class FailureProfile:
    pin: SourcePin
    collect: object = None
    fixtures: object = None
    lab: object = None

    def __post_init__(self):
        if not isinstance(self.pin, SourcePin): raise ValueError('pinned source required')
        if self.collect is not None and not callable(self.collect): raise ValueError('host collector required')
        if self.fixtures is not None and not callable(self.fixtures): raise ValueError('host fixture provider required')
        if self.lab is not None and not isinstance(self.lab, LabObserver): raise ValueError('registered model lab required')


class FailureRegressions:
    def __init__(self, store, registry, profiles=None):
        if registry.store is not store: raise ValueError('shared project registry required')
        self.store, self.registry = store, registry
        self.profiles = dict(profiles or {})
        for project, profile in self.profiles.items():
            if not isinstance(profile, FailureProfile) or project != profile.pin.project:
                raise ValueError('exact project failure profile required')
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS manager_failure_receipts (
              id TEXT PRIMARY KEY, project TEXT NOT NULL, company TEXT NOT NULL,
              source_sha TEXT NOT NULL, config_fingerprint TEXT NOT NULL,
              code TEXT NOT NULL, receipt_digest TEXT NOT NULL,
              observed_at REAL NOT NULL, recorded_at REAL NOT NULL,
              reviewed_cases TEXT, reviewer TEXT, reviewed_at REAL,
              UNIQUE(project,source_sha,code,receipt_digest));
            ''')

    def _config(self, c, token, project, roles=('owner', 'manager')):
        return self.registry._get(c, token, project, roles=roles)

    def _binding(self, config, project):
        profile = self.profiles.get(project)
        if profile:
            actual = config['config']
            pin = profile.pin
            if (pin.source_kind, pin.source_ref, pin.branch) != (actual['source_kind'], actual['source_ref'], actual['branch']):
                raise Conflict('failure observer source binding changed')
        return profile

    def _fence(self, token, project, before, roles=('owner', 'manager')):
        with self.store._transaction() as c:
            config = self._config(c, token, project, roles)
            if config['fingerprint'] != before['fingerprint']:
                raise Conflict('project changed during observation')
        return config

    @staticmethod
    def _age(value, now, max_age):
        return 0 <= now - value <= max_age

    def collect(self, token, project, *, max_age=3600):
        if type(max_age) is not int or not 1 <= max_age <= 86400: raise ValueError('bounded observation age required')
        roles = ('owner', 'manager', 'worker')
        with self.store._transaction() as c: config = self._config(c, token, project, roles)
        profile = self._binding(config, project)
        if profile is None or profile.collect is None:
            return {'project': project, 'state': 'not_configured', 'inserted': 0, 'operational_change': False}
        try: events = profile.collect(project)
        except PermissionDenied:
            self._fence(token, project, config, roles)
            return {'project': project, 'state': 'permission_denied', 'inserted': 0, 'operational_change': False}
        except Exception:
            self._fence(token, project, config, roles)
            return {'project': project, 'state': 'unavailable', 'inserted': 0, 'operational_change': False}
        if type(events) not in (tuple, list) or len(events) > 100:
            raise ValueError('bounded typed event batch required')
        now = self.store._now()
        for event in events:
            if not isinstance(event, FailureEvent): raise ValueError('typed failure event required')
            _reject_credentials({'source_sha': event.source_sha, 'receipt_digest': event.receipt_digest}, token)
            if event.project != project: raise PermissionDenied('failure event project mismatch')
            if event.source_sha != profile.pin.sha: raise Conflict('failure event source mismatch')
            if not self._age(event.observed_at, now, max_age): raise Conflict('stale failure observation')
        inserted = 0
        with self.store._transaction() as c:
            current = self._config(c, token, project, roles)
            if current['fingerprint'] != config['fingerprint']: raise Conflict('project changed during collection')
            for event in events:
                key = digest({'project': project, 'source_sha': event.source_sha,
                    'code': event.code, 'receipt_digest': event.receipt_digest})
                old = c.execute('SELECT config_fingerprint FROM manager_failure_receipts WHERE id=?', (key,)).fetchone()
                if old:
                    if old['config_fingerprint'] != config['fingerprint']: raise Conflict('failure receipt belongs to old configuration')
                    continue
                c.execute('INSERT INTO manager_failure_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key, project, config['config']['company'], event.source_sha, config['fingerprint'],
                     event.code, event.receipt_digest, event.observed_at, now, None, None, None,))
                inserted += 1
        return {'project': project, 'state': 'observed', 'inserted': inserted,
                'observed': len(events), 'operational_change': False}

    def incidents(self, token, project):
        with self.store._transaction() as c:
            config = self._config(c, token, project)
            rows = c.execute('SELECT * FROM manager_failure_receipts WHERE project=? ORDER BY observed_at,id', (project,)).fetchall()
            return [self._public(row, config) for row in rows]

    @staticmethod
    def _public(row, config):
        return {'id': row['id'], 'project': row['project'], 'company': row['company'],
                'source_sha': row['source_sha'], 'code': row['code'],
                'area': FAILURE_AREAS[row['code']], 'critical': row['code'] in CRITICAL_CODES,
                'receipt_digest': row['receipt_digest'], 'observed_at': row['observed_at'],
                'configuration_current': row['config_fingerprint'] == config['fingerprint'],
                'state': 'reviewed' if row['reviewed_cases'] is not None else 'pending_review',
                'regressions': json.loads(row['reviewed_cases']) if row['reviewed_cases'] is not None else []}

    def review(self, token, project, incident_id, selectors):
        _receipt(incident_id); _reject_credentials(incident_id, token)
        if type(selectors) is not list or not 1 <= len(selectors) <= 25 or any(type(s) is not str for s in selectors) or len(set(selectors)) != len(selectors):
            raise ValueError('explicit bounded unique regression selectors required')
        with self.store._transaction() as c:
            config = self._config(c, token, project)
            actor = self.store._authenticate(c, token)
            row = c.execute('SELECT * FROM manager_failure_receipts WHERE id=? AND project=?', (incident_id, project)).fetchone()
            if row is None: raise NotFound('failure receipt not found')
            profile = self._binding(config, project)
            if profile is None or row['source_sha'] != profile.pin.sha or row['config_fingerprint'] != config['fingerprint']:
                raise Conflict('review requires current pinned failure receipt')
            area = FAILURE_AREAS[row['code']]
            allowed = FIXTURE_CHECKS if area is None else {case_id for case_id, case_area in CASE_AREAS.items() if case_area == area}
            if any(type(s) is not str or s not in allowed for s in selectors): raise ValueError('review selector outside fixed catalog')
            encoded = canonical_json(sorted(selectors))
            if row['reviewed_cases'] is not None and row['reviewed_cases'] != encoded:
                raise Conflict('reviewed regression mapping is immutable')
            c.execute('UPDATE manager_failure_receipts SET reviewed_cases=?,reviewer=?,reviewed_at=? WHERE id=?',
                (encoded, actor['token_hash'], self.store._now(), incident_id))
            return {'id': incident_id, 'project': project, 'state': 'reviewed', 'regressions': sorted(selectors),
                    'executable_text_accepted': False, 'operational_change': False}

    def evaluate(self, token, project, *, max_age=3600):
        if type(max_age) is not int or not 1 <= max_age <= 86400: raise ValueError('bounded observation age required')
        with self.store._transaction() as c: config = self._config(c, token, project)
        profile = self._binding(config, project)
        fixture = {'state': 'not_measured', 'passed': None, 'failed': None, 'skipped': None}
        checks = {}
        if profile and profile.fixtures:
            try:
                observed = profile.fixtures(project)
                if not isinstance(observed, FixtureResult): raise ValueError('typed fixture observation required')
                _reject_credentials({'source_sha': observed.source_sha, 'receipt_digest': observed.receipt_digest}, token)
                if observed.project != project: raise PermissionDenied('fixture observation project mismatch')
                if observed.source_sha != profile.pin.sha: raise ValueError('fixture observation source mismatch')
                if not self._age(observed.observed_at, self.store._now(), max_age):
                    fixture['state'] = 'stale'
                else:
                    state = 'failed' if observed.failed or any(not v for _, v in observed.checks) else ('partial' if observed.skipped else ('verified' if observed.passed else 'not_measured'))
                    fixture = {'state': state, 'passed': observed.passed, 'failed': observed.failed,
                        'skipped': observed.skipped, 'receipt_digest': observed.receipt_digest, 'observed_at': observed.observed_at}
                    checks = dict(observed.checks)
            except PermissionDenied: fixture['state'] = 'permission_denied'
            except ValueError: fixture['state'] = 'unknown'
            except Exception: fixture['state'] = 'unavailable'
        self._fence(token, project, config)
        # ModelLab receives only its static public catalog; caller cannot submit a score.
        lab = ModelLab(profile.lab if profile else None).run()
        _reject_credentials(lab, token)
        self._fence(token, project, config)
        failures = {item['id'] for item in lab.get('failures', [])}
        results = []
        incidents = self.incidents(token, project)
        for item in incidents:
            state = 'pending_review'
            if not item['configuration_current'] or not profile or item['source_sha'] != profile.pin.sha: state = 'stale'
            elif item['state'] == 'reviewed':
                if item['area'] is None:
                    values = [checks.get(key) for key in item['regressions']]
                    state = 'failed' if False in values else ('not_measured' if None in values else 'passed')
                elif lab.get('status') != 'graded': state = 'not_measured'
                elif lab.get('source') == 'fixture': state = 'fixture_only'
                else: state = 'failed' if failures.intersection(item['regressions']) else 'passed'
            results.append({'incident_id': item['id'], 'state': state, 'critical': item['critical']})
        self._fence(token, project, config)
        return {'project': project, 'company': config['config']['company'],
                'source_sha': profile.pin.sha if profile else None,
                'fixture_reliability': fixture, 'model_lab': lab, 'regressions': results,
                'pending_review': sum(item['state'] == 'pending_review' for item in results),
                'critical_regressions_clear': bool(results) and lab.get('target_met') is True and lab.get('source') == 'native_model_lab' and all(not item['critical'] or item['state'] == 'passed' for item in results),
                'operational': {'state': 'not_measured', 'score_verified': False, 'baseline': dict(BASELINE)},
                'operational_change': False}
