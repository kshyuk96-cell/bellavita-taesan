# Trusted source archive handoff

`SourceSnapshot(project, source_kind, source_ref, branch, sha, revision, archive_digest)` pins a trusted provider's exact project/source/version and the expected SHA256 of its archive. GitHub references use repository form; Sites references keep their actual `appgprj_…` identifier. The adapter does not infer or invent an origin.

`prepare_source_snapshot(snapshot, archive_path, owned_root, max_bytes=128*1024*1024, max_files=20000)` accepts only a typed snapshot and trusted local paths. It copies and hashes the input into a private staging area, validates the complete bounded archive, extracts regular files into a separate owned project/version directory, and publishes a separate `source-manifest.json` atomically. The version key includes the entire snapshot identity and archive digest. A changed source revision or archive produces a separate version; existing versions are never overwritten.

Supported archives are tar, gzip-compressed tar and ZIP with stored/deflated files. Both archive bytes and expanded tar/payload sizes are bounded. ZIP central-directory metadata and entry counts are checked before allocating ZIP entry objects. Explicit entries and implicit parent directories are bounded; duplicate paths, case/Unicode collisions, conflicting parent spellings, file/directory collisions, traversal, absolute/drive paths, links, sparse/special files, encrypted ZIPs, Git metadata, bundled `node_modules`, and common credential configuration paths fail closed before source extraction. Wrapper directories are preserved rather than silently stripped.

The handoff base, project and version roots are owned private directories. Version roots use mode 0700; extracted directories/files are normalized read-only, and the manifest is separate from the source tree. The manifest records actual archive bytes, SHA256 of every source file, modes, directory inventory, file count, total bytes and a canonical tree digest. Repeated preparation validates the archive again in bounded staging, then compares the full regenerated manifest and tree with the existing version. It cannot promote an altered cache manifest into Git or worker authority.

Archive attributes, `.gitattributes`, scripts and hook-like source files are inert bytes. No Git, package manager, hook, source synchronization, customer command or network request is executed. No original source directory is modified.

## Provenance and worker boundary

Every result identifies `source_form: provider_archive`, `actual_git_clone: false`, `git_origin_verified: false`, `provider_revision_independently_verified: false`, `worker_ready: false`, and `approval_granted: false`. The provider revision is a trusted caller assertion; actual archive/tree hashes independently describe the bytes received. A source SHA in metadata does not make this directory a real Git checkout or prove current remote freshness.

Existing CodexHost origin and clean-Git checks still require a separately prepared real clone where applicable. Sites exports must retain Sites provenance and their version identifier. Do not add fake GitHub remotes to satisfy that worker boundary. SourceWatch observers may be registered later only by trusted Python host configuration after exact provider/source checks; this adapter neither registers observers nor accepts public clients choosing paths, hashes or revisions.

Missing Sites archives remain missing handoff prerequisites. An observed version number, available source metadata or published site does not fabricate an archive, a live worker connection or current QA acceptance. No Sites write credential is requested or consumed.
