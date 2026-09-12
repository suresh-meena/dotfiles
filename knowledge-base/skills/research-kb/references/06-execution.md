# Optional execution: designs, launches, resources, and reconciliation

This reference defines the optional execution module for projects that need controlled launches: immutable design and trial identity, provenance gates, transactional dispatch, idempotency, resource leases, reconciliation, cancellation, and failure tests.

## Availability and boundary

The knowledge core must operate without this module. Importing existing runs and their evidence is useful before implementing any launcher. The conservative default profile has execution off; enable launch/cancel tools only after their safety and recovery tests pass and through explicit reviewed configuration.

- Do not depend on execution to record knowledge, evidence, or progress; use [retrieval](04-retrieval.md) and [progress and publication](05-progress-and-publication.md) without it.
- Check the runtime's capabilities before assuming launch tools exist. If the module is disabled, unimplemented, or unauthorized, say so and return an uncommitted proposal instead of simulating execution.
- Never expose an unrestricted shell or SQL operation as an execution adapter (see [safety and operations](07-safety-and-operations.md)).
- Execution commands, receipts, and errors follow the envelope in [tool contracts](08-tool-contracts.md).

## Study and execution lifecycles

Scientific study lifecycle:

```text
planned -> active -> concluded
    \        \------> abandoned
     \--------------> abandoned
```

Readiness, blocker state, execution activity, and required coverage are derived separately. Concluding a study is an explicit evidence-backed operation, not an automatic consequence of the last process exiting.

Execution-attempt lifecycle:

```text
queued -> starting -> running -> completed | crashed | cancelled
```

- A missing worker/process can produce `lost` only after a reconciler records why its outcome cannot be established.
- Temporary unreachability is the observation `contact_unknown`, not immediate scientific invalidation.
- A verified late executor receipt reconciles a lost record through a new audited revision. Never overwrite history or create a second attempt for the same execution identity.
- Scientific validity (`unknown|valid|suspicious|invalid`) is independent of execution status; `completed+invalid` is allowed, and validity checks themselves have versioned results and reasons.
- Partial outputs of a crashed run can be registered as partial artifacts, but they do not satisfy full-trial coverage without a separately defined partial-output criterion.

## Immutable designs and trial keys

Freeze a complete study specification before execution: protocol revision, condition design, required outputs/metrics, analysis/selection rules, resource constraints, and completion criteria. Store every specification revision, not only the latest number.

- Support explicit sparse condition rows as well as Cartesian products with constraints. Validate the expanded count and enforce a configurable expansion cap before allocating millions of accidental slots.
- Adaptive studies add a reviewed design revision or explicit amendment; they never retroactively alter old coverage.
- A retry keeps the same trial identity and gets a new attempt number. A materially changed protocol, resolution, dataset, seed policy, or scientific configuration is a different condition/specification, not an OOM-recovery retry that silently replaces the intended result. Compatibility-preserving operational changes must be declared and checked.

A trial key is the hash of a versioned canonical representation of:

```text
project identity + study identity + study revision
+ normalized scientific conditions + replicate identity
```

- Array order remains significant unless the schema explicitly defines a set.
- Resolve defaults before hashing. Preserve null-versus-absent distinctions, normalize declared units, and encode exact scientific decimals and large integers as typed strings where necessary. Reject NaN, infinity, and unsupported values.
- Use a specified canonicalization implementation, not an undocumented `json.dumps(sort_keys=True)` approximation. RFC 8785 defines a JSON canonicalization scheme with numerical constraints; record the fingerprint format/version and test published vectors or project-specific fixtures.
- Trial slot conditions and their types are defined in [data model](02-data-model.md); do not add unvalidated condition keys.

Coverage is the number of required slots with eligible selected evidence under the specified policy, not the number of run rows. Return the exact missing/ineligible slots and the study revision defining the denominator. Deliberate replications are distinct slots, not accidental duplicate valid attempts.

## Launch manifest

The manifest is immutable and must contain every category below:

