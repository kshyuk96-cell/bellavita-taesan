# Order amendments, cancellation requests and explicit additional orders

Local review implementation only; not deployed or published. Authenticated staff use:

```
주문 수정 21380
주소: 부산광역시 해운대구 …
```

Only editable labeled fields are accepted. The original operation ID and ledger row remain unchanged, event time is preserved, and before/after details are recorded. Existing queue writes the same row with update=True. Conflicting, empty or invalid date/time fields are held.

```
주문 취소 21380
```

Records `cancel_requested` and an audit snapshot; removes it from automatic write eligibility, including direct retries. It does NOT erase the sheet row, reverse a charge, retract a chat post, or cancel a driver's work. Staff receive an explicit unconfirmed physical-cancellation notice. Those confirmations need a separate workflow.

```
추가 주문 21380
요청ID: additional-001
```

This explicitly requests another order for the SAME event, venue and inscription; it is not an automatic historical default. The queued copy has a new operation ID, no assigned ledger row, and provenance linking to the parent. The same request ID on the same parent and staff account is idempotent, even after the new order saves. A new ID denotes a separately requested additional order. Old canonical-message keys are unchanged; generic incoming transport duplicates and migration to original source-event IDs remain open.

References must uniquely identify a saved Kakao order owned by the authenticated staff account. Ambiguous “the earlier one” requests ask for a number rather than guessing. Orders pending amendment/cancellation cannot be copied. The shared ORDER_LOCK serializes these actions with queue writes in the current single-process service. Multiple service processes would require a shared transactional lease before deployment.

Validation: 72 unit tests pass, including authenticated dispatch and fake ledger calls. 7,000 synthetic checks pass over 1,000 independent parent orders: same-row amendments, amendment replay, same-ID additional replay, distinct additional IDs, owner isolation, cancellation replay and blocked cancelled-order writes. No external services are called. Real sheet/chat/driver confirmation is still untested.

Current writer serialization, retry limits and sharing behavior supersede earlier single-process notes: see [writer/retry/sharing](WRITER_RETRY_AND_SHARING.md).
