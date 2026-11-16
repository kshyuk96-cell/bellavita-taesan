# Four-customer connection preflight

`ProjectConnectionPreflight(store, registry, control_onboarding=None, metadata=())` reads the existing scoped `ControlOnboarding` inventory, cached `connection_readiness.Connections` facts, and durable SourceWatch heads/status. It does not invoke provider callbacks, probe services, fetch source, import customer code, read credentials, register roles, enable maintenance or grant approval.

Supported projects are exactly `bellavita-shop`, `bellavita-erp`, `hope-sunrise`, and `funeral-association`. An omitted project list selects all four; a scoped manager can request an explicit accessible subset. Unknown, duplicate or inaccessible selections fail closed.

- `inspect(token, projects=None, max_age=300)` returns exact registry revision/fingerprint bindings, source pins, registered role mappings, cached capability facts, provider metadata, project-specific next actions and explicit readiness blocks.
- `owner_packet(token, request_key, projects=None, expected_fingerprints=None, max_age=300)` creates one durable, idempotent owner-review checklist. Optional exact selected fingerprints provide a configuration comparison fence. Reusing the key with changed evidence, bindings or scope conflicts.
- `get_packet(token, packet_id)` rechecks origin authentication, project scopes and current evidence. An outdated stored snapshot is returned as `invalidated`; another actor cannot retrieve a private packet.

All packets keep `execution_blocked: true`, `approval_granted: false`, `operational_authorized: false`, `customer_acceptance_verified: false` and `model_quality_score: null`. A fresh source observation, local source snapshot, fixture test or provider metadata observation cannot establish a live receiver connection, customer acceptance or model quality. Actual work still requires the separate exact owner/policy/QA/lease fences.

## Trusted provider snapshots

Root-side read-only GitHub, Sites and Render metadata can be passed as frozen `MetadataObservation(project, config_fingerprint, purpose, provider, target, state, reference, observed_at, source_sha=None, source_revision=None)` values. No untrusted dictionary or callback is accepted. These are trusted host configuration snapshots, not HTTP authority.

`purpose` is `source` or `service`; provider is `github`, `sites` or `render`. Source targets must match the registry source reference, and service targets must match registered service IDs. State is `observed`, `permission_denied`, `unavailable` or `unknown`. References are bounded opaque evidence IDs; no provider bodies or credentials belong here. Every returned packet rejects known bearer values. Configuration drift, stale/future timestamps and missing metadata remain explicit blocks.

For source metadata, optionally supply the actual remote SHA and provider revision together. This produces an `observed_metadata_source_pin` separate from SourceWatch's durable cached source and the cached connection report's source pin. Metadata never writes either stored pin. A new provider source SHA, or a different Sites version even with the same SHA, downgrades cached connection facts to `unverified` when they cannot be tied to the latest source; `latest_source_qa_verified` remains false. GitHub equality uses exact SHA; Sites also compares its version identifier.

For example, a fresh Sites version 32 observation is not proof that cached version 28 fixtures or QA validate version 32. Likewise a new association GitHub HEAD does not inherit an older frozen source's QA. Record the new source/version, preserve originals and require a separate fresh handoff and acceptance path.

## Customer-specific boundaries

| Customer | Required review before connecting |
| --- | --- |
| Bellavita shop | Exact GitHub branch and shop requirements, registered Render review service, shop/ERP original-data boundaries. |
| Bellavita ERP | Latest Sites version and ERP requirements, Sheets/D1 ownership and unresolved conflicts, private Sites receiver boundary. |
| Hope Sunrise | Latest Sites version and travel requirements, sample balance versus actual ledger, member/staff/payment-provider boundaries. |
| Funeral association | Exact GitHub branch and association requirements, registered Render review service, separation of Sites sample from PostgreSQL review data. |

Source freshness requires matching registry fingerprints, finite nonfuture head/status timestamps and a successful cached SourceWatch status. A recent permission denial is reported even when no prior source head exists. These checks consume stored state only and never activate an integration or production service.
