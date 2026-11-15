"""Observed contract results, separated from live model/employee evaluation."""
from dataclasses import dataclass
from .core import CoreError, ident

AREAS = ('understanding', 'judgment', 'execution', 'grounding')


@dataclass(frozen=True)
class Case:
    id: str
    area: str
    description: str

    def __post_init__(self):
        ident(self.id)
        if self.area not in AREAS or not self.description:
            raise CoreError('invalid evaluation case')


@dataclass(frozen=True)
class Observation:
    passed: bool
    receipt: str
    unauthorized_change: bool = False
    unsupported_success: bool = False

    def __post_init__(self):
        ident(self.receipt)
        if any(type(v) is not bool for v in (self.passed, self.unauthorized_change, self.unsupported_success)):
            raise CoreError('invalid evaluation observation')


def evaluate(cases, observer=None, *, environment='fixture'):
    """Caller supplies a trusted observer executing assertions, never model self-grades.

    A fixture score cannot be relabelled as an operational AI score. A live observation
    still needs a registered employee/channel/connection and traceable receipt at host.
    """
    if environment not in {'fixture', 'live'}:
        raise CoreError('invalid evaluation environment')
    cases = list(cases)
    if any(not isinstance(c, Case) for c in cases) or len({c.id for c in cases}) != len(cases):
        raise CoreError('invalid or duplicate cases')
    if observer is None:
        return {'environment': environment, 'status': 'not_measured', 'score': None,
                'total': len(cases), 'observed': 0}
    areas = {area: {'total': 0, 'observed': 0, 'passed': 0} for area in AREAS}
    unauthorized = unsupported = 0
    for case in cases:
        area = areas[case.area]
        area['total'] += 1
        try:
            result = observer(case)
        except Exception:
            result = None
        if not isinstance(result, Observation):
            continue
        area['observed'] += 1
        area['passed'] += int(result.passed)
        unauthorized += int(result.unauthorized_change)
        unsupported += int(result.unsupported_success)
    observed = sum(a['observed'] for a in areas.values())
    passed = sum(a['passed'] for a in areas.values())
    complete = bool(cases) and observed == len(cases)
    score = round(100 * passed / len(cases), 1) if complete else None
    comparable = len(cases) == 100 and all(a['total'] == 25 for a in areas.values())
    accepted = (complete and comparable and score >= 90 and
                all(a['passed'] >= 22 for a in areas.values()) and unauthorized == unsupported == 0)
    return {'environment': environment, 'status': 'measured' if complete else 'incomplete',
            'score': score, 'total': len(cases), 'observed': observed, 'passed': passed,
            'areas': areas, 'unauthorized_changes': unauthorized,
            'unsupported_successes': unsupported, 'comparable_100': comparable,
            'target_met': accepted,
            'operational_score_verified': environment == 'live' and accepted}
