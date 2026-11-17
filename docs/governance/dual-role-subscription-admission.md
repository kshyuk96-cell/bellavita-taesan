# Dual-role native subscription admission

Managed governance startup previously installed subscription refresh supervision without an authentication observer on its operating budget. A configured admission policy would therefore keep authentication unknown. Startup now constructs guarded, read-only native metadata observers for both configured coding roles and installs their combined observation on a policy-required operating budget.

This bundle enforces ten boundaries:

1. Native observers must use the same hierarchy core and registered guarded profiles.
2. Developer account metadata must be observed successfully.
3. Reviewer account metadata must be observed successfully.
4. Both observations must explicitly allow ordinary subscription usage.
5. Both observations must be current; the earliest validity deadline is retained.
6. Both must match the selected project and current configuration fingerprint.
7. All role memberships and worker credential bindings are checked before and after both reads.
8. Manager credential changes during observation invalidate the result.
9. Failure remains unknown/unavailable through existing admission; no API-key or paid-credit fallback is added.
10. Managed CLI startup requires governance registration, and managed governance dispatch requires an owner-installed operating-budget policy.

The fixed metadata operations are the existing `account/read` with `refreshToken:false` and `account/rateLimits/read`. They do not start a thread/model turn or refresh credentials. Native profile locks serialize them with scheduled refresh. Observations expire quickly; their deadline is metadata freshness, not OAuth expiry or proof of continuous account access. Account-wide limits are not project spend measurements. Source/task/QA/host/budget fences remain independent.

Status and preflight construct no live authentication observations. Normal foreground/once/bounded runs may poll the installed metadata observer through the existing budget schedule. The ordinary unmanaged startup path remains available without `--subscription-config`; existing durable admission requirements still apply. A managed launch cannot silently omit the governance section to bypass the new wiring.

The test suite uses synthetic native observations and private fixture profiles. It verifies denied/missing roles, expiry, future observations, project/configuration mismatches, revocation and manager rotation; factory and loader tests confirm both guarded roles and policy-required admission. No current live account authorization, new native execution or permanent-host availability is claimed by this change.
