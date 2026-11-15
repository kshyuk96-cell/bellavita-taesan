"""Fail-closed review of version-bound synthetic QA evidence, never release approval."""
import re
from .store import digest
from taesan_core.universal_qa import scenario_contract_row, CONTRACT_SCHEMA


SCHEMA_VERSION = 'universal-qa/v1'


def admit_qa_report(report, *, expected_source_revision, required_coverage, expected_contract=None):
    """Validate a trusted runner's report before manager consideration.

    A digest protects artifact integrity, not observer independence or authenticity.
    The caller supplies the expected revision, coverage and independently stored
    scenario contract. Never derive expected_contract from a submitted report.
    Synthetic admission cannot authorize production, deployment or operational writes.
    """
    reasons = []
    warnings = []
    result = {'admitted': False, 'reasons': reasons, 'warnings': warnings,
              'scope': 'synthetic_only', 'operational_authorization': False}
    def reject(reason):
        if reason not in reasons:
            reasons.append(reason)
    # A digest in a worker report is integrity metadata, never authority. The
    # actual reviewed contract must arrive through the manager-owned call path.
    if type(expected_contract) is not dict:
        reject('trusted_scenario_contract_required')
        return result
    contract_rows = expected_contract.get('scenarios')
    contract_versions = expected_contract.get('adapter_versions')
    if (expected_contract.get('schema_version') != CONTRACT_SCHEMA or
            type(contract_rows) is not dict or not contract_rows or
            type(contract_versions) is not dict or not contract_versions):
        reject('malformed_trusted_scenario_contract')
        return result
    try:
        for sid, row in contract_rows.items():
            if (type(sid) is not str or not sid or type(row) is not dict or
                    row.get('id') != sid or scenario_contract_row(row) != row or
                    row.get('priority') not in ('P0','P1','P2','P3') or
                    row.get('adapter') not in contract_versions or
                    type(row.get('risk')) is not dict or
                    row['risk'].get('priority') != row['priority'] or
                    any(type(row['risk'].get(k)) is not str or not row['risk'][k].strip()
                        for k in ('impact','likelihood','recovery')) or
                    any(type(row.get(k)) is not str or not row[k].strip()
                        for k in ('group','coverage_key','oracle','state_rationale')) or
                    row['state_key'] != digest([row['adapter'],row['inputs']])):
                raise ValueError('invalid manager contract')
        trusted_digest = digest(expected_contract)
    except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
        reject('malformed_trusted_scenario_contract')
        return result
    if type(report) is not dict:
        reject('malformed_report')
        return result
    try:
        unsigned = {k: v for k, v in report.items() if k != 'report_digest'}
        if report.get('report_digest') != digest(unsigned):
            reject('report_digest_mismatch')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        reject('malformed_report')
        return result
    if report.get('schema_version') != SCHEMA_VERSION or report.get('environment') != 'synthetic':
        reject('unsupported_schema_or_environment')
    if (type(expected_source_revision) is not str or
            not re.fullmatch(r'[a-f0-9]{40}', expected_source_revision) or
            report.get('source_revision') != expected_source_revision):
        reject('source_revision_mismatch')
    if report.get('contract_digest') != trusted_digest:
        reject('scenario_contract_digest_mismatch')
    versions = report.get('adapter_versions')
    if versions != contract_versions:
        reject('trusted_adapter_versions_mismatch')
    if (type(versions) is not dict or not versions or
            any(type(k) is not str or not k or type(v) is not str or not v for k, v in versions.items())):
        reject('adapter_versions_missing')
    if type(required_coverage) not in (list, tuple, set, frozenset) or not required_coverage or any(
            type(k) is not str or not k for k in required_coverage):
        reject('invalid_required_coverage_contract')
        return result
    required = set(required_coverage)
    scenarios = report.get('scenarios')
    if type(scenarios) is not list or not scenarios:
        reject('scenarios_missing')
        return result
    ids, observed, failures, states = set(), set(), set(), set()
    executed = asserted = 0
    for scenario in scenarios:
        if type(scenario) is not dict:
            reject('malformed_scenario')
            continue
        sid = scenario.get('id')
        if type(sid) is not str or not sid or sid in ids:
            reject('duplicate_or_invalid_scenario_id')
        else:
            ids.add(sid)
        state_key = scenario.get('state_key')
        if state_key is not None and 'adapter' in scenario and 'inputs' in scenario and state_key != digest([scenario['adapter'], scenario['inputs']]):
            reject('state_key_mismatch')
        if state_key is not None:
            if type(state_key) is not str or not re.fullmatch('[a-f0-9]{64}', state_key):
                reject('invalid_state_key')
            else:
                states.add(state_key)
        try:
            if (sid not in contract_rows or
                    digest(scenario_contract_row(scenario)) != digest(contract_rows[sid])):
                reject('trusted_scenario_contract_mismatch')
        except (KeyError, TypeError, ValueError, UnicodeError):
            reject('trusted_scenario_contract_mismatch')
        priority, status = scenario.get('priority'), scenario.get('status')
        key = scenario.get('coverage_key')
        if (priority not in ('P0', 'P1', 'P2', 'P3') or
                status not in ('pass', 'fail', 'blocked', 'unmeasured') or
                type(key) is not str or not key or
                type(scenario.get('group')) is not str or not scenario.get('group') or
                'expected' not in scenario or 'actual' not in scenario):
            reject('malformed_scenario')
        if status == 'pass' and digest(scenario.get('expected')) != digest(scenario.get('actual')):
            reject('pass_value_mismatch')
        if status == 'fail' and digest(scenario.get('expected')) == digest(scenario.get('actual')):
            reject('failure_without_value_mismatch')
        if 'adapter' in scenario and (type(scenario['adapter']) is not str or
                type(versions) is not dict or scenario['adapter'] not in versions):
            reject('scenario_adapter_version_missing')
        if 'risk' in scenario:
            risk = scenario['risk']
            if type(risk) is not dict or risk.get('priority') != priority or any(
                    type(risk.get(k)) is not str or not risk[k].strip()
                    for k in ('impact', 'likelihood', 'recovery')):
                reject('invalid_risk_rationale')
        formula = scenario.get('formula')
        if formula is not None:
            if (type(formula) is not dict or any(not formula.get(k) for k in
                    ('id', 'expression', 'variables', 'units', 'rounding')) or
                    'expected' not in formula or 'actual' not in formula or
                    formula.get('status') != status):
                reject('malformed_or_incoherent_formula')
            else:
                if digest(formula['expected']) != digest(scenario.get('expected')) or digest(formula['actual']) != digest(scenario.get('actual')):
                    reject('formula_row_value_mismatch')
                if status == 'pass' and digest(formula['expected']) != digest(formula['actual']):
                    reject('formula_pass_value_mismatch')
        did_run, did_assert = scenario.get('executed'), scenario.get('asserted')
        if type(did_run) is not bool or type(did_assert) is not bool:
            reject('scenario_execution_flags_missing')
        executed += did_run is True
        asserted += did_assert is True
        if did_assert is True and did_run is not True:
            reject('assertion_without_execution')
        if status in ('pass', 'fail'):
            if did_run is not True or did_assert is not True or type(scenario.get('evidence')) not in (dict, str) or not scenario.get('evidence'):
                reject('verdict_without_execution_assertion_evidence')
            if did_run is True and did_assert is True and type(key) is str:
                observed.add(key)
        elif did_assert is True:
            reject('nonverdict_claims_assertion')
        if priority in ('P0', 'P1') and status != 'pass':
            reject('critical_scenario_not_passed')
        if status == 'fail':
            failure_key = scenario.get('failure_key', str(scenario.get('group')) + ':' + str(key))
            if 'adapter' in scenario and failure_key != digest([scenario['adapter'], key, scenario.get('expected'), scenario.get('actual')]):
                reject('failure_key_mismatch')
            if type(failure_key) is not str or not failure_key:
                reject('invalid_failure_key')
            else:
                failures.add(failure_key)
            if priority in ('P2', 'P3'):
                warnings.append('noncritical_failure:' + str(sid))
    if ids != set(contract_rows):
        reject('trusted_scenario_set_mismatch')
    counts = report.get('counts')
    expected_counts = {'generated': len(scenarios), 'executed': executed,
                       'asserted': asserted, 'unique_failures': len(failures)}
    if type(counts) is not dict or any(type(counts.get(k)) is not int or counts[k] != v
                                       for k, v in expected_counts.items()):
        reject('counts_mismatch')
    if type(counts) is dict:
        for status in ('pass', 'fail', 'blocked', 'unmeasured'):
            if status in counts and (type(counts[status]) is not int or counts[status] !=
                    sum(type(s) is dict and s.get('status') == status for s in scenarios)):
                reject('status_counts_mismatch')
        if 'unique_states' in counts and (type(counts['unique_states']) is not int or
                counts['unique_states'] != len(states) or
                any(type(s) is dict and 'state_key' not in s for s in scenarios)):
            reject('state_counts_mismatch')
    coverage = report.get('coverage')
    if type(coverage) is not dict or any(type(coverage.get(k)) is not list or any(
            type(v) is not str for v in coverage[k]) for k in ('required', 'observed')):
        reject('malformed_coverage')
    else:
        if set(coverage['observed']) != observed:
            reject('coverage_observations_mismatch')
        if not required <= set(coverage['required']) or not required <= observed:
            reject('required_coverage_missing')
    result['admitted'] = not reasons
    result['verified_counts'] = expected_counts
    return result
