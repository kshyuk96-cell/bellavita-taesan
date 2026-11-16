# Manager workflow trace

`POST /v1/representative/runtime/governance/control/operations/workflow/trace`
accepts `task_id` and an optional `limit` (1–100, default 30). The caller must be an active owner or manager with access to that task's project. No commands, callbacks, host paths or approvals are accepted.

The response groups the selected task's recorded hierarchy events, controller phase attempts, current stored host phase and outgoing next-task assignments. History covers all actors on that project/task, not only the caller's own actions. Host history is explicitly limited to the current assignment; previous private rework-assignment bodies are not exported. The controller-history availability flag describes table availability, not a live worker or registered handler. Each list has a separate total and truncation flag; events are the latest bounded window in chronological order. This is not an unbounded audit export.

Current task/config pins are compared to recorded bindings. Host configuration binding is derived from its exact controller action request because the host assignment table has no configuration column. Raw requests, reasons, assignments, source paths, receipts and credential hashes are omitted. Outgoing edge creation and parent candidate pins are historical; `current_child_*` fields describe the child's current database record, not the version originally authorized by that edge. Incoming dependency history is outside this endpoint.

This read-only database snapshot invokes no fresh observers. A recorded `accepted` state is not current verified technical acceptance, a next-task assignment is not proof of its execution, and the snapshot digest is not a signature. Execution, operational and always-on authority remain false. Use the existing independently refreshed quality/acceptance interfaces when a current decision is required.

The synthetic flow rehearsal is separate: it creates its own temporary Git repository and owner/manager/developer/reviewer identities, uses fixed local command workers and the real existing manager/host transitions, and never touches customer projects. It must retain a blocked/unverified result when actual mechanical-review isolation is unavailable. It does not build or run the separately developed external tester engine and does not demonstrate existing Codex UI chat control or autonomous model capability.

Run it with `python scripts/rehearse_governance_flow.py --output <new-report.json>`. An optional private `--work-parent` constrains temporary storage and `--max-ticks` bounds controller work. The script returns 0 only for the complete verified synthetic flow, 2 for blocked/incomplete evidence and 1 for an unverified error.

## Measured integration result

The supported execution environment completed the real command-worker rehearsal. Three developer commands ran: the first parent candidate, its rework and the dependent child. Two independent semantic commands produced `rework` then `pass`. The existing mechanical review runner verified actual isolation and checks before manager acceptance. The manager issued one rework and dispatched the exact preapproved dependent task. Attempts to start unapproved work and approve an unapproved next task were rejected; those tasks stayed pending approval.

The normal restricted execution environment separately produced a blocked isolation result; no replacement pass evidence was used. Sanitized successful evidence is `flow-rehearsal-20261117.json`. This demonstrates the manager's orchestration with synthetic command workers, not paid API availability, actual Codex model execution, existing UI conversation control, production readiness or always-on recovery.

| Priority / group | Scenario | Observed result |
|---|---|---|
| P0 authority | Start an unapproved task | Rejected; task remains pending. |
| P0 authority | Request an unapproved dependent next task | Rejected; sibling remains pending. |
| P0 isolation | Actual mechanical isolation unavailable | Blocked; no technical acceptance or next dispatch. |
| P0 scope | Worker or out-of-project caller requests a trace | Denied by role/project checks. |
| P1 workflow | First semantic review fails | Manager queues one rework. |
| P1 workflow | Reworked frozen candidate passes actual checks | Manager records technical acceptance. |
| P1 continuation | Exact preapproved dependency follows accepted parent | One confirmed dependent dispatch and actual command. |
| P1 reporting | Historical acceptance or next assignment is queried | Recorded snapshot only; fresh acceptance/execution flags false. |

The full-flow result is a conjunction, not a score: current synthetic parent technical acceptance AND exactly one rework AND semantic recommendations `[rework, pass]` AND observed rework queue AND actual approved child dispatch AND unapproved-start denial AND unapproved-next denial AND unapproved tasks remaining pending. There is no business pricing calculation in this increment and no measured AI/model capability score.
