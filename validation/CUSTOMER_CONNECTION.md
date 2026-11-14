# Confirmed source adapter

`agent.dispatch` accepts an authenticated staff forward in this form:

```
고객 주문 room-id sender-id
https://vendor.example/invitation
```

The IDs must be verified original customer room/sender IDs, never the employee channel ID or a display name. Staff forwarding is an explicit trusted action; it is not automatic Windows capture or cryptographic proof of original source. The adapter runs only after staff authentication. Existing channel messages remain separate.

Import a private locally reviewed JSON file with `python customer_orders.py --db /private/taesan-data/customer-memory.sqlite3 --confirmed-config /private/confirmed.json`. Top-level arrays: `profiles` (id,name,sender,confirmed_by), `rooms` (id,confirmed_by), `bindings` (room,sender_id,customer_id,confirmed_by). Do not commit the file. No actual customer source IDs have been provisioned in this review branch.

Link-only messages use a confirmed default only for an allowed room and matching sender. Explicit labeled multiline inscriptions win. Unlabeled signatures or extra instructions are held rather than overwritten. Enriched current-order text is persisted in the existing order queue, so retries do not reselect a later default. Dates, venue and private historical event details are never copied from a profile.

This branch is not deployed. Keep the main branch automatic-ledger-write guard when integrating. Validate the copy workbook and planning-room receipt before production enablement.
