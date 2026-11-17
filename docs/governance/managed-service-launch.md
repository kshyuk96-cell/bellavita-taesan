# Fixed managed service launch

The foreground service CLI now accepts `--subscription-config /absolute/private/subscription.json`. Omit it to retain the existing unmanaged startup path. The additional file must be an owned private JSON object with exactly these fields:

```json
{
  "version": 1,
  "broker_ref": "registered-broker",
  "canonical_home": "/private/canonical-profile",
  "owner_token_file": "/private/control/owner.token"
}
```

No tokens, API keys, endpoint selectors, callbacks or dynamic modules belong in this JSON. Worker targets and their roles/homes are derived from the registered host configuration rather than a second independently maintained target list. The service file must explicitly configure a bounded `supervision.subscription_interval`.

`load_managed_service` constructs the existing fixed `CodexSubscriptionRefresher` from the canonical home and the first configured native developer binary/source binding. The canonical profile and owner credential are refused inside worker source, candidate and runtime directories. The canonical profile must also be separate from the application source. Existing host validation checks the pinned clean application, project memberships, workspaces and separate developer/reviewer profiles.

Loading creates no login, model turn, API-key fallback, enrollment or credential distribution. Periodic refresh occurs only when the service runs the explicitly configured supervision schedule. Status mode does not poll that schedule. The loader rechecks all three private JSON snapshots after composition and closes the service if they changed. Error output is sanitized by the CLI.

Example foreground diagnostic, after the host has independently been qualified and registered:

```sh
python scripts/run_governance_service.py --config /private/service.json --subscription-config /private/subscription.json --status
```

`--once` and `--max-ticks` are real service runs and can dispatch approved work and refresh credentials. They are not read-only validation flags. Unbounded foreground operation uses the same path without those flags. No systemd installation, daemon activation, existing schedule changes or production deployment is performed by adding this feature.

Tests use synthetic authentication and mocked host loading, with real local service composition and CLI routing. The native refresh callback is installed but deliberately never invoked. Real host startup, current account refresh and unattended operation remain separate integration checks.

The systemd draft generator also accepts `--subscription-config-file /private/subscription.json`. This emits the matching CLI argument in `ExecStart` and applies the existing strict path grammar (no whitespace, traversal, format specifiers or newline injection). It does not read the private configuration or install/start the service. Omitting it preserves existing draft output.
