# Customer worker profile preparation

`CustomerWorkerProfiles(store, registry, core=None)` prepares trusted host inputs. It does not install tools, authenticate a coding profile, read token files, run customer tests, enable maintenance, or launch a worker.

Call `prepare(token, project, source, base_sha=..., checks=..., evidence_files=..., paths=..., profiles=..., session_id=None)` from the trusted host configuration layer. Projects must be one of the four customer catalog identifiers. The source must be a clean physical checkout at the exact SHA. `checks` maps check identifiers to fixed argv lists; their canonical digest and the registry configuration fingerprint pin the packet. Command registration is preparation, not a passing test result.

`paths` has exactly `candidates`, `runtime`, `developer_token_file`, and `reviewer_token_file`. `profiles` has distinct `developer` and `reviewer` objects with absolute `binary` and `codex_home`, optionally `require_origin` and bounded `execution_timeout`. Work roots, profiles, source, database, and role-token references must satisfy the isolation checks. Paths are references only: credential contents are never serialized or inspected. Private-directory permissions, credential validity, executable availability and profile authentication remain pending until the existing loader validates them.

The returned `continuous_host_project` is the project-entry schema accepted by `manager_bridge.continuous_host.load_host`. Add a registered canonical session UUID (`session_id`) or an approved Fleet template before loading it. The existing native transport UUID validator is reused. A final scoped authentication transaction rejects any registered bearer accidentally embedded anywhere in the completed packet, including another role’s token in command arguments or path references. The enclosing private configuration still needs the existing version/application commit/data root/database/manager-token/instance fields. Use `load_host`; no alternate executor is introduced. A successful packet always reports `execution_ready: false` and `authentication_verified: false`.

`bind_roles(owner, project, developer_credential_hash, reviewer_credential_hash, manager_hash)` requires an existing ThreeTier core. It atomically applies its live credential, capability, subject independence, source-configuration and active-membership invariants. Member IDs are `<project>-developer`, `<project>-reviewer`, and `<project>-manager`. Registration does not prove connection or authorize work. No raw credential is accepted or returned.

## Requirements evidence boundary

The manifest records hashes and byte counts only for explicit README, package manifest and test files actually read, plus source SHA, commandset digest and config fingerprint. It explicitly reports `requirements_scope: selected_files_only`, `all_requirements_known: false` and `tests_executed: 0`. Missing business requirements, provider integration contracts, runtime state and independent QA remain separate prerequisites.

The following local sources were inspected while preparing this module on 2026-11-16. These are local source versions, not proof of current remote heads. Recovered Sites checkouts are pinned at version 28 (ERP) and version 37 (Hope); authentication and runtime/QA connections remain pending.

| Customer | Local inspected SHA | Inspected starting evidence | Preparation implications |
| --- | --- | --- | --- |
| bellavita-shop | `d5047f3345b757edfcfbd81a73c609c0392007dd` | `README.md`, `tests/test_shop_catalog.py` | Python shop/ERP integration; README distinguishes source-only changes from deployment. Preserve the separate platform migration requirements. |
| funeral-association | `42cfeedee9e0fbc1ffc90603b86d4fb5ed0e9e84` | `README.md`, `package.json`, `tests/family-task-items.test.ts` | Node/PostgreSQL review platform; existing Sites sample is a separate data environment. Real verification, messaging and payment providers remain pending. |
| bellavita-erp | `13f78626e415c67ce2d07424f5ba5c1351666581` | `README.md`, `package.json`, `tests/settlement.test.js` | Recovered Sites v28. Preserve original Sheets/D1 ownership and unresolved data conflicts; README documents skipped private-fixture tests and unfinished visual QA. |
| hope-sunrise | `985a918f55f3cd2fbf0445d6ef38c761332577d2` | `README.md`, `package.json`, `tests/commerce.test.mjs` | Recovered Sites v37 prototype. README documents pending identity, staff roles, payment and supplier integrations; displayed sample balances are not an operating ledger. |

Source directories were `/workspace/taesan-governance-portfolio-proof/sources/{bellavita-shop,funeral-association}` and `/workspace/taesan-rollout-source-recovery/{bellavita-erp,hope-sunrise}/.sites-checkout`. The module does not hardcode these paths or assume these SHAs remain latest. Supply explicit reviewed test files when creating each packet; an inventory listing alone does not attest their requirements or execution.

## Prepared four-project packet

[customer-worker-profile-packets.json](customer-worker-profile-packets.json) records the actual inspected current four checkouts, SHA256 hashes for explicitly selected README/package or requirements files and every selected command's test files, and the fixed argv plan coordinated with the QA registry. It contains no credential values. Token/profile/work paths under `/workspace/taesan-worker-runtime-prep` are **uninstalled references**, and every packet remains pending authentication, role binding, capacity, runtime isolation, source connector and QA registration. The temporary registration database used to prepare these packets was discarded; no customer role credentials or operating registrations were created.

Python discovery and Node/tsx/Playwright commands in this packet were not executed. Node SQLite support, installed tsx/Playwright modules, a separate local PostgreSQL/Next review environment and all provider/role authentication remain prerequisites. Sites transport entries explicitly record that an origin check cannot substitute for their Sites source/version observation. Final profile loading still runs the existing private `load_host` checks.
