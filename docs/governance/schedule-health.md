# Durable schedule health and explicit next actions

The existing supervision loop retains atomic attempts and due times. This change adds ten operational checks/behaviors without starting a daemon, modifying external schedules or creating an automatic recovery path:

1. Reject nonfinite, boolean and nonpositive claim clocks before reserving a job.
2. Refuse a new claim when local time precedes the retained attempt time.
3. Detect invalid retained attempt/due times and preserve the record for review.
4. Report missing expected auth, health, recovery and explicitly enabled jobs.
5. Report elapsed/overdue seconds without interpreting them as worker-stop evidence.
6. Mark snapshots stale when their supervision-settings binding differs.
7. Require the same settings binding, attempt and running state on completion.
8. Preserve disabled job history, including unresolved attempts, without deletion.
9. Provide fixed next-action codes for clocks, credentials, connection failure, retention and uncertain attempts.
10. Aggregate deduplicated attention reasons and the next retained due time without sending notifications or leaking paths/credentials.

Reports now include `schedule` on each retained job and `schedule_health` for the shared scope. A subset report still authenticates the entire shared scope; configuration currentness also checks the full shared scope. Retained successful credential refresh remains historical evidence, not current authentication proof. Invalid times are rendered safely in JSON and cannot silently free a reservation. Existing running/uncertain attempts stay blocked after timeouts or restarts.

A due time is an observation of a stored schedule, not proof the scheduler is running. `next_action` is advice only: it does not execute recovery, retry providers, refresh accounts or stop workers. Reporting performs no job claims. Existing bounded retry intervals and explicit owner/host authority remain in force. Snapshot/settings changes are diagnosed; no automatic policy migration or uncertain-attempt resolution is added.

Validation covers clocks, restart suppression, missing jobs, overdue age, changed settings, completion binding, disabled history, credential attention, shared configuration scope and corrupted retained timestamps. All fixtures are synthetic; no existing scheduled automation, customer database or live credential is used.
