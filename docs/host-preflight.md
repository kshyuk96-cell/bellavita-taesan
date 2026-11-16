# Local host preflight

`python3 scripts/check_governance_host.py identity` measures a local kernel boot/hostname fingerprint. It does not verify an expected host. Supply an independently established expected fingerprint to `inspect --expected-host-identity HEX --state-root /absolute/private/root`. A requested fingerprint never becomes an observed identity. Matching this local fingerprint is not cloud or Render attestation; an optional Render service environment match remains environment evidence only.

The state root must already exist, be owned by the running user, have mode 0700, and contain no symlink ancestors. Inspection performs the existing bounded namespace isolation and private fsync probes. It starts no models, deploys nothing, and offers no plain-process fallback when namespace isolation is unavailable.

Run `restart-proof` with the same explicit arguments in a new private root to create a mode0600 fsynced challenge, wait for its writer process to exit, launch a distinct reader process, and wait for the reader to exit. The artifact is preserved and cannot be overwritten. Standalone `prepare` and `verify` support inspection of this boundary, but `verify` alone reports only a separate-process read. The digest detects accidental edits; it is not protection against the trusted directory owner replacing the artifact.

This proves local state survives two actual process lifetimes on the measured host. It does not prove a machine reboot, container replacement, disk durability across infrastructure restart, a persistent cloud disk, remote execution, or an always-on supervisor.

`RegisteredWorkerObservations` accepts host-owned live `subprocess.Popen` handles and records the existing exact CodexHost boot/PID/start-time identity. It exposes no public PID selector and sends no signals. Only a process handle's actual exit observation can support `stopped_verified`; PID absence alone is unobserved, PID reuse is explicit, and identity failures remain unknown. Even an observed process exit does not prove descendants stopped or authorize recovery/replay. Existing core reconciliation fences remain authoritative.
