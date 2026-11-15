-- Reference only. Generated from an EMPTY isolated PR database; no production data.
-- Do not apply as a migration to an existing database.
BEGIN;
CREATE TABLE access_requests (
        id TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        created REAL NOT NULL, decision TEXT);

CREATE TABLE alert_account (singleton INTEGER PRIMARY KEY, refresh TEXT);

CREATE TABLE alert_link (digest TEXT PRIMARY KEY, expires REAL);

CREATE TABLE alert_links (id TEXT PRIMARY KEY, url TEXT NOT NULL);

CREATE TABLE alert_outbox (
        id TEXT PRIMARY KEY, message TEXT NOT NULL, created REAL NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, retry REAL NOT NULL DEFAULT 0);

CREATE TABLE approvals (
        job TEXT PRIMARY KEY, decision TEXT NOT NULL, actor TEXT NOT NULL, decided REAL NOT NULL);

CREATE TABLE development_focus (owner TEXT PRIMARY KEY, job TEXT NOT NULL);

CREATE TABLE development_signatures (nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL);

CREATE TABLE development_work (
        job TEXT PRIMARY KEY, detail TEXT NOT NULL, approved REAL NOT NULL,
        completed REAL, result TEXT);

CREATE TABLE enrollment (
        digest TEXT PRIMARY KEY, role TEXT NOT NULL, expires REAL NOT NULL);

CREATE TABLE invitation_input (
        owner TEXT PRIMARY KEY, expires REAL NOT NULL, details TEXT NOT NULL);

CREATE TABLE invitation_orders (
        id TEXT PRIMARY KEY, job TEXT NOT NULL, price INTEGER NOT NULL, created TEXT NOT NULL,
        details TEXT NOT NULL, state TEXT NOT NULL, payment_number TEXT UNIQUE, payment_url TEXT);

CREATE TABLE invitation_photo_reviews (
        order_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL);

CREATE TABLE jobs (
        id TEXT PRIMARY KEY, owner TEXT, fingerprint TEXT, created REAL,
        state TEXT, input TEXT, result TEXT);

CREATE TABLE order_changes (order_id TEXT, owner TEXT, changed TEXT,
                 before TEXT, after TEXT);

CREATE TABLE order_operations (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL,
        raw TEXT NOT NULL, details TEXT NOT NULL, state TEXT NOT NULL, order_number INTEGER,
        retry_after REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);

CREATE TABLE order_review (order_id TEXT PRIMARY KEY, owner TEXT, checked TEXT);

CREATE TABLE order_sms (order_id TEXT, owner TEXT, state TEXT, receipt TEXT,
                 PRIMARY KEY(order_id,owner));

CREATE TABLE order_staff_notify (owner TEXT PRIMARY KEY, center TEXT, phone TEXT UNIQUE);

CREATE TABLE order_staff_web (digest TEXT PRIMARY KEY, owner TEXT, expires REAL, kind TEXT);

CREATE TABLE payment_checks (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, payment_number TEXT NOT NULL,
        amount INTEGER NOT NULL, note TEXT NOT NULL, checked TEXT NOT NULL);

CREATE TABLE payment_events (
        order_id TEXT NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL,
        payment_number TEXT NOT NULL, amount INTEGER NOT NULL,
        provider_time TEXT NOT NULL, received TEXT NOT NULL, previous_state TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(order_id, event_key));

CREATE TABLE route_points (
        address TEXT PRIMARY KEY, latitude REAL NOT NULL, longitude REAL NOT NULL, checked REAL NOT NULL);

CREATE TABLE route_sessions (
        owner TEXT PRIMARY KEY, changed REAL NOT NULL, settings TEXT NOT NULL);

CREATE TABLE shop_amendment_events (
        id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,amendment_id TEXT NOT NULL,decision TEXT NOT NULL,
        actor TEXT NOT NULL,reviewer TEXT NOT NULL,note TEXT NOT NULL,changed TEXT NOT NULL);

CREATE TABLE shop_amendments (
        id TEXT PRIMARY KEY,order_id TEXT NOT NULL,base_revision INTEGER NOT NULL,
        changes TEXT NOT NULL,state TEXT NOT NULL,requester TEXT NOT NULL,note TEXT NOT NULL,created TEXT NOT NULL);

