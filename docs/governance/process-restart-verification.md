# Manager process restart verification

This increment verifies process restart over a disposable synthetic database/repository. It does not install a daemon, reboot a machine, attach existing Codex UI sessions or claim always-on operation. Command workers remain fixed synthetic programs; no model API or external tester engine is used.

## Recovery reporting

The existing authenticated `workflow/trace` endpoint adds a `recovery` snapshot. Project-wide recorded uncertain actions require manual reconciliation, including uncertainty from another task in the same project. Counts do not expose those other task IDs or private failure messages. Other projects do not affect the selected project's counts.

A running action is reported as an outcome pending confirmation. The endpoint never changes running actions into uncertain/completed states, expires leases, observes processes, reconciles work or authorizes retries. Lease expiry and process exit alone are not evidence that a previous command can safely run again. A controller-history-unavailable result is explicitly separate from no recorded uncertainty.

## Required scenarios

| Priority / group | Scenario | Required evidence |
|---|---|---|
| P0 replay prevention | A command writes, then its transport fails before acknowledging | Restarted ordinary ticks quarantine the project; source value and command count do not increase. |
| P0 authority | A prior lease expires | Report grants neither retry permission nor process-stop verification. |
| P0 scope | Another task is uncertain | Same-project quarantine is reported without exporting that task's identity; another project remains separate. |
| P1 continuation | First process exits after a persisted execution receipt | A second process reopens the same database and collects that receipt without replaying the original command. |
| P1 review | Reopened manager processes the candidate | Real existing isolated checks and independent semantic command control rework/acceptance; unsupported isolation blocks. |

The replay-prevention condition requires an unchanged actual command count AND unchanged synthetic source value across process restart AND recorded uncertain action/quarantine AND no acceptance. Clean continuation requires distinct manager process identities, the same synthetic task/database, a persisted executed checkpoint and acceptance through the existing isolated review path. Neither condition is an uptime or service recovery-time score.

## Measured result

Both scenarios passed in the supported isolation environment; evidence is `process-restart-evidence-20261117.json`. There are two separate manager processes per scenario, four in total. Each pair pins the same task ID and database device/inode digest.

Clean restart: the first process ran one developer command and persisted the `executed` checkpoint. The second process's first phase was `collect_development`; it never dispatched the original development phase. Independent review requested rework, a distinct rework dispatch produced the second command, and the real existing isolated checks allowed technical acceptance. The fixed fixture's original value-1 receipt occurred exactly once. This counter describes that fixture, not a universal exactly-once execution guarantee.

Uncertain completion: the first process performed the actual write and recorded it privately, then its transport raised before acknowledging completion to the host. Both processes observed value 1, one developer command, unchanged source HEAD and project quarantine. Four ordinary resumed ticks remained quarantined; no acceptance or repeated command occurred. The restricted isolation environment separately left clean continuation blocked while preserving the uncertainty/no-replay result.

Run `python scripts/rehearse_governance_restart.py --output <new-proof.json>` to repeat the bounded synthetic scenarios. Proof files are created exclusively and never overwrite an existing result. Independent review reran 17 focused tests and found no remaining blockers. No production deployment or model invocation was performed.
