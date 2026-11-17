# Current source preparation for the manager

The prior private source proofs were not sufficient for current work: ERP had advanced from saved v34 to v47 and then v48 during this inspection; Hope Sunrise had advanced from v39 to v47. The association's main branch also changed. Source preparation therefore uses exact commits and live pre/post source checks rather than assuming a previous successful copy remains current.

| Primary project | Branch / saved version checked | Prepared source commit |
|---|---|---|
| Bellavita shop | `codex/shop-backend` | `d5047f3345b757edfcfbd81a73c609c0392007dd` |
| Bellavita ERP | Sites main, saved/deployed v48 | `8d692430f9b5042c5eee160b91de403abbaddbab` |
| Hope Sunrise | Sites main, saved/deployed v47 | `ac4bca8be5080b1bdc47826bdb98e3e247a49a13` |
| Funeral association | main | `5fa7133d98b2a7f7614f968a5c4c536cdbda5737` |

These are observations at preparation time, not permanent currentness claims. A newer push invalidates a task's old source binding. Saved Sites versions, their deployments and the current Git source head are separate observations; the ERP source head advanced before the saved version list caught up. The matching v48 deployment was subsequently confirmed succeeded.

## Staging contract

`SourceSnapshotStage(core, private_root, current_pin=trusted_callback).stage(token, pin, archive_sha256, observed_at, archive_bytes, strip_prefix=None)` is a local owner/manager API. Registered source kind/ref/branch and current configuration must match. The callback reads the actual remote head; the caller cannot supply a success flag in place of a source observation. Observations expire after 300 seconds, and scope/configuration are checked again after callback I/O.

The stager accepts bounded plain/gzip tar data, verifies the exact approved archive hash, and bounds decompression before parsing tar metadata. It rejects traversal, PAX traversal, links, sparse/device entries, duplicates and credential-bearing paths. Both header count and actual filesystem nodes, including implicit directories, are bounded; path depth is capped at 32. Files are private data (0600), directories 0700. A fresh server-generated directory is published without replacement; source/config/auth drift after publication removes it.

GitHub archives were retrieved for exact remote commits. Sites archives could not be resolved through the upload-download tool, so the supported Sites Git source helper fetched into new private checkouts using short-lived access passed only through hidden stdin. No push or publication was requested. Credentials were not saved in source, commands or artifacts; expired access failed closed and was refreshed before successful live checks.

The association's `.env.example`, `.env.production.example` and `deploy/staging/.env.example` were explicitly omitted from a derived staging archive. Its original and derived hashes are distinct and its `full_original_source` flag is false. Other three snapshots have no filename exclusions. This filename policy is not an audit of arbitrary embedded credentials.

Four prepared trees were independently reread and their per-file tree hashes, sizes, depth, node counts and private modes checked. Evidence is `source-snapshot-evidence-20261117.json`. Older working copies remain untouched. These snapshots are not Git-backed worker checkouts, tested builds, credential-audited delivery bundles, worker activation or deployment. The generic stager does not independently prove Git commit contents.

## Hosting boundary

The user confirmed Simba's existing Render workspace. Read-only inventory found seven services, including an ERP production background worker and customer/review web services. No service was changed or repurposed as the manager. Workspace reuse does not prove an existing service qualifies as a native coding host. Additional ERP backend/runtime components require separate source binding from the primary Sites snapshot.

The desired private recovery repository still returned 404/not accessible at inspection. Persistent manager-host qualification, offsite recovery and production activation remain separate gates. A one-time cloud-scheduler workspace-access probe was registered without modifying existing schedules; its result must be verified separately before claiming unattended access, cold restart or always-on operation.