| Category | Required fields |
|---|---|
| Scientific identity | Project/study revision, trial key, attempt, protocol and policy revisions |
| Invocation | Exact argument vector, executable identity, working directory, resolved nonsecret configuration and hash |
| Code | Repository identity, commit, dirty-state flag, exact execution-source snapshot including relevant untracked files when allowed |
| Inputs | Dataset/artifact/protocol revisions, content fingerprints/manifests, identity assurance and verification |
| Environment | Interpreter, dependency lock/container digest, drivers/libraries, OS/architecture, thread/precision settings |
| Randomness | Generator/seed/state policy, or explicit `not_applicable` for deterministic methods |
| Placement | Worker/machine identity, scheduler allocation, physical GPU UUIDs or partition identities where relevant |
| Outputs | Expected result schema/version, artifact destinations, tracker identities, log locations |
| Authorization | Approved proposal/launch token, resource limits, budget/walltime, narrow exceptions if any |

Record argument arrays, not a shell string requiring interpretation. Never include secret values; store credential references and redaction markers. Redaction must not pretend a secret-dependent computation is fully reproducible without the required authorized credential.

Capture the code/configuration actually executed, not merely the Git state observed while planning. Use an immutable checkout/package and have the worker verify its identity. A dirty-tree patch alone can omit untracked files, submodules, LFS objects, generated inputs, or environment-dependent imports; handle those explicitly or mark provenance incomplete. Prefer immutable manifests for dataset fingerprints; do not rehash a large immutable dataset on every launch, and never label file size or mtime a content hash.

## Provenance gates

A provenance profile declares the fields required for this method and intended use.

- Paper-critical promotion or launch fails closed on missing required fields, unknown input identity, blocking applicability, unapproved design, or unauthorized duplication.
- Exploratory imports remain recordable with `unknown` provenance rather than being lost.
- Prefer a clean immutable code snapshot for critical work. A fully captured dirty snapshot requires an explicit policy; a reason string alone does not authorize an override.
- Deterministic work does not need an invented seed. A diagnostic experiment may receive a narrow exception tied to the blocking issue, operation, study revision, reason, and expiry; it does not waive the blocker for paper conclusions.

## Launch transaction and dispatch

A database transaction cannot atomically commit both SQLite rows and an arbitrary external process launch. Persist launch intent and required database state in one transaction, then dispatch through an outbox. Do not promise universal exactly-once physical execution; promise one logical operation per idempotency key, durable intent, deduplicated dispatch where the executor supports it, and explicit reconciliation of ambiguous outcomes.

1. Validate the approved study, input identities, trial eligibility, permissions, resource limits, and applicable blockers.
2. In one short database transaction, claim the trial attempt, create the run/manifest reference, reserve controller-managed resources if needed, append an outbox intent, audit, and record the idempotent response.
3. Commit before contacting an external worker or scheduler. Never hold a SQLite write lock while hashing a large dataset, calling a model, or waiting for a job launch.
4. Dispatch the stable execution ID and manifest to the adapter. The adapter durably associates that execution ID with a process/job reference before acknowledging whenever its platform permits it.
5. The worker rechecks the launch token's revision/policy binding and expiry, verifies immutable inputs, and reports its acknowledgment. Manifests and retrieved worker text are data, not instructions.
6. Persist acknowledgment and meaningful execution transitions; retry delivery of receipts safely.
7. On exit, ingest structured outputs, verify declared schemas/checksums, assess validity, and reconcile resource release.

For schedulers without idempotent submission, an ambiguous submission must be searched or reconciled by the stable execution ID or held for operator review. Blindly resubmitting after a timeout is unsafe. A local process wrapper needs an execution registry and process identity checks; a claimed unique job name is insufficient. A stored approval must expire or be invalidated when its bound specification, policy, critical blockers, or resource budget changes.

## Idempotency rules