CREATE TABLE shop_artifact_reviews (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, version INTEGER NOT NULL,
        decision TEXT NOT NULL, reviewer TEXT NOT NULL, actor TEXT NOT NULL,
        note TEXT NOT NULL, checked TEXT NOT NULL);

CREATE TABLE shop_artifacts (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, source_hash TEXT NOT NULL,
        layout TEXT NOT NULL, png_hash TEXT NOT NULL, pdf_hash TEXT NOT NULL,
        state TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(order_id,version), UNIQUE(order_id,source_hash));

CREATE TABLE shop_assignment_accepts (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
        note TEXT NOT NULL, accepted TEXT NOT NULL, PRIMARY KEY(order_id,version));

CREATE TABLE shop_assignment_batches (
        id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,
        result TEXT,actor TEXT NOT NULL,created TEXT NOT NULL,applied TEXT);

CREATE TABLE shop_assignment_history (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, owner TEXT NOT NULL,
        name TEXT NOT NULL, note TEXT NOT NULL, actor TEXT NOT NULL, changed TEXT NOT NULL,
        PRIMARY KEY(order_id,version));

CREATE TABLE shop_assignments (
        order_id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
        version INTEGER NOT NULL, note TEXT NOT NULL, updated TEXT NOT NULL);

CREATE TABLE shop_backup_runs (
        id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL,
        state TEXT NOT NULL, snapshot TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');

CREATE TABLE shop_cash_receipts (
        order_id TEXT NOT NULL, generation INTEGER NOT NULL, amount INTEGER NOT NULL,
        state TEXT NOT NULL, cashstno TEXT, receipt_no TEXT, url TEXT,
        error TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
        PRIMARY KEY(order_id,generation));

CREATE TABLE shop_delivery_confirms (
        order_id TEXT NOT NULL, photo_id TEXT NOT NULL, photo_version INTEGER NOT NULL,
        actor TEXT NOT NULL, confirmed TEXT NOT NULL, PRIMARY KEY(order_id,photo_id,photo_version));

CREATE TABLE shop_delivery_photo_changes (
        id INTEGER PRIMARY KEY, photo_id TEXT NOT NULL, version INTEGER NOT NULL,
        state TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, changed TEXT NOT NULL);

CREATE TABLE shop_delivery_photos (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL, sha256 TEXT NOT NULL,
        state TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
        note TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(order_id,sha256));

CREATE TABLE shop_diagnostic_runs (
        id INTEGER PRIMARY KEY, created TEXT NOT NULL, actor TEXT NOT NULL, report TEXT NOT NULL);

CREATE TABLE shop_external_backups(id TEXT PRIMARY KEY,provider TEXT NOT NULL,sha256 TEXT NOT NULL,verified TEXT NOT NULL,mode TEXT NOT NULL);

CREATE TABLE shop_feature_favorite_events(feature_id TEXT NOT NULL,version INTEGER NOT NULL,favorite INTEGER NOT NULL,changed TEXT NOT NULL,PRIMARY KEY(feature_id,version));

CREATE TABLE shop_feature_favorites(feature_id TEXT PRIMARY KEY,version INTEGER NOT NULL,favorite INTEGER NOT NULL,updated TEXT NOT NULL);

CREATE TABLE shop_fulfillment (
        order_id TEXT PRIMARY KEY, stage TEXT NOT NULL, version INTEGER NOT NULL, updated TEXT NOT NULL);

CREATE TABLE shop_handoff (
        event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL,
        payload TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
        consumer TEXT, lease_digest TEXT, lease_until REAL, receipt TEXT, created TEXT NOT NULL,
        acked TEXT);

CREATE TABLE shop_launch_checks(version INTEGER PRIMARY KEY,author TEXT NOT NULL,note TEXT NOT NULL,checks TEXT NOT NULL,created TEXT NOT NULL);

CREATE TABLE shop_message_checks (
        id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, decision TEXT NOT NULL,
        note TEXT NOT NULL, checked TEXT NOT NULL);

CREATE TABLE shop_messages (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL, event TEXT NOT NULL, audience TEXT NOT NULL,
        recipient TEXT NOT NULL, state TEXT NOT NULL, image_id TEXT, group_id TEXT, message_id TEXT,
        error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, checked REAL NOT NULL DEFAULT 0, channel TEXT NOT NULL DEFAULT 'mms', request_json TEXT);

CREATE TABLE shop_notification_settings (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), phones TEXT NOT NULL);

