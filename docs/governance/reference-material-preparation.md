# Approved reference material preparation

This increment prepares retained reference bytes for a trusted local manager. It does not yet mount them into native developer/reviewer sandboxes. The existing quality handoff remains metadata-only.

The preparation service must use the same hierarchy core as artifact retention and quality handoff. Only the references from the exact current approved task/baseline may be read. Preparation checks project/configuration, retained content identity, expiry, byte limits and SHA-256; it rechecks the task binding after reads. It exposes neither arbitrary host paths nor a public artifact-download API.

Prepared files live in a private, temporary directory with server-generated names. File permissions restrict ordinary access; they do not by themselves provide an operating-system read-only sandbox against the owning process. The context owns cleanup on normal exit and exceptions. Abrupt machine termination and persistent orphan cleanup require a separate lifecycle mechanism.

## Evidence boundaries

| Observation | Meaning |
|---|---|
| Reference identity in quality handoff | Approved metadata was supplied. |
| Local material preparation | Retained bytes were verified and staged for the manager. |
| Sandbox readability probe | Still required before claiming worker delivery. |
| Worker file/image access | Requires separate actual tool evidence. |
| Visual or business verification | Requires review evidence against approved criteria. |

No preparation result grants execution, acceptance, production access or visual-verification authority. No model invocation is needed to prepare files.

The local Python interface is `ReferenceBundles(core, artifact_content, private_root).prepare(token, task_id, role, candidate_digest=None)`, used as a context manager. The private root must already exist with owned 0700 permissions. The result holds generated filenames and verified hashes; its `delivered`, `viewed`, `execution_authorized` and `os_sandbox_enforced` fields remain false. Reviewer preparation additionally requires the exact frozen candidate digest.

## Validation

Ten new tests cover byte/hash integrity, private modes, cleanup on read/caller errors, expired or revoked authority during reads, count/aggregate limits, changed handoff identity, root/symlink constraints, descriptor cleanup failures and exact reviewer candidate binding. Together with existing quality handoff, baseline, artifact-retention and native-prompt regression tests, 42 tests passed. An independent reviewer separately ran the initial nine new tests plus nine handoff tests (18 passed) and confirmed the preparation-only boundary. No model calls or native mount probes were performed.

## Next integration gate

The native transport currently denies the temporary directory and control paths. A path in a prompt is insufficient. Future integration must register a trusted provider, mount only the exact staged directory read-only, revalidate current task/reference identity during execution, and run the actual command confidentiality probe with those mounts for both developer and reviewer. Test reference reads, blocked writes/chmod/create, denied sibling/parent/control/auth access and cleanup. Do not reuse a confidentiality result obtained without reference mounts.

Persistent hosting, independent external backup and control of existing Codex UI chats remain separate integration work. Preparing materials does not establish 90% always-on control.
