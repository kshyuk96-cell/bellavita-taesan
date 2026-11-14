# Writer verification, retries, serialization and test sharing

This bundle changes review code only. No live Sheet or Kakao calls, merge, deployment or service configuration changes were performed.

## Behavior

- Ledger.save verifies every field it writes, including artwork, size, message and notes, by reading unformatted saved values. Dates/times are compared as sheet serial numbers, including midnight zero. Clearing a field during an amendment is verified. Protected creation/event-time/marker columns retain existing amendment behavior. A mismatch is held with a safe field-specific reason rather than reported as completed. Existing marker retries do not silently overwrite conflicting data.
- Failed attempts back off for 60, 120, 240, 480 and 960 seconds, with a five-attempt ceiling and review_hold state. Early duplicate requests respect the wait and do not consume attempts. A successful save resets the budget; effective human amendments reset it too. Only explicit owner-scoped `주문 재시도 <number>` or `접수 재시도 <full-id>` resumes a budget-held operation. A renewed attempt revalidates details and the sheet.
- A crash-released OS file lock serializes save and bot lifecycle/assistance actions across processes sharing the SAME SQLite file. Linux process contention and abrupt holder termination are tested, as are two competing save processes with one writer call. This does not coordinate other apps writing the same Google Sheet, separate SQLite replicas, or multiple hosts. The Windows fallback is not validated here. Existing payapp web-edit paths still need cross-process integration review.
- A local outbox queues a test summary and a test order-form SUMMARY for 기획방 only after verified ledger saving. It is NOT an exact copy of the native Sheet form. Job keys include content and change generation; retries do not duplicate them, and reverting content does not revive old receipts. No actual send adapter is connected. `주문 공유 대기` lists the authenticated owner's jobs.
- An internal adapter hook accepts exact job/destination/body-hash receipt matches; both current messages need distinct verified receipts before the sharing status changes. It has no chat command or HTTP endpoint. Tests use explicitly synthetic receipts. Amendments and cancellations invalidate prior queued content and current sharing evidence. Physical cancellation, payment reversal and recipient read confirmation remain separate.

## Evidence

114 unit tests pass, including dropped artwork/size/message/notes, failed clears, midnight serials, callback rollback, early retries, retry exhaustion, wrong and stale receipts, content reversion, two-process save contention, and crash-released locking.

`validation/full_flow_checks.py` runs 1,000 synthetic full flows with the production parser, queue, Ledger.save, amendment, fragment, additional-order and outbox code against an in-memory Sheet transport. Eight phase assertions per flow pass: 8,000 checks, NOT 8,000 independently observed orders. Fake receipts are never presented as actual Kakao delivery. Aggregate output is in full-flow-results-2026-11-14.json. The helper under tests/sheet_fake.py contains synthetic data only.

Current release scope remains draft PR #23 stacked on PR #19. Preserve the main branch's disabled ledger-write guard during any later integration. Actual source-room capture, customer bindings, native order-form extraction, group-message sending and external-writer contention remain open integration work.
