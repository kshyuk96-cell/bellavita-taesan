# Applied test formulas, legacy quarantine, explicit recovery and PC diagnostics

## Two authorized test-copy formula edits

Only C8/I8 in 기획방_주문서테스트 were updated with the reviewed formulas, after reading current metadata, selectors, original formulas, data validation and formats. The existing test row selector and original order-form selector matched the approved preconditions. Only userEnteredValue was written in one two-request batch. Post-read confirmed exact formulas, no effective errors, both displayed values 미확인, unchanged native formats/validation, and unchanged checked selectors/ledger inputs. The existing test order was not registered again.

This proves the CURRENT missing-shipping-data branch in the real Sheets engine. Valid/invalid/duplicate-clock combinations remain reference-model tests; no extra helper cell, ledger value or selected row was written to manufacture live cases. The real test form still lacks shipping/core order details and remains ineligible for final sharing. The original order-form selector and ledger event-time column were not edited.

## Legacy origin quarantine

Unfinished Kakao operations without an order_sources entry or authenticated staff-direct approval are classified as origin_hold, and unsent sharing is invalidated. This is performed on authenticated dispatch and before enabled queue polling. The save and sharing layers fail closed as well; omitting a source-guard callback does not authorize an untracked order. Existing paid checkout operations and completed historical operations are excluded from this legacy migration rule.

New direct staff requests are marked staff-direct only after existing role authentication, in the same transaction as intake. This does not identify a customer from an employee ID. Replaying an old untracked intake cannot silently add that approval. Real customer source IDs are still not invented or provisioned.

## Explicit recovery

Use `접수 연결재확인 <full-id>` with separate lines `원본방: <verified-room-id>`, `발신자ID: <verified-sender-id>`, `보내는분: <current inscription>`, `확인근거: <actual confirmation>`. The owner must match; a revoked-source order must keep its original room/sender pair, and the current registry must have a verified binding and an active room grant. Reenable alone never restores old pending orders. Current inscription is explicit, never inferred from a changed customer default.

For genuinely source-untracked origin_hold records only, `접수 직접입력확인 <full-id>` plus explicit sender and reason can record staff-direct confirmation. A revoked-source order cannot use this route to bypass a room revocation. Cancellation states cannot be reopened by either action.

Recovery keeps the original operation/ledger identity and audit, resets retries, and records exact-message aliases under the refreshed grant. Repeating those stored snapshots does not create another order; conflicting existing intake aliases require human review. This is NOT semantic duplicate detection for arbitrary rewordings or source-event IDs. Errors after a partial local recovery roll back approval, alias, details and state together. Recovered jobs still revalidate schedule, ledger fields and native form before any write/share; a recovery acknowledgment is not registration or delivery completion.

## PC package

windows_diagnostics builds taesan-windows-diagnostics-v2.zip from an explicit four-file allowlist. The PowerShell package needs no Python and checks Kakao process presence, exact 기획방 window count and UIA Edit-control technical candidates only. It does not read chat bodies, edit values or clipboard; it does not focus/click/type/send or upload. Results remain locally and a short status file opens in Notepad so closing the console does not erase the result. Raw diagnostic exception details stay in a separate local-only file that is never packaged.

The CMD has real CRLF and the PowerShell script has UTF-8 BOM for Windows PowerShell 5. Packaging tests cover exclusion of local reports, BOM and broken escaped line endings. The actual Windows APIs have NOT been run on the company/home PC or parsed by a Windows PowerShell host here. This is a static-checked diagnostics package, not proof of native UI compatibility, remote connectivity, customer identification, capture or transmission.

## Validation and operational scope

187 unit tests and eight synthetic scripts pass locally, including 500 legacy orders with 3,000 quarantine/recovery/replay/one-writer phase checks. Main's loopback HTTP tests remain included; external Python sockets remain blocked during the CI runner. A Windows-native run and positive-time live Sheet cases remain unverified.

The only live external write in this bundle was the approved two formula cells in the test copy. No production Sheet/state DB, source registry, deployment, payment, dispatch cancellation or Kakao send was changed. Code remains in draft review with automatic ledger writes disabled.
