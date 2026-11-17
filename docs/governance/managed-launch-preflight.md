# Ten-check managed launch diagnostics

Use `python scripts/run_governance_service.py --config /private/service.json --subscription-config /private/subscription.json --preflight` to inspect local configuration before composing a service. This path never constructs a service, opens a listener, opens the manager database, reads credential file contents, invokes a native binary, refreshes authentication or dispatches tasks. It reads the three private JSON configurations, inspects filesystem metadata and invokes fixed Git read commands for the application pin. Git optional locks, hooks and filesystem monitoring are disabled for that inspection.

The ten checks are:

1. Private, bounded configuration files and supported top-level schemas.
2. Bounded explicit supervision schedule, poll interval and listener settings.
3. Bounded unique project scope and supported host-entry fields.
4. Distinct developer/reviewer member IDs and transport setting fields.
5. Owned private, nonoverlapping canonical and worker profile directories.
6. Distinct owned 0600 credential/auth files with bounded size and one link; metadata only.
7. Credential and configuration paths separated from worker workspaces/application source.
8. Absolute existing executable paths; binaries are never executed or authenticated here.
9. This running application's clean checkout and exact configured commit.
10. Private nonempty database file metadata in its registered data root, separate from credentials.

Each result uses a fixed check name and `passed` or `blocked`. Exceptions, configured paths, tokens and raw configuration are omitted. Unreadable configurations return all ten blocked checks. Configuration snapshots are reread at the end; drift prevents an overall pass. Counts are configuration checks, not an AI capability score or a governance-completion percentage. CLI exit 0 means only these preliminary checks passed; exit 2 means blocked.

This report is not an admission gate and cannot replace the complete service loader, owner registration, current source checks, subscription account observation, host namespace/fsync qualification or independent restore. In particular, filesystem metadata does not establish valid credentials, SQLite schema/integrity, binary identity or current role membership. Those unverified facts remain false in the report even when all ten checks pass. No existing schedule or runtime admission policy is changed by these diagnostics.
