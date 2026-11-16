# External tester result exchange v1

This contract is a bounded reference exchange for a future, separately developed tester. It does not implement a tester, run code, fetch artifacts, verify tester independence or grant admission. Every parsed receipt is classified **`externally_reported_unverified`**, including a result that claims every case passed.

The existing trusted review and governance gates remain the only technical acceptance path. `technical_accepted`, `admission_authority` and `operational_authorization` are always false in the reference evidence returned here.

## API

```python
from manager_bridge.external_tester_contract import (
    ExternalTesterBinding, parse_external_tester_result,
)

# Obtain all fields independently from the authorized manager task/policy snapshot.
expected = ExternalTesterBinding(
    project=project, task_id=task_id, task_version=task_version,
    source_revision=candidate_head_sha, manifest_digest=manifest_digest,
    policy_digest=policy_digest, policy_version=policy_version,
    config_fingerprint=config_fingerprint, candidate_digest=candidate_digest,
    qa_contract_digest=qa_contract_digest,
)
receipt = parse_external_tester_result(
    raw_utf8_json, expected_binding=expected,
    known_secrets=relevant_live_credentials,
)
reference = receipt.reference_evidence()
```

The caller must authenticate and authorize the project scope before parsing/recording, supply trusted manager-owned bindings, reject relevant live credentials, and recheck those bindings in its final recording transaction. Constructing `expected` from the submitted result defeats binding checks. This module has no HTTP route, database mutation, execution callback, network client or credential discovery mechanism.

Record the resulting reference as an external claim. Do not translate its case statuses or counts into hierarchy acceptance, owner business approval, deployment approval or a trusted runner report. Existing secrets/caller-scope guards should also be applied by the authenticated recording service; arbitrary opaque credentials cannot be identified from spelling alone.

## Exact envelope

The JSON object must contain exactly these fields:

| Field | Meaning |
|---|---|
| `schema_version` | Exactly `external-tester-result/v1` |
| `binding` | Exact independently supplied binding object described below |
| `tester_digest` | SHA-256 reference to the external tester identity/version declaration; not authentication |
| `run_digest` | SHA-256 reference to the external run record |
| `report_digest` | SHA-256 reference to the external report artifact; artifact contents are not retrieved or verified |
| `counts` | Exact case count vector |
| `cases` | Case digest references with claimed statuses and evidence digests |
| `formula_counts` | Exact formula count vector |
| `formulas` | Formula digest references with claimed statuses and evidence digests |

`binding` contains exactly `project`, `task_id`, `task_version`, `source_revision`, `manifest_digest`, `policy_digest`, `policy_version`, `config_fingerprint`, `candidate_digest` and `qa_contract_digest`. The task ID is 32 lowercase hexadecimal characters, source revision is a full 40-character Git SHA, and digest fields are 64 lowercase hexadecimal characters. Task version is a positive integer. Project and policy version are bounded identifiers. Candidate, contract and policy digests are opaque to the external tester: it can echo the manager-provided binding without implementing the manager's engine.

Each case reference contains exactly:

```json
{"case_digest":"<64 lowercase hex>","status":"pass","evidence_digest":"<64 lowercase hex>"}
```

Each formula reference has the same shape, using `formula_digest` instead of `case_digest`. References contain no raw expected/actual values, customer data, formulas, case names, logs, URLs, file paths, commands or provider credentials. The separate tester can maintain those details in its own authorized report artifact.

Allowed claimed statuses are `pass`, `fail`, `blocked` and `unmeasured`. Unknown statuses, including `accepted`, `approved`, `success` and `skipped`, are rejected. A tester may map an unexecuted/skipped result to `unmeasured`; this is a report claim, not verified execution.

Both count vectors contain exactly `total`, `pass`, `fail`, `blocked` and `unmeasured`. Counts must match the corresponding reference list, including the count of each status. Zero references with all-zero counts are permitted and convey no observations. Case/formula digests must be unique within their respective list. Larger runs must use separately bounded result exchanges; this schema does not silently accept a truncated list with a larger total.

## Limits and rejection

The parser accepts raw UTF-8 bytes or text, up to 64 KiB. It rejects already parsed dictionaries so duplicate-key and byte-budget checks cannot be bypassed. Limits are 128 case references, 128 formula references, nesting depth eight and 2,048 structural nodes.

Duplicate JSON keys, duplicate references, unknown fields, malformed or excessive outputs, nonfinite values, booleans masquerading as counts/versions, mismatched source/task/policy bindings and unknown statuses fail closed. Credential-shaped strings and supplied known credential values are rejected without echoing them in error messages. No raw payload should be logged on failure.

The receipt's `receipt_digest` hashes the normalized external envelope; `transport_digest` hashes the exact received bytes. These support reference identity and integrity checks, not authenticity, correctness or independent tester authority. Whitespace-only transport changes preserve the receipt digest while changing the transport digest.

Unknown schema versions are rejected. Future versions must receive their own explicit parser/adapter and preserve the separation between external reporting and trusted acceptance. No tester must use universal QA, the fixed review runner, or any particular execution engine merely to emit this exchange schema.
