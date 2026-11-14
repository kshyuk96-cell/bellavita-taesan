# Compatibility, native form inspection, customer settings, backlog pagination and CI

## Compatibility

Reviewed main commit is recorded in main-compatibility-2026-11-14.json. It contains disabled automatic ledger writes. The review branch now explicitly preserves ORDER_LEDGER_ENABLED=False in intake, direct writing, queue polling and queue startup; tests prove no Sheet session or queue thread starts while disabled. This is a compatibility REVIEW, not an integration merge: merge-tree simulation reports agent.py and invitation.py conflicts. Do not merge the stacked branch directly into main or claim it deploy-ready. Newer delivery-count and development-queue behavior needs conflict-aware integration.

## Native form, read only

Read the test copy's first form block, selected-row cell, and ledger headers with the connected Sheets reader. No cell was changed and no test order was added. The original form's selected-row input remains unchanged. The copied native form has duplicate left/right blocks; plain-text extraction selects the first block's formatted values only, rather than duplicating both or evaluating formulas locally.

The shipping-time label currently reads the ledger EVENT time column. native_order_form.inspect_form returns ready_for_final_share=False for that reference or an unverified shipping source. Aggregate inspection is in native-form-inspection-2026-11-14.json. No customer cell text, name, address, spreadsheet link or original formula payload is included. Actual returned cells remain in private local storage. This task did not fix the Sheet formula or connect native-form text to a sender. The existing outbox remains a clearly labeled order SUMMARY, not an exact native-form copy.

## Local customer configuration

CustomerMemory modifications are transactional and audited. Import failures roll back profile, room, binding, variant and audit changes together. Use `customer_admin.py --db /private/customer-memory.sqlite3 --config /private/reviewed.json` to import, or `--changes /private/changes.json` for an ordered array of local operations: profile, binding, variant, allow_room, disable_room, unbind, remove_variant, remove_profile. Field definitions are in customer_admin.OPERATIONS; no remote endpoint or chat configuration command is exposed.

Changing an existing binding to another customer requires explicit unbind with the expected old customer ID before rebinding. Disabling a room removes its bindings; reenabling it does not restore identities. Profile removal revokes bindings, variants and historical evidence; the audit remains in the private DB, so this is not a personal-data-erasure function. Existing order snapshots are not rewritten when defaults change. Real customer source IDs have not been provisioned.

## Review pages

`주문 검토 대기` scans the full authenticated owner's Kakao order history in bounded 50-row database chunks. Filters: 전체, 누락, 일정, 지역, 오류, 취소, 공유, 재시도. Example: `주문 검토 대기 오류`. Follow the exact next-page command shown. The keyset cursor carries account and filter context; a different account or filter is rejected. No raw order text is included. Page text stays under the Kakao message budget; changing order states can change later review membership. Tests cover 120 equal-timestamp records without loss/duplication and finding an error older than the old 100-row cutoff.

## CI

The pull-request workflow runs Python 3.12 and Node 24 on Ubuntu with read-only repository permission and no secrets or deployment step. It installs only public test dependencies, runs the unit suite with outbound Python sockets blocked, and executes six synthetic-only scripts. It never reads private customer source files. Artifacts contain aggregate counts only and expire after seven days. Runtime networking restrictions here are a regression guard, not an OS sandbox for arbitrary subprocess code.

Local validation: 133 unit tests pass. All six synthetic scripts complete (8,100 edge checks; 5,600 customer checks; 7,000 lifecycle checks; 7,000 assistance checks; 6,000 rule checks; 1,000 complete flows with 8,000 phase assertions). These include reruns, and phase assertions are not independent real conversations. GitHub execution status must be checked after publication; local success alone is not reported as a remote CI pass.
