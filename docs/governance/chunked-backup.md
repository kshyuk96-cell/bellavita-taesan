# Bounded encrypted-envelope chunk transport

The existing GitHub recovery adapter caps each object at 1 MiB. A manager database can exceed this before encryption metadata is added. `ChunkedBackupBackend` wraps a registered `BackupBackend` without changing that per-object limit or silently enabling production backups.

The ten transport boundaries are: bounded chunk size; bounded total size/count; immutable backup-ID claim; distinct generated part IDs; per-part length/hash verification; whole-envelope hash verification; exact bounded manifest schema; manifest publication only after all parts; publication recheck after reconstruction; and no replay/deletion after uncertain transfers.

Defaults are 512 KiB parts, at most 64 parts and a 32 MiB envelope. The maximum configured payload cannot exceed the envelope limit or chunk-count capacity. The manifest is at most 64 KiB. Metadata contains no remote URLs or host paths. Reserved claim/part namespaces cannot be chosen as public backup IDs.

A claim is written first using create-only storage. Parts are retained after failures. The final create-only manifest is the publication point. A duplicate request cannot silently generate another part set under the same ID. Unknown partial uploads require explicit reconciliation or a new independently authorized backup ID; they are not rolled back, retried or deleted automatically. Completed legacy single-object backups must retain their original backend registration; no ambiguous fallback is added.

This wrapper transports opaque bytes; it does not encrypt them or authenticate their authorship. SHA256 consistency alone does not defeat a malicious store that rewrites all content. `EncryptedBackup` AEAD and the independently authenticated recovery catalog must remain in use. Hash/length checks and the final manifest recheck detect inconsistent or changed content, not durable provider retention, key custody or whole-history rollback.

Tests cover ordered roundtrip, create-only publication, duplicate/partial transfer handling, corrupt/missing/reordered parts, pre-read size limits, strict manifests, reserved keys, changed publication, actual private-file reconstruction above 1 MiB and an encrypted synthetic database restored into quarantine through the existing backup service. External tests use large synthetic ciphertext only. Production backup activation and durable independent keys remain separate gates.