CREATE TABLE shop_offers (
        id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
        price INTEGER NOT NULL, details TEXT NOT NULL, active INTEGER NOT NULL);

CREATE TABLE shop_operator_handover(order_id TEXT NOT NULL,version INTEGER NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(order_id,version));

CREATE TABLE shop_operator_task_events(task_id TEXT NOT NULL,version INTEGER NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(task_id,version));

CREATE TABLE shop_operator_tasks(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,title TEXT NOT NULL,owner TEXT NOT NULL,due TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,note TEXT NOT NULL,updated TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal');

CREATE TABLE shop_partial_reviews (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        refunded INTEGER NOT NULL, remaining INTEGER NOT NULL, decision TEXT NOT NULL,
        reviewer TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, checked TEXT NOT NULL);

CREATE TABLE shop_product_changes(product_id TEXT NOT NULL,revision INTEGER NOT NULL,before_details TEXT NOT NULL,after_details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(product_id,revision));

CREATE TABLE shop_product_notes(product_id TEXT NOT NULL,version INTEGER NOT NULL,author TEXT NOT NULL,note TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(product_id,version));

CREATE TABLE shop_products (
        id TEXT PRIMARY KEY, details TEXT NOT NULL, revision INTEGER NOT NULL);

CREATE TABLE shop_receipt_checks (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, generation INTEGER NOT NULL,
        decision TEXT NOT NULL, note TEXT NOT NULL, checked TEXT NOT NULL);

CREATE TABLE shop_receipt_payments (
        order_id TEXT PRIMARY KEY, pay_type TEXT NOT NULL, paid_at TEXT NOT NULL);

CREATE TABLE shop_response_favorite_events(template_key TEXT NOT NULL,version INTEGER NOT NULL,favorite INTEGER NOT NULL,changed TEXT NOT NULL,PRIMARY KEY(template_key,version));

CREATE TABLE shop_response_favorites(template_key TEXT PRIMARY KEY,version INTEGER NOT NULL,favorite INTEGER NOT NULL,updated TEXT NOT NULL);

CREATE TABLE shop_response_template_events(template_id TEXT NOT NULL,version INTEGER NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(template_id,version));

CREATE TABLE shop_response_templates(id TEXT PRIMARY KEY,name TEXT NOT NULL,body TEXT NOT NULL,active INTEGER NOT NULL,version INTEGER NOT NULL,updated TEXT NOT NULL);

CREATE TABLE shop_saved_searches(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,filters TEXT NOT NULL,created TEXT NOT NULL);

CREATE TABLE shop_service_cases (
        id TEXT PRIMARY KEY,order_id TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,
        assignee TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);

CREATE TABLE shop_service_events (
        case_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,assignee TEXT NOT NULL,
        note TEXT NOT NULL,actor TEXT NOT NULL,changed TEXT NOT NULL,PRIMARY KEY(case_id,version));

CREATE TABLE shop_service_photos (
        id TEXT PRIMARY KEY,case_id TEXT NOT NULL,order_id TEXT NOT NULL,sha256 TEXT NOT NULL,
        note TEXT NOT NULL,actor TEXT NOT NULL,created TEXT NOT NULL,UNIQUE(case_id,sha256));

CREATE TABLE shop_shift_snapshots(id TEXT PRIMARY KEY,title TEXT NOT NULL,author TEXT NOT NULL,owner TEXT NOT NULL,until_date TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL);

CREATE TABLE shop_stage_history (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, stage TEXT NOT NULL,
        actor TEXT NOT NULL, changed TEXT NOT NULL, PRIMARY KEY(order_id,version));

CREATE TABLE staff (
        owner TEXT PRIMARY KEY, role TEXT NOT NULL, enrolled REAL NOT NULL);

CREATE TABLE staff_report_shortcuts (owner TEXT PRIMARY KEY);

CREATE UNIQUE INDEX shop_cashstno ON shop_cash_receipts(cashstno) WHERE cashstno IS NOT NULL;

CREATE INDEX shop_handoff_order ON shop_handoff(order_id,created);

CREATE INDEX shop_order_job ON invitation_orders(job, created);

CREATE INDEX shop_task_order ON shop_operator_tasks(order_id,state,due);

COMMIT;
