-- Empty isolated receiver schema. Reference only; no production data.
BEGIN;
CREATE TABLE order_ledger (
        order_id TEXT PRIMARY KEY, details TEXT NOT NULL, state TEXT NOT NULL, payment TEXT NOT NULL DEFAULT '{}');

CREATE TABLE production_queue (
        order_id TEXT PRIMARY KEY, details TEXT NOT NULL, state TEXT NOT NULL, guard TEXT NOT NULL DEFAULT '[0,0]');

CREATE TABLE received_events (
        event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL,
        digest TEXT NOT NULL, ack_json TEXT, acked INTEGER NOT NULL DEFAULT 0, guard TEXT NOT NULL DEFAULT '[0,0]');

COMMIT;
