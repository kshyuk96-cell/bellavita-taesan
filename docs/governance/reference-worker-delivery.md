# Approved reference files in native workers

The trusted continuous-host configuration may register:

```json
"reference_materials": {
  "artifact_root": "/private/retained-reference-data",
  "preparation_root": "/private/reference-preparation"
}
```

These directories must already exist with private owner permissions. They must be separate from sources, candidates, runtime, application, profiles and manager control resources. Configuration does not import or approve any reference. Retained content and an exact owner-approved business baseline are prerequisites.

Only the trusted manager provider may prepare reference bytes. Worker HTTP input never supplies an output path, filesystem policy or artifact body. Developer and independent reviewer receive their own task/candidate-bound contexts. References are inert data; embedded instructions grant no authority.

Before any native model execution involving references, the transport requires a fresh actual command isolation probe for the exact prepared mount. It tests both developer and reviewer policies. The probe checks reference hash reads and blocked writes, mode changes and file creation, while preserving existing control/profile/Git/network restrictions. Preparation identity and live authority are checked again during execution. A prior probe without reference mounts is insufficient.

The isolation tool creates empty ancestor directories to reach allowed mounts. Their listing can expose the fixed policy's child components, including explicit denied-path placeholders; this is not complete ancestor invisibility. The probe permits only the exact immediate child names computed from the normalized filesystem policy. Any additional real host child name fails the probe. Actual forbidden children, synthetic sibling files and private resource contents must remain inaccessible. Reference-enabled receipts distinguish sanitized ancestor scaffolding from hidden real parent contents instead of claiming the ancestor cannot be listed.

The existing handoff remains unchanged. Additional reference metadata identifies staged files and their hashes without treating these as viewed images, verified business output or execution authority. Files are removed when their preparation context exits, including failures. A native command's ability to read a file is evidence of filesystem delivery, not proof that a model opened or understood it. No model-quality score follows from this probe.

This feature does not attach images through the native image-input option, deploy a daemon, create paid resources, connect existing Codex UI conversations, run the external tester engine or authorize customer data writes. Always-on operation and offsite recovery remain separate integration gates.

## Validation evidence

Actual non-model probes used fresh empty coding profiles and synthetic retained material. Both an approved developer context and an independently frozen reviewer context passed both filesystem policies: four sandbox runs in total. File reads matched the approved SHA; write, chmod and file-creation attempts failed; real sibling/private contents stayed hidden; Git writes and network access were blocked. Sanitized evidence is `reference-native-probe-20261116.json`. This establishes filesystem accessibility and read-only enforcement for the tested environment, not model viewing or a guarantee for another host.

An independent reviewer reran 51 related tests and found no remaining confirmed blockers. The review reproduced a same-inode replacement case; the fixed mount identity includes directory/file change and modification times plus post-read named-file checks. A separate test rejects unexpected parent-directory names. No model inference or operational deployment was used for these checks.

Final whole-repository regression: **2149 tests passed in 247.322 seconds**. This includes 17 new tests in this increment and expanded host-loader registration coverage. Existing prompt-only fixtures use no reference bodies; a dedicated case verifies that approved references without a registered delivery provider prevent model startup.
