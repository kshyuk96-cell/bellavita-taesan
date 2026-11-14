# Five-item local assistance bundle

Local implementation only. No upload, deployment, sheet mutation or Kakao posting was performed.

1. Missing-field clarification: `주문 확인질문 21380` or `접수 확인질문 <full-operation-id>` asks only for absent fields, plus relevant delivery notices. Placeholder values remain missing. Event time is never shipping time. `주문 보완 21380` / `접수 보완 <id>` followed by labeled values records changes; an unparsed draft retains and reparses its original current-order source, instead of treating partial fields as a complete event. Intake responses expose the full operation ID when not yet registered.
2. Owner-scoped status: `주문 상태 21380` or `접수 상태 <id>` distinguishes pending, amendment pending, saved, cancellation requested, and review hold. Verified ledger state and chat receipts are separate. No chat transport feeds `order_transmissions` yet, so no chat-success claim is inferred from a sheet write or local send attempt.
3. Explicit fragments: `주문 문구추가 21380` (or `접수 문구추가 <id>`) followed by `메시지ID: unique-id`, then `보내는분: additional title` appends multiline inscription text in order. A repeated ID with the same body is idempotent; different content under the same ID is held. Only sender fields are accepted for append. Ambiguous, unassigned messages are not automatically merged across customers. Automatic Windows source grouping remains unconnected.
4. Schedule review: Seoul-local calendar checks detect invalid or past dates, elapsed shipping clocks, shipping at/after known event start, and delivery dates after event dates even when clocks are absent. A shipping appointment within three hours gets a warning; three hours is a review threshold, not a confirmed florist deadline. Hard date/time issues hold the automatic writer; missing fields retain existing provisional registration behavior. Venue-specific installation deadlines and corrected old venue/address pairs still need review.
5. Review backlog: `주문 검토 대기` scans the authenticated staff account's latest 100 Kakao operations, displaying at most 10 entries within the Kakao text budget, with missing fields, schedule/regional issues, parser/writer failures, cancellation requests, and unconfirmed sharing. It is not a complete global admin queue. No raw customer message is exposed in the list.

All commands run after existing staff authorization. The shared process ORDER_LOCK serializes assistance with lifecycle and queue writes. A shared lease is needed for multiple service processes. Current test-only destinations and production-write guards must be preserved during integration.

Evidence: 90 unittest tests pass. 7,000 new synthetic checks cover missing-only questions, schedule holds, no event-to-shipping inference, fragment replay, unknown chat receipts, owner isolation and backlog display across 1,000 simulated staff/orders. The previous 7,000 lifecycle checks were rerun with triage integration. There were no external calls in these scripts. These are synthetic/local checks, not end-to-end Kakao/Sheet/driver confirmations.

Examples (synthetic):

```
주문 문구추가 21380
메시지ID: title-002
보내는분: 모의협회 회장
모의재단 이사
```

```
접수 보완 full-operation-id
배송일: 2026-12-01
배송시간: 10:00
규격: 2호
```

Current writer serialization, retry limits and sharing behavior supersede earlier single-process notes: see [writer/retry/sharing](WRITER_RETRY_AND_SHARING.md).