- Scope a request ID to the authenticated actor, project, and operation. Store a canonical payload hash and durable outcome.
- An identical retry returns the same logical operation/result. The same ID with a different payload returns `IDEMPOTENCY_CONFLICT` and performs no side effect.
- A launch timeout returns an operation/execution ID with `pending` or `unknown`, not an invented success/failure. Clients reconcile that operation before requesting a new one.
- Keep idempotency records at least as long as their associated scientific operations can be retried; do not evict them while duplicate effects remain possible.
- On `EXECUTION_PENDING` or `EXECUTION_AMBIGUOUS`, reconcile the same execution ID; do not launch another attempt.

## Resources and leases

Separate durable inventory, latest observations, and allocations. A resource has a stable machine/device identity, capabilities, capacity, admin state, known limitations, and an optional parent allocation domain. CPU-only machines are valid. Heterogeneous GPU cards are individual resources, not one uniform machine-level model string.

- Use physical UUIDs/partition identities rather than only device indices, which can change after reboot or remapping.
- If GPU partitioning/sharing is supported, define mutually exclusive allocation domains and capacity rules explicitly. Default to exclusive allocations; do not invent fractional sharing from "free VRAM."
- For Slurm or another scheduler, its allocation is authoritative for physical placement; local leases coordinate intended work, not a competing claim to scheduler-controlled hardware.
- For unmanaged machines, state that leases prevent conflicting cooperating launches, not jobs started outside the controller.

Reserve all resources for one attempt atomically, using uniqueness/capacity constraints over active or quarantined allocations rather than an application-level "looks free" check. A lease carries owner, execution ID, generation/fencing token, acquired time, expiry, and state.

**Expiry is not termination proof.** Expiry means the owner may be unresponsive; it does not prove a process stopped using a GPU. Move uncertain allocations to quarantine and verify termination before release or reuse.

Where a worker/storage operation can enforce fencing tokens, reject stale generations. A database token alone cannot fence a running CUDA process. If physical fencing is unavailable, retain the quarantine until reliable process/scheduler evidence or an authorized operator resolves it. Do not release hardware on an expired lease alone.

## Reconciliation

| Observation | Action |
|---|---|
| Worker reachable, matching process/job alive | Refresh observation; keep allocation |
| Trusted exit receipt and matching execution identity | Record terminal transition, ingest outputs, release after checks |
| Process absent and launch was acknowledged | Reconcile with exit logs/scheduler; record crash/lost with reason |
| Worker unreachable | Mark contact unknown, stop new placement there, quarantine expired allocations |
| Worker rebooted | Compare boot ID and old job identities; reconcile before reusing stale allocations |
| PID reused | Reject identity match unless PID start time, boot ID, and process group also match |
| Late or repeated receipt | Deduplicate; accept only for the correct execution/generation and audit reconciliation |
| Imported running job not owned by controller | Observe/import only; do not assume permission to stop or adopt it |

Use controller receipt time for freshness and include worker clock/boot metadata for diagnosis. Do not compare unsynchronized worker wall clocks as though they impose a total event order. Retain meaningful transitions; keep dense heartbeats and logs external.

## Cancellation

Cancellation is a requested operation until the executor verifies termination. A successful "cancel request accepted" response must not be rendered as "job stopped." Preserve resulting partial artifacts and the actual final outcome, and do not release resources on the basis of the request alone.

## Failure injection before enabling launch

Test all of the following before enabling launch or cancel:

- Duplicate requests; simultaneous same-trial claims; simultaneous multi-resource reservations; changed-payload retries.
- Kill the controller before reservation, after reservation, after outbox commit, before acknowledgment, and after external launch but before receipt persistence.
- Disconnect a worker while its job continues; expire its lease; verify resources remain unavailable until termination is established.
- Reboot a worker; reuse a PID; deliver old-generation heartbeats; deliver terminal receipts out of order.
- Restore an older backup while external jobs still run.
- Change the study/configuration after approval; modify files after manifest capture; omit relevant untracked source files; return a result with the wrong schema or hash.
- Fill the disk during ingestion; request cancellation without permission; import an externally owned job; simulate scheduler submission without idempotent support.

Required outcomes: precise logical operation identity, no unsafe silent retries, no automatic reuse of uncertain physical allocations, preserved partial artifacts, and accurate phase-specific receipts — not an unsupported claim of exactly-once physical execution.
