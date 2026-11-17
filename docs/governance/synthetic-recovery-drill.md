# Runnable synthetic recovery drill

Run `python scripts/rehearse_governance_recovery.py` using the application's installed Python environment. No arguments or external paths are accepted. The tool creates only a fresh private temporary manager store with synthetic identity and project data. It does not inspect the real registry, credentials, source snapshots or customer databases.

The drill encrypts a database backup, publishes an independently authenticated catalog receipt, removes the original synthetic database, and restores into a new database using a short-lived recovery grant. It checks SQLite integrity, the reconciliation quarantine, admission denial and consumed-grant replay rejection. It verifies that known plaintext and the key are absent from the ciphertext and that recovery staging is cleaned. Ephemeral keys and tokens are never printed. The temporary directory is cleaned on exit.

A real-time defect was discovered by running the drill: grant issuance called its clock twice for `issued_at` and `expires_at`. At the default 300-second limit, time advancing between those reads made the lifetime greater than 300 seconds and the grant rejected itself. Issuance now captures one finite positive time and derives expiration from that same instant. A deterministic advancing-clock regression reproduces the former failure.

This is a local synthetic recovery rehearsal. Passing it does not establish external backup storage, key custody after host loss, scheduler availability or a working permanent host. Restored authority remains quarantined; no automatic task replay or production writes occur.
