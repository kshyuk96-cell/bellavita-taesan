# Customer runtime dependency preparation

`prepare_locked_node(source, expected_sha, target, node=NODE, npm=NPM, omit_dev=True)` installs locked packages only in a new private committed-tree copy. It rejects a dirty or mismatched original checkout, an existing/nested target, committed `node_modules`, and project `.npmrc` overrides. `git archive` uses the exact SHA and safe extraction. Before npm runs, both staged `package.json` and `package-lock.json` must match raw committed blobs read through the existing hardened Git helper; export substitutions or omitted inputs fail closed. Their committed hashes are recorded, and existing ERP/Hope copies were revalidated in place without reinstalling. No customer source is edited.

Before invoking npm, `validate_locked_registry_inputs` validates every fetched entry in the complete package map, including omitted development dependencies. Only exact credential-free HTTPS `registry.npmjs.org` tarballs with valid SRI and exact versions are accepted; package/lock root dependency metadata must agree. File/git/SSH/workspace/link targets, aliases, bundles, overrides, alternate dependency graphs, duplicate JSON keys and malformed metadata fail before the installer runs. Existing prepared ERP (125 lock entries) and Hope (89 entries) passed this validation in place without repeat installation.

The installer runs the existing trusted npm binary with `ci --ignore-scripts --omit=dev --no-audit --no-fund`, fixed public registry, independent cache, and separate empty user/global npm configs. No npm lifecycle script or customer command is executed. Only managed egress proxy and public certificate trust environment references are forwarded for registry access; their values are not serialized. TLS verification remains enabled. `HOME`, npm auth, provider secrets and private keys are not supplied or read.

After installation, internal dependency links are verified not to escape the installed tree, then materialized into a separate physical `dependencies/node_modules` copy for the existing sandbox's read-only dependency bind. Every actual regular file gets a SHA256/size record, along with a complete tree digest, original package/lock hashes, installed-lock package versions/integrities and SBOM root, npm log hash, and actual Node/npm versions and binary hashes. Shipped nested package manifests are listed separately from the installed-lock dependency graph. This is dependency evidence, not a passing customer test, complete runtime attestation or business QA result.

## Actual preparation on 2026-11-16

- Node: `/opt/codex/runtimes/codex-primary-runtime/dependencies/node/bin/node`, **v24.22.1**. npm **11.9.0**. `/usr/bin/node` is absent; runtime argv must use the actual pinned binary copied into the trusted sandbox runtime.
- ERP source SHA: `13f78626e415c67ce2d07424f5ba5c1351666581` (recovered Sites v28).
- ERP lock SHA256: `ff21420c52b253241d410a0557f706230bab90e31badedfabcaaf2b8b90f316e`.
- ERP physical dependency root: `/workspace/taesan-customer-runtime-prep/bellavita-erp/dependencies/node_modules`, **536 files / 4,955,714 bytes**. Installed root versions include `qrcode-generator@2.0.4` and `read-excel-file@9.3.17` plus six locked transitive packages.
- ERP tree digest: `7cfa89abffd31dbce78b1521203696256735b9c4c6c5542eb2c86562556dc88c`. Installed package SBOM root: `dc2ebbd6dc205f0d6e6f58467e43dbdc23cd920f4ae9a379963e25cc3f4a8679`.
- Full ERP manifest and private log: `/workspace/taesan-customer-runtime-prep/bellavita-erp/dependency-manifest.json` and `npm-ci.log`. Earlier failed attempts are retained under separate diagnostic directories; they have no success manifest. Initial network attempts exposed missing egress proxy/public CA configuration, which was corrected without disabling TLS.

The existing canonical runner retains its 256 MiB trusted runtime bound, separate source bound, new network namespace, link/special-file rejection and whole-runtime before/after fingerprint. Dependency preparation changes none of those policies. The worker proof agent consumes this physical root; installation success does not assert that the QR-code import or ERP business scenarios pass inside the namespace.

## PostgreSQL and browser prerequisites

The Docker client and daemon were verified using read-only access. The local image inventory was initially empty. The official `postgres:18-alpine` image was downloaded at digest `sha256:b6972e1115a4ff8eba5d7f7d19184926d1e16d7d1136ae1e1a7ebf286da0f015`.

A task-owned temporary PostgreSQL fixture used `--network none`, no published ports, a read-only root, tmpfs data, 256 MiB memory, one CPU and 128 PIDs. Local `pg_isready`, `SELECT version()` and `SELECT 1` succeeded. No customer schema or external data was loaded. The fixture was removed afterward and all tmpfs data discarded; the downloaded image remains available. Evidence: `/workspace/taesan-customer-runtime-prep/postgresql-prerequisites.json`.

Chromium **156.0.8078.79** is installed; its actual executable SHA256/version are recorded in `/workspace/taesan-customer-runtime-prep/browser-prerequisites.json`. No browser process or customer browser case was launched. `inspect_optional_prerequisites()` only reports binary locations and leaves runtime readiness false.

A separate Docker PostgreSQL probe cannot satisfy the canonical `bwrap --unshare-net` full-stack requirement. A same-namespace reviewed application/database/browser fixture, locked browser transport dependencies and customer-schema setup remain pending. Existing isolation is not relaxed to connect to host services, and no production/public service is started.

## Minimal source-locked TypeScript prerequisite

The Funeral source at `42cfeedee9e0fbc1ffc90603b86d4fb5ed0e9e84` declares `typescript@5.9.3` in its exact lock. All 177 fetched lock entries passed the registry-only validation. `prepare_locked_registry_package` downloaded only that locked tarball into a new private target, rejected redirects before following them, verified its complete SRI before extraction, rejected tar links/special files, checked package identity and hashed all 132 extracted physical files (23,625,066 bytes). No npm lifecycle hook, compiler or customer module was executed on the host.

Package root: `/workspace/taesan-customer-runtime-prep/funeral-typescript-5.9.3/extracted/package`. Full source/lock/SRI/file evidence: `/workspace/taesan-customer-runtime-prep/funeral-typescript-5.9.3/package-manifest.json`. Compiler `lib/typescript.js` SHA256: `3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675`.

The business runner may copy this physical package into its pinned read-only runtime and transpile the actual pure customer TypeScript module inside the existing namespace. This avoids relying on Node native TypeScript stripping under `--jitless`; it does not install the full Next/tsx stack or supply PostgreSQL/browser connectivity. Compilation and business-case results belong to the independent namespace run, not this preparation evidence.
