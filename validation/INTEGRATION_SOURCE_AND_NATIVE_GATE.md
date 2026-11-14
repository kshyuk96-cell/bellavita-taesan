# Isolated integration candidate and remaining operational limits

## Integration

customer-order-integration-20261114 incorporates the reviewed main commit without changing main, service configuration or deployment. agent.py conflicts preserve main's delivery-count and development-queue behavior and the disabled automatic-ledger-write guard. invitation.py preserves main's general invitation/checkout flow; the strict order-only parser is extracted into order_invitation.py, while newly supported literal-data vendors are shared with the general flow. All 12 cached real order references retain sender matches and all 10 available reference dates/times still match. This is offline replay, not new vendor fetching.

The integration branch includes main as an ancestor and has no unresolved conflict at the reviewed main commit. This does NOT establish deployment readiness. The original stacked PR remains a reference; review the isolated integration candidate for main compatibility.

## Unapplied shipping formula proposal

shipping-formula-proposal.json contains concrete C8/I8 formulas for the test form only. They read the delivery DATE from ledger B and exactly one explicit `배송시각 HH:MM` token from notes J. They never substitute event-time C. No clock becomes 미확인; duplicate or invalid clocks require confirmation. The native gate also independently compares the displayed value against the explicit shipping date/time in the verified current order. Tests enumerate all 1,440 valid minutes and invalid/duplicate cases.

No formula was applied to a Sheet. The formula is validated against a Python reference model, not executed by the Google Sheets engine. Before any future application, re-read the test copy selectors/formulas, preserve the original order-form selector and ledger C, and verify the actual calculated result. The current real test form still uses the old event-time reference and remains blocked for final native sharing.

## Native form and outbox

Summary jobs can wait locally, but final-form jobs start as form_review. The internal attach_native_form hook reads a supplied native cell snapshot, checks file/sheet/selected row/schema, exact approved formula, explicit shipping data and all displayed order fields against the current saved order, and only then replaces the summary with native formatted TEXT and marks the form send-eligible. This is not a visual clipboard/image copy. A failed reinspection re-blocks an unsent form. Old summary digests cannot be acknowledged as verified native content. Both current messages still require separate verified outgoing receipts before the sharing status changes. No actual Sheet-read adapter or Kakao sender is connected to this hook; tests supply synthetic cells and receipts.

## Cancellation evidence

`주문 취소 <number>` is a request only. `주문 취소확인 <number>` followed by `대상: 배송|원장|공유|청구`, `확인ID: unique-id`, `근거: actual result` records an authenticated owner's MANUAL assertion and audit entry. Plans/pending wording is held. A delivery record moves to cancel_confirmed but labels it as a staff record and leaves the other targets unknown. Billing confirmation never implies physical delivery cancellation. These functions perform no dispatch cancellation, payment reversal or external verification.

## Source revocation

New authenticated customer forwards persist room/sender provenance and revocable room/binding grants in the job and order queue. Save/retry/native attach/receipt hooks require a current source guard. Disabling a room or unlinking a sender invalidates the old grant, and reenabling/rebinding does not reactivate old pending orders. Accepted explicit inscriptions remain frozen across mere default-text changes.

Authenticated dispatch sweeps tagged pending/review orders and saved orders with unsent jobs into source_hold, invalidating their queued sharing. customer_admin also sweeps the provided --orders-db (or an existing sibling state.sqlite3) after configuration commits. Missing/corrupt source settings fail closed. Completed historical shares retain their historical state; future modifications/retries still require a current source grant. Legacy orders without recorded provenance cannot be assigned to a customer room by guessing and need separate migration review before enablement.

Source IDs are trusted authenticated STAFF-forward metadata, not proof of automatic original-sender capture. Actual Windows capture/source binding remains unconnected. A source config transaction held during a write serializes a revocation with that in-flight write; revocation only takes effect when the config commit succeeds and cannot undo a write already completed.

## Evidence and release scope

173 unit tests pass locally, including latest-main HTTP flow tests restricted to loopback. Seven synthetic scripts pass, including 4,690 new shipping/grant/cancellation checks and 1,000 full fake-sheet flows with 8,000 phase assertions. These are not independently observed real conversations, actual cancellations or Kakao deliveries. CI permits loopback for main's HTTP tests and blocks external Python connections. Korean font package requirements follow main's thumbnail tests. No private source files are used by CI.

No main merge, operational deployment, service configuration change, live Sheet write, real source binding provisioning, payment operation, driver cancellation or Kakao send occurred.
