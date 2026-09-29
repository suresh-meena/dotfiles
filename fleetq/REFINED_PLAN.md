# fleetq (`fq`) — refined implementation and operations plan

**Review date:** 23 September 2026  
**Basis:** `PLAN(5).md`, all 524 source lines, including formatting lines.  
**Status:** proposed replacement specification, not an implemented or hardware-verified system.  
**Companion:** `REVIEW_LEDGER.md` maps every original line to its disposition and the replacement section.  
**Evidence notation:** `P:Lx–Ly` refers to the uploaded plan. `[Rnn]` refers to the primary-source registry at the end. All defaults introduced below are design proposals, not measurements or university policy.

## 0. Context, decisions, and boundaries

### 0.1 Preserve the goal

Run one always-on scheduler on numpi for trusted research jobs across the workstations and university Slurm clusters. Keep fleetctl an independent subprocess interface, keep fleetqd separate from fleetmon, and keep clusters free of an installed fleetq service or persistent login-node helper. A job-scoped wrapper, ordinary submission files, and bounded one-shot login commands are permitted; a resident controller on a login node is not. Human-initiated cluster onboarding is a production action, never a development test.

Keep snapshot-by-default submissions; `--in-place` requires explicit destinations. Keep `--on a,b` as one execution on one alternative and `--each a,b` as separate grouped jobs. Keep UUID-based workstation allocation and per-job cluster opt-in. These are retained user decisions, not reopened choices. [P:L20–L28]

The reported machine count, free space, audit-call count, fleetctl implementation details, university restrictions, filesystem problems, accounts, and site-specific Slurm behavior are **source assertions pending local verification**. No fleetctl or fleetmon repository, host configuration, audit log, or administrator policy was supplied separately. Nothing in this review establishes that those assertions are still true.

### 0.2 Explicit amendments to the original decisions

| Area | Refined decision |
|---|---|
| Runtime | Python 3.11+ for daemon, client, and shim; stdlib-only client remains a single file. This deliberately replaces the client's Python 3.9 minimum, avoiding an undeclared TOML parser dependency. |
| Workstation execution | systemd user services only in supported v1; working linger and demonstrated cgroup memory enforcement required. No automatic `setsid` fallback. |
| Shared-node promise | Conservative, cooperative placement based on observations, not exclusive ownership against outsiders. No hostile multi-tenant isolation claim. |
| Delivery guarantee | At most one possibly-live attempt per logical job; durable per-attempt launch claims; uncertain operations stop automatic replacement. No universal exactly-once side-effect guarantee. |
| Slurm recovery | A job name is a lookup label, not a deduplication primitive. Ambiguous submissions enter `SUBMISSION_UNKNOWN`; three empty lookups do not authorize resubmission. |
| Poll ownership | fleetqd owns the managed cluster's queue snapshot and publishes it to fleetmon. A managed cluster has no second fleetmon queue poller. |
| Budgets | Central authority on numpi for participating clients, keyed by canonical cluster/controller, with separate RPC, session, and transfer counters. Local files alone are not a distributed budget. |
| Transport | Prefer loopback API behind tailnet-only HTTPS using Tailscale Serve. A CLI-only tailnet HTTP mode requires an explicit deployment decision. |
| Rollout | Safety and recovery precede all live execution. Slurm comes before shared-node support after the exclusive-node canary, addressing the original login-polling problem sooner. |

`tomllib` first appears in Python 3.11. [R12] Reuse compatible dependencies, not fleetmon's lockfile blindly: fleetq owns a separately validated lockfile and records Python's linked SQLite version. “Python is a practical fit” replaces the unmeasured assertion that another language offers no gain.

### 0.3 Supported scope and deliberate exclusions

V1 supports trusted batch commands, one machine per job, zero or more whole NVIDIA GPUs on workstations, and one Slurm allocation on one compute node. Multi-GPU is supported within that node. CPU-only jobs explicitly request `--gpus 0` and still reserve CPU, RAM, scratch, and job slots. Resource values are totals for the job; `--vram` is a minimum **per GPU**.

Excluded from supported v1: MIG, MPS sharing, fractional GPUs, multi-node distributed launch, interactive services, managed containers, preemption of labmates, automatic GPU resets, arbitrary Slurm option passthrough, speculative multi-placement, hostile code isolation, active-active controllers, and transparent retry of unproven failures. Dependencies, groups, and arrays arrive only when their feature gates and tests pass. An advertised but unavailable option returns `feature_unavailable`; it is never ignored.

Submitted jobs execute with the configured remote Unix account's authority. API scopes restrict API operations, not everything arbitrary submitted code can do under that Unix account. Separate Unix identities and administrator-enforced isolation would be required for a stronger boundary. Do not put the controller's token store or all-cluster SSH credentials in a workload-readable shared directory. numpi is not an execution candidate.

## 1. Architecture and invariants

### 1.1 Component ownership

The client builds and uploads snapshots and calls fleetqd. fleetqd owns admission, durable desired state, scheduling, dispatch, reconciliation, artifacts, quotas, and notifications. It invokes fleetctl through one allowlisted adapter. fleetctl owns inventory, route selection, transport policy, host-key handling, preflight, budgets, and audit envelopes. fq-node performs bounded node operations and starts per-attempt systemd services. It is not a resident node daemon; the per-job runner exists only for its job.

Telemetry directions are explicit: **fleetqd reads workstation capacity from fleetmon; fleetmon reads queue/allocation/managed-Slurm snapshots from fleetqd.** Both are read-only integrations. No integration request synchronously causes another remote query or a reciprocal API request. This also corrects the misleading direction of the capacity arrow in the original diagram. [P:L33–L48]

One uvicorn process, one scheduler, one database-owner thread, and one held local controller lock are the supported configuration. Reject multiple workers and development reload in production. API concurrency must not create additional scheduler instances.

### 1.2 Required invariants

1. No two unreleased fleetq reservations contain the same `(node_id, GPU_UUID)`.
2. A logical job has at most one attempt whose remote execution is live **or possibly live**. Unknown and cancelling attempts count.
3. A workload entrypoint is invoked at most once for a particular attempt's durable launch claim. Retrying the logical job is a distinct attempt and requires proof that the prior attempt cannot still execute.
4. A workload-affecting remote mutation requires a committed intent, current authorization, a current fence, and a budget permit before it is sent. Fencing itself establishes the fence. An isolated, unique temporary staging transfer may precede the remote fence check after committed staging intent and fleetctl transfer-budget admission; it cannot publish an input or create a workload. Publication requires the current fence.
5. A cancellation request is durable desired state, not proof of cancellation. No success response claims remote cancellation until termination or never-started evidence is recorded.
6. Unknown required placement evidence makes the candidate ineligible. An empty or truncated response is not a clean observation.
7. A snapshot or artifact referenced by a live, uncertain, retryable, or uncollected attempt is not garbage-collected.
8. API reads, waits, log tails, and UI refreshes do not increase remote polling frequency.
9. Cluster use requires all of: permitted owner/token, job opt-in, enabled site, permitted account/partition/QOS, admitted resource request, and budget.
10. A pause stops new workload creation, not reconciliation, cancellation, result collection, or essential observation.
11. Every externally meaningful state change increments the job version and writes an audit event in the same transaction.
12. A restored or newly initialized controller does not create work until remote discovery and fencing are complete for the affected targets.

These are engineering properties under the stated trusted-code, durable-filesystem, and enrolled-target assumptions. They do not guarantee exactly-once effects in external databases, object stores, or arbitrary applications. A retried application must handle repeated side effects itself.

### 1.3 Controller identity and stale-command rejection

Persist a `fleet_id` across normal restarts. Increment a `controller_epoch` at startup; maintain a distinct diagnostic `process_instance_id`. Each enrolled workstation and cluster control root records the accepted fleet identity and highest controller epoch. Under its control lock, a node rejects lower-epoch launch mutations. On restart, fleetqd fences and reconciles each target before enabling dispatch there. Old running jobs remain valid: the fence applies to new controller operations, not the later execution of a legitimately pending Slurm script.

The production fleet identity and approved node/cluster control root are pinned during onboarding. Development identities cannot claim the same resources by choosing another `job_root`. A same-user adversary can still alter these files; that is outside the supported trust boundary.

On ordinary daemon restart, reconcile known nonterminal **and uncertain** attempts. On backup restoration, first prove the old controller stopped or revoke its ability to issue mutations; this is not active-active failover. Enumerate the enrolled namespace remotely to discover attempts absent from the older database. Each manifest carries enough immutable job/spec, principal, group, and request-key-digest metadata to reconstruct identities without storing bearer secrets. Retain minimal request and attempt tombstones beyond bulky artifact retention. Do not delete evidence merely because the current DB has no matching job.

A restored epoch must exceed the highest accepted epoch discovered at every target being re-enabled, not merely the epoch in an old backup. Persist the new fence before use. During old-backup restoration, keep new submission admission and dispatch globally disabled until every possible prior execution destination is reconciled or authoritatively isolated; otherwise a lost idempotency key could replay on another backend. Normal restarts with an intact database may quarantine individual unreachable targets while other safely reconciled targets continue.

## 2. Bare-metal GPU placement

### 2.1 Identity and visibility

Use complete GPU UUIDs in reservations, manifests, environment configuration, and event records. Record PCI bus IDs and observed indices only as diagnostic attributes. NVIDIA recommends stable UUID or PCI identities because enumeration can change. [R05]

Set `CUDA_VISIBLE_DEVICES` to the assigned UUIDs and `CUDA_DEVICE_ORDER=PCI_BUS_ID` in the controlled workload environment. For CPU-only jobs set CUDA visibility empty. Do not set `SLURM_*` on bare metal. Container visibility is only guaranteed by a validated container adapter configuring the actual runtime; setting `NVIDIA_VISIBLE_DEVICES` in a parent shell is not itself a container-launch implementation. [R06, R07]

Skip MIG-enabled GPUs and nodes using unvalidated MPS or namespace configurations. Reject prohibited compute mode. Mark EXCLUSIVE_PROCESS as a compatibility constraint; do not assume every multi-process job is incompatible or silently change the device's compute mode. Require an explicit compatible execution profile before placing there. Probe architecture, driver identity, model, total VRAM, and capability support separately. A driver's displayed CUDA compatibility version does not prove that a toolkit or the requested Python framework is installed. [R05]

### 2.2 Conservative idle history

Shared placement requires: no fleetq reservation or uncertainty hold; no manual or health drain; no owner reservation; supported process enumeration with no unexplained compute client; utilization and memory within the approved profile; sufficient independent observation history; and a clean immediate launch gate.

Proposed starting policy: a 120-second history; at least three **distinct** sample IDs; no gap above 65 seconds for a validated 60-second source; newest GPU sample no older than 75 seconds; three launch samples one second apart. Changing the collection cadence requires changing the gap and freshness policy. Re-reading one cached sample does not create additional observations. A feed response's generation time is not the GPU sample time.

Every required metric has `supported`, `complete`, `sample_id`, `sample_time`, `source`, and `boot_id`. An unsupported value, permission error, parse error, missing GPU, namespace ambiguity, or stale sample is unknown, never zero. Utilization is a short-period measurement, not proof that no work ran between probes. [R05]

Do not use a universal 1 GiB allowance as proof that hidden processes are harmless. During authorized onboarding, measure an idle baseline for each GPU. A display GPU is reserved by default; an owner-approved profile may allow a measured baseline plus a small documented margin. Non-display GPUs also use measured baselines rather than assuming literal zero. Low memory usage cannot substitute for unavailable process visibility. Store the approved threshold and its rationale.

On exclusive nodes the historical window is unnecessary, but the immediate process, utilization, memory, identity, driver-health, and resource checks still apply. “Exclusive” means an operating agreement, not permission to ignore unexpected users.

### 2.3 Node launch protocol

A placement decision first commits an attempt ID, spec digest, epoch, GPU/CPU/RAM/scratch reservations, and launch operation ID on numpi. Staging may then proceed idempotently by content digest. Before acquiring resources, recheck that the job remains dispatchable.

`fq-node launch` takes a bounded node-wide lock under the canonical, healthy control root. It validates enrollment, epoch, request digest, immutable attempt identity, any cancellation tombstone, existing execution evidence, local reservations, resource headroom, and the chosen UUIDs. It records the attempt manifest and allocation markers durably before starting a deterministic unit.

Under the same control-lock discipline used by cancellation, the unit's runner rechecks the cancellation tombstone and acquires an exclusive, durable **payload-entry claim** before invoking setup or user code. A pre-existing claim causes replay to return/adopt evidence without rerunning the payload. Atomic directory creation alone is insufficient: the manifest distinguishes staged, start-requested, runner-entered, payload-started, stopped, and finalized facts.

If a crash occurs before runner entry, replay may ensure the same deterministic unit exists after reconciling any pending systemd start operation. If the runner-entry claim exists, the same attempt never invokes user code again, even when the unit has been garbage-collected. A crash between claim and payload invocation sacrifices progress, not duplicate prevention; record `START_UNKNOWN` until reconciled and only create a fresh attempt under the proof-of-death/retry policy.

Gate refusal is typed `placement_refused`, not inferred from the workload's eventual exit code. Record it in the audit/placement-decision history, release safe reservations, cool down the rejected GPU for five minutes, and try another candidate without consuming the execution retry budget. A workload that itself exits 75 has not produced a placement refusal.

Staging, lock contention, prechecks, and actual executions have separate counters. Rate-limit all of them so “does not consume a retry” cannot create an infinite dispatch storm.

### 2.4 Cooperative contention handling

A successful gate does not stop a labmate from launching afterwards. Display allocations in fleetmon, publish the residual limitation, and obtain node-owner acceptance before shared enablement. A foreign process generates an alert; fleetq never kills it. Do not automatically kill our own job unless the owner has explicitly selected that contention policy.

Process attribution uses host PID, process start time, boot ID, and cgroup membership. A PID alone can be reused; process-tree ancestry and a mutable environment variable are not sufficient identity. Unreadable attribution is `ownership_unknown`, not a confidently foreign process. Check all visible GPUs for fleetq processes using unallocated devices. With unsupported namespaces, turn off shared placement rather than implying reliable escape detection.

### 2.5 CPU, RAM, scratch, and job-slot admission

Track declared RAM, CPU quota, scratch bytes, and concurrent job slots in the reservation model, not only GPUs. Two concurrent attempts cannot each reserve the same apparent RAM or scratch headroom.

Require both a policy reservation check and a physical-headroom check. The policy check is `sum(unreleased declared RAM) + new RAM <= node RAM budget`; the physical check is `MemAvailable >= new RAM + node headroom`. Include staged-but-not-started and uncertain attempts. Do not subtract the whole running-job reservation again from `MemAvailable`, which already reflects current use.

For disk, account for compressed cache, expanded snapshot size, extraction temporary files, declared writable scratch, retained logs, and uncollected outputs. Quotas and inode availability matter in addition to `df`. Persist bytes reserved by concurrent uploads/transfers. Use measured available space and committed reservations without double-counting already materialized data.

CPU quota limits CPU time, not exclusive CPU affinity. Record that distinction in `fq explain`. CPU sets/topology policies are optional later work. Neither CPU nor memory policy reserves resources against independent labmate launches.

### 2.6 systemd execution, environment, and result durability

Enable a workstation only after an authorized disposable unit survives SSH logout, reports the correct cgroup, and proves effective memory, swap, CPU, task, and runtime properties. Inspect actual cgroup files and parent limits; a configured `MemoryMax` string is not evidence of enforcement. Memory-controller availability depends on the hierarchy/delegation. [R08, R09]

Supported unit contract: a named per-attempt service, `Restart=no`, appropriate `Type=exec` where supported, `KillMode=control-group`, `MemoryMax=<job RAM>`, `MemorySwapMax=0`, `CPUQuota=<requested CPUs × 100>%`, bounded `TasksMax`, `RuntimeMaxSec=<requested time>`, and `TimeoutStopSec=<kill grace>`. Walltime includes setup and application execution. Grace is a separate shutdown allowance, not added twice to RuntimeMaxSec. Start/stop commands themselves have bounded controller-side deadlines.

Use a small runner and an `ExecStopPost` finalizer. Persist application exit/signal evidence and service result independently. The finalizer receives systemd result variables and must be able to run when normal execution failed. Test its behavior under OOM and storage faults; it is not guaranteed to survive a failed disk or host loss. [R10]

Bind execution evidence to the winning payload-entry claim and an immutable invocation nonce. A replayed unit that refuses duplicate entry must write a separate replay observation, not overwrite the original payload's result. Finalizers use compare-and-write semantics for their own invocation; conflicting or late evidence is retained and reconciled rather than replaced by whichever writer finishes last. The same rule applies to an unexpected Slurm restart.

Do not rely on polling a transient unit for the final exit result. `--collect` can discard successful and failed unit state. Enable collection only after durable completion evidence and the reconciliation fallback are implemented and tested; simply removing `--collect` is not enough to retain successful units indefinitely. [R11]

All control records use write-to-temporary-file, file fsync, atomic rename, and parent-directory fsync on a filesystem whose semantics have been validated. A completed wrapper record is not allowed to free GPUs while descendants remain. Status corroborates service/cgroup termination, or a changed boot ID, before release. Missing unit metadata alone gives an unknown outcome, not automatic application failure.

Launch a non-login shell by default with a minimal explicit environment and an approved setup script. `--setup` and `--wrap` are intentional code; ordinary `-- argv...` preserves argument boundaries. Snapshot and in-place paths are canonical absolute paths; no casual `~` expansion through shell quoting. Explicit login-shell mode is per-node opt-in and receives its own timeout/canary. It cannot repair a broken sshd or PAM path that hangs before the shim runs.

Resolve and validate `XDG_RUNTIME_DIR`, runtime-dir ownership, and the user bus rather than trusting caller environment variables. Require linger. Do not auto-change login policy during a probe; enabling linger is an explicit installation action. No fallback to process-group killing plus `/proc/*/environ` sweeps.

### 2.7 Mounts and potentially stuck probes

Keep the shim, control root, working directory, and supervisor result path on validated local storage. A protected runtime probe directory on a separate known-good filesystem must remain usable even when `job_root` fails. Data and in-place paths may be remote mounts only when explicitly declared.

Parse mountinfo without resolving suspect paths. Perform liveness, writability, filesystem identity, free-space, and HOME probes in isolated subprocesses. The parent stays off the suspect filesystem, redirects/closes inherited SSH descriptors, and stops waiting after a deadline. A subprocess in uninterruptible I/O can outlive that deadline. Hard NFS requests may retry indefinitely; do not switch application storage to soft mounts to make the monitor responsive. [R13]

Bound outstanding probe processes: at most one unresolved probe per mount/device and a small node-wide cap. Persist enough identity to avoid spawning more on every poll. A hang quarantines that mount or node until a controlled diagnostic confirms recovery. Never assume a timed-out child was killed successfully. An SSH client timeout also does not prove the remote command stopped.

`--needs PATH` means readable by default; `--needs-rw PATH` explicitly requires a bounded write test. Read-only datasets are valid. Missing, unresponsive, wrong-filesystem, and permission-denied paths have different reasons. Do not globally drain a healthy node for a job-specific absent dataset; use job/requirement-specific cooldowns.

### 2.8 Cancellation, release, and drains

Persist cancellation first. Under the node control lock create a cancellation tombstone, then stop the exact known unit. Confirm termination and cgroup emptiness before marking cancellation complete or reusing resources. A naturally completed job racing with cancel keeps its actual outcome with `cancel_requested=true`.

When unreachable, retain `CANCELLING` and all uncertainty holds. An unkillable child is a cancellation failure requiring quarantine, not a successful stop. A changed boot ID establishes that old local processes are gone; it does not automatically establish the prior application's exit code.

After termination, require fresh clean samples through a 60-second minimum cooldown before reuse. Classify elevated memory as unattributed busy memory until attribution supports a leak. A labmate who started after completion must not be labeled our leaked context.

Transient reachability/telemetry drains may auto-clear after successful checks and a fresh idle history. Driver hangs, severe Xid/ECC conditions, fallen-off-bus reports, and failing storage require classified recovery criteria or manual review. Unsupported ECC telemetry is not an ECC fault, and a historical corrected counter alone does not mandate draining. Manual drains clear only manually. Never issue automatic GPU reset, filesystem repair, or host reboot.

## 3. Slurm clusters

### 3.1 Site profiles and admission

Each enabled cluster has a versioned, owner-approved site profile: canonical controller/budget identity; login endpoints and authentication method; supported Slurm version and command fields; Unix user; account, partition, QOS, GRES mapping; maximum job time and resources; shared control/artifact root; quota and purge policy; accounting availability/lag; query visibility; permitted one-shot actions; and approved query budgets.

Queue preset, partition, account, and QOS are separate fields. Add the missing QOS contract, not only `--account`. Normalize them once and persist the resolved profile digest with the attempt. Explicit account/QOS overrides are checked; they cannot bypass queue/site policy. Fully native scripts are rendered from this same normalized specification and pass the same preflight as wrapped scripts.

Perform static preflight locally at admission. Dynamic remote validation is cached and budgeted at dispatch; request traffic must not force one remote preflight per HTTP submission. `sbatch --test-only` is not an admission or start guarantee. Distinguish a permanently invalid request from temporarily unavailable validation.

The KIAC `chiru`, h200/QOS, AMD 22.05, wellsfargo accounting/native-script, `/rhome`, `/storage`, `/scratch`, and authentication claims stay disabled-site facts until validated. Do not choose a home-directory default without confirming compute-node visibility, quota, and retention.

### 3.2 Stage files before submission

Create the shared attempt/control directory and output directory **before** sbatch. The batch script cannot create its own stdout parent in time for Slurm to open that output. Upload the verified bundle and immutable manifest before any submission claim; verify their digest and durable presence remotely. A bundle cached once may be referenced by multiple attempts, with explicit reference ownership.

Inline the small runner payload into the script. Do not claim that this spools the bundle, dataset, environment, or output directories: Slurm moves the batch script, not other user files. [R01] Keep all referenced inputs pinned while a job is pending, running, uncertain, or eligible for replay recovery. Staging prune and cluster cleanup must respect pins.

A routinely purged scratch filesystem is not suitable for the sole copy of a long-pending input or the only recovery marker. Keep minimal control records in a durable compute-visible root. Scratch output has an explicit risk/deadline; a stopped controller or expired retention can still cause data loss unless the application checkpoints to durable storage.

### 3.3 Submission protocol without blind replay

1. Commit an attempt, immutable request digest, canonical site profile, cap reservation, and remote submission intent on numpi.
2. Stage and verify inputs and directories. No claim of execution has happened yet.
3. Invoke a bounded one-shot login wrapper through fleetctl. It validates the fence and spec and atomically creates a persistent submit-once claim under the attempt root.
4. The wrapper durably records `CALLING_SBATCH` before invoking sbatch once. Capture `--parsable` stdout to a receipt file on the remote side, parse `job_id[;cluster]`, and atomically persist the accepted identity. Request `--no-requeue` where the site permits it.
5. Return the same receipt on a duplicate wrapper invocation. An existing claim with no conclusive receipt returns `submission_unknown`, not a second sbatch invocation.
6. On a confirmed acceptance, commit `(cluster, Slurm ID, submit time, attempt identity)` and observe that allocation. A protocol error, timeout, truncated response, or SSH 255 after the send boundary is ambiguous.
7. Reconcile ambiguity using the durable receipt, exact controlled job name, user, cluster, time window, optional supported metadata, controller snapshot, and accounting. Verify identities and handle multiple matches as a conflict. Never use a broad job-name cancellation to “clean up” uncertainty.
8. If acceptance remains unresolved, keep `SUBMISSION_UNKNOWN`, reservations, inputs, and an alert. Re-query at the normal budgeted cadence with backoff. Do not retry this attempt or place an alternative.

A confirmed pre-execution refusal can release the attempt safely. After the submit-once claim, absence of records is not enough to prove that sbatch never accepted. Slurm names are not unique; `singleton` would serialize same-named jobs rather than deduplicate their effects. [R01] A rare claim-before-submit crash may need operator reconciliation. That is an intentional safety-over-progress choice, not an unhandled code path.

A supported cluster must either honor no-requeue for these jobs or have separately tested scheduler-restart semantics. Record restart count/`SLURM_RESTART_COUNT`. An unexpected scheduler requeue must not run the same payload again: its durable runner-entry claim blocks it and produces a recovery event. Retries initiated by fleetq use a new attempt and do not silently reuse result files.

### 3.4 Batch execution contract

The spooled script contains a minimal early-failure wrapper, immutable attempt identity, expected hashes, cleanup/exit handling, and the result writer. It writes the actual Slurm ID and restart identity before setup. It never inherits a fleetqd token or controller SSH agent.

Preserve scheduler-provided device visibility and Slurm variables. Do not overwrite a Slurm allocation with workstation UUID assignments. Render CPU/GPU/RAM/time and optional `srun` through the tested site adapter. Raw `#SBATCH` lines from a submitted application script cannot override normalized resources: the application script is invoked as payload, not accepted as an unvalidated native submission header.

Capture setup failures distinctly. Ensure the script returns the actual child exit code rather than the exit code of a final successful `echo` or result write. Persist application and scheduler evidence separately. A shell trap cannot record SIGKILL, host loss, every OOM, or an unwritable result path.

### 3.5 One cluster observer and actual budgets

fleetqd is the queue-snapshot owner for managed clusters. Disable fleetmon's direct queue polling for those clusters; fleetmon consumes fleetqd's cached Slurm snapshot. Node inventory and other cluster monitoring must also declare their budget costs. No hidden “preflight” or log helper may bypass the ledger.

A bounded observation session may batch one queue query, a small bounded accounting query for recently disappeared known IDs, result-file reads, and capped log deltas. Count **each controller/accounting RPC**, as well as SSH sessions and transfer bytes. One SSH session containing ten Slurm commands is not one Slurm query. SchedMD warns that excessive client RPCs can degrade its controller. [R02]

Every participating fleetctl on a laptop or other machine obtains a permit from the numpi budget authority before a managed-cluster control operation; numpi co-resident callers use the same authority locally. Deny by default when authority is unavailable. A locally stored token bucket can remain an interim per-machine safeguard, but it cannot claim a global limit. Aliases and multiple login endpoints share a canonical cluster bucket.

Initial cadence remains a proposal until approved by the site owner: 30 seconds during a transition window, 120 seconds otherwise, jittered and constrained by the actual hourly/minute budget. Bound fast-mode duty cycle so a continuous stream of events cannot keep the site permanently in fast mode. No managed live, uncertain, cancelling, or finalizing work means no job-driven polls; explicitly enabled cluster-wide monitoring is a separate budgeted workload.

Reserve action capacity for cancellations and necessary recovery, inside the site's total allowance. Do not let bulk staging or slow accounting hold the only cancellation slot. A budget refusal returns a retry time and never spins. Controller restart must not refill every bucket to a fresh burst; use conservative persisted token state and clock handling.

Key authentication is noninteractive with strict host-key checking. Password/sshpass sites use an explicitly approved bounded authentication flow, not indiscriminate `BatchMode=yes` that disables the required password mechanism. MFA or expired credentials produce an authentication block and human action; never bypass MFA. Disable SSH agent forwarding and redact secrets from process arguments and audit output. [R14]

### 3.6 State and completion evidence

Store raw observations with provenance, query scope, sample age, completeness, and the exact site profile used. Use validated text formats for the oldest supported site; do not parse human default columns. Request all relevant states, including suspended, configuring, completing, and requeue states. A parser error is an error, not an empty queue. Accounting queries specify their time window and identity fields explicitly; default windows can omit older work. [R02, R03, R04]

There is no universal `rc > sacct > log text` rule. A trustworthy scheduler OOM, timeout, cancellation, or node failure outranks an application's isolated zero marker for the overall allocation outcome. A valid application result supplies the process outcome. Conflicting evidence remains visible and is reconciled; arbitrary text in stdout is merely a diagnostic hint. [R04, R15]

A successful `scancel` call acknowledges a request, not completed teardown. Re-placement requires final scheduler evidence that the old allocation is no longer active or can no longer start. In no-accounting sites, a known accepted ID that the validated controller protocol confirms is no longer live may receive `UNKNOWN_EXIT` if no result survived. That is different from an ambiguous sbatch whose acceptance was never resolved. If the site's available commands cannot establish safe termination, keep the attempt unknown instead.

### 3.7 Pending reasons and caps

Ordinary `Priority`, `Resources`, and aggregate association/QOS limits remain submitted with a waiting reason. `AssocGrpGRES` denotes an aggregate limit and is not inherently permanent. User limits can also depend on currently running work. A request beyond a verified per-job or total allowed maximum, invalid account/partition/QOS combination, or impossible time request is a configuration/request block. Classify from both the request and the profile, not a string-only denylist. [R16]

A notification may describe prolonged waiting without cancelling anything. Automatic alternative placement requires an explicit pending-timeout/failover policy, no unknown competing attempt, successful cancellation, and proof the previous copy cannot run. Default automatic failover is off.

Enforce exact local fleetq outstanding-commitment caps transactionally, including pending and uncertain allocations. Display observed external account usage separately, excluding already-counted fleetq jobs. With complete fresh visibility, use that observation conservatively for admission. With incomplete visibility, either block an explicitly strict account-wide policy or use an owner-approved fleetq sub-allocation. Only the site's scheduler/admin policy can guarantee a cap against concurrent independent labmate submissions. Do not assert a never-exceeded global cap based solely on sampled external data.

## 4. Job model, state machine, and scheduling

### 4.1 Separate intention, execution knowledge, and outputs

`desired_state`: `RUN`, `HOLD`, or `CANCEL`.

`phase`: `HELD`, `PENDING`, `DISPATCHING`, `SUBMISSION_UNKNOWN`, `SUBMITTED`, `RUNNING`, `CANCELLING`, `RECONCILING`, `FINALIZING`, `BLOCKED`, or `TERMINAL`.

`execution_outcome`, when justified: `COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`, `OUT_OF_MEMORY`, `NODE_FAIL`, `PREEMPTED`, or `UNKNOWN_EXIT`. Each attempt also has `remote_may_be_live`, evidence records, and an immutable attempt identifier.

`artifacts_state`: `NOT_REQUESTED`, `PENDING`, `COLLECTING`, `COMPLETE`, `RETRY_WAIT`, `FAILED`, or `EXPIRED`. It is independent of GPU allocation ownership. A successful computation with a failed required pull is not resubmitted as compute.

After 24 hours of lost contact, escalate to `RECONCILING(reason=LOST_CONTACT)` with a human alert. Do not make elapsed time a proof of death. `LOST` is no longer an ordinary terminal, automatically retryable outcome. This deliberately resolves the contradiction between the original retry rules and no-concurrent-copy claim. [P:L225–L235]

### 4.2 Retry and operator semantics

`--retry N` defaults to zero and counts **additional executions**, not gate refusals or transport checks. Only retry a proved-stopped attempt of an allowed class. Application-exit retries require explicit `--retry-on=exit`; timeout and OOM are separate explicit classes, not silently equivalent to nonzero exit. Add backoff and a bounded total retry horizon.

No `requeue --force` bypasses `remote_may_be_live`. An operator can attach verified evidence and resolve an uncertain attempt; the action is audited. Intentionally creating potentially duplicate work is a distinct, explicitly acknowledged clone/new job, not a safe requeue and not represented as satisfying the original no-duplicate guarantee.

Hold acts only on locally unlaunched work in v1. Holding a remotely submitted job is unsupported until the site adapter and races are implemented. Cancel, hold, release, modify, and priority updates use expected job version/If-Match. An edit racing with dispatch either commits first and is used or fails with a conflict; it cannot partially modify an already-sent attempt.

### 4.3 Admission versus placement

At admission, reject contradictions and permanent impossibilities supported by verified inventory/profile data. Return `422 unsatisfiable` only when every authorized candidate is known permanently unsuitable. A stale probe, unavailable path validation, busy GPU, down node, exhausted temporary quota, or unknown cluster visibility is not a permanent unsatisfiable proof.

Normalize destination grammar into structured backend candidates. `--queue` narrows Slurm candidates; conflicting `--on`/queue/account options are rejected with an explanation. An explicit cluster name is job opt-in, but never token authorization. `--in-place` paths must be allowed and checked on each named candidate; reproducibility is explicitly weaker than a snapshot.

Require finite positive time/RAM/CPU values except explicitly supported zero-GPU jobs. Resolve omitted values from approved node/site resource profiles and record the resolved amounts. Never invent a universal RAM-per-GPU setting that might make a real workstation unsafe.

### 4.4 Scheduling policy

Run on events plus a 10-second tick, with backpressure. Sort by authorized base priority plus bounded pending-age boost, then submission sequence and stable job ID. Per-job candidate rejection reasons include evidence and freshness. Score exclusive before shared, best-fit VRAM, compatible homogeneous GPU sets, cached bundle, and recent reliability. Deterministic tie-breaking makes tests reproducible.

Reserve all resources atomically before staging. Recheck authorization, configuration generation, desired state, prerequisites, and pause controls immediately before the remote launch boundary. Use one placement/launch in flight per node and four globally; cancellation and small status operations use separately bounded priority lanes. Bulk transfers use their own concurrency and byte limits.

Greedy backfill is permitted. Advanced six-hour starvation reservations are deferred until cancellation/expiry and feasibility are specified. A future reservation must reserve a concrete feasible set, have a lease/reevaluation policy, and disappear when its job is held, modified, cancelled, or becomes unsuitable. Never freeze a shared workstation indefinitely waiting for a labmate to finish.

### 4.5 Dependencies, groups, and arrays — phase P5 contract

Dependency edges reference immutable logical job IDs and are evaluated centrally after retries are exhausted or the applicable start event occurs. `afterok` means the parent is terminal and `job.success=true`, including required artifact finalization. `afterany` means the parent has a definitive terminal result of any kind. `afternotok` means a definitive terminal result with `job.success=false`, including cancellation or required-artifact failure. `after` means an actual first payload-start event, not local dispatch or sbatch acceptance. Unknown or possibly-live parents never satisfy a terminal dependency. Dependencies are ANDed unless a future explicit expression language is introduced.

Reject missing, unauthorized, duplicate, self-referential, or cycle-forming dependencies in the admission transaction. An `afterok` parent that finishes unsuccessfully puts the child in `BLOCKED(DependencyNeverSatisfied)`, with no execution attempt. Retain minimal parent outcomes while dependents reference them. A dependency establishes ordering, not data copying: downstream data comes from declared collected artifacts or approved shared paths, with separate placement checks.

`--each` creates exactly one child per unique canonical named destination, as one all-or-nothing admission transaction and one idempotency replay unit. A Slurm destination means one allocation on that site/profile, not every compute node. Reject alias-duplicate destinations rather than accidentally duplicating work. Every child has an immutable spec, independent outcome, and shared bundle reference. Validate and reserve admission quota for the entire group; do not admit the first half and fail the rest silently.

Arrays expand a bounded, validated index set into those same child records; v1 does not rely on native Slurm array syntax. Store the array index and expose `FQ_ARRAY_TASK_ID`; do not forge Slurm variables. `%throttle` limits live-or-possibly-live children across all backends, not just currently RUNNING rows. Held and pending children still count toward active-job admission limits. A proposed absolute group maximum is 1,000 children, but smaller owner/token limits still apply; the default 20-job agent quota therefore rejects a larger fan-out unless explicitly raised.

Group completion means all children are definitively terminal; success means all satisfy their success policy. Group wait supports explicit any/all semantics and returns all known child IDs on timeout. Group cancel persists child cancellation intents idempotently; a failure of one child does not implicitly cancel siblings. Scheduling aging and resource reservations apply to children, so a large array cannot monopolize admission or bypass per-owner limits.

### 4.6 Clocks

Use monotonic time for in-process elapsed deadlines. Store UTC wall times and boot identity for persistence and presentation. Monotonic timestamps cannot be compared across reboots. After restart, rebuild observation windows and conservatively re-establish cooldowns and budget refill eligibility.

Clock health gates new dispatch and time-sensitive token decisions, not cancellation, status ingestion, or recovery. Use a validated clock-health provider for numpi's actual time service; `timedatectl` is not assumed sufficient everywhere. Do not silently accept expired tokens while the clock is uncertain. Provide local administrator recovery without turning uncertain wall time into permission to create work.

## 5. Data model and durability

Retain jobs, attempts, allocations, deps/groups, bundles, nodes/GPUs, caps, tokens, idempotency, events, outbox, and remote-call audit. Add `controller_meta`, `operations`, `observations`, `resource_reservations`, `artifacts`, `artifact_transfers`, `bundle_refs`, `placement_decisions`, `site_profiles`, and `recovery_tombstones`. These are logical records; combine tables when it does not weaken the invariants.

Database constraints include: foreign keys on every connection; unique request identity; unique `(job, attempt_number)`; partial uniqueness for possibly-live attempts; partial uniqueness for unreleased GPU reservations; valid state/value checks; unique remote receipt identity within its site/submit context; and version-checked updates. Multi-row admission, quotas, reservations, operation intent, event, and job-version changes share one transaction.

Use SQLite WAL with FULL synchronization on validated local SSD storage, never NFS. One database-owner thread serializes DB work, checkpoints, migrations, and source-side backup steps; no HTTP request or long-poll owns a long-running transaction. Bound queue depth and surface overload rather than exhausting RAM. FULL is not a substitute for healthy hardware or a tested filesystem. [R17]

**Current dependency gate:** require a runtime SQLite build containing the WAL-reset fix. Upstream identifies 3.51.3 and later, with fixes also backported to 3.44.6 and 3.50.7. Record `sqlite3.sqlite_version` and documented distribution backport provenance; Python version or a pip lock alone does not establish this. The rare bug concerns concurrent writes/checkpoints from multiple connections. Keep checkpoint ownership explicit even on patched builds. [R18]

Maintain bounded WAL size, disk/inode watermarks, DB queue delay, and backup age metrics. Do not retain read transactions during streaming. Disk pressure first disables uploads/new admission/staging while preserving a metadata reserve for recovery. A failed durable cancellation commit is reported as a failure to persist; never claim otherwise.

Use SQLite's online backup interface, not a naked copy of an active `.db` file without its WAL. [R19] Back up fleet identity, config/profile digests, request tombstones, and protected secrets alongside the DB through a consistent manifest. Snapshots/artifacts need their own storage policy: a DB backup does not contain source bundles or output files.

## 6. Interfaces, bundles, logs, and security

### 6.1 Client and commands

Retain `fq` and the `fleetq` alias, with PATH collision detection. Use Python 3.11+ and strict TOML. Validate configuration size, token-file owner, type, and permissions; do not follow a token-file symlink blindly. Avoid credentials on command lines. Ignore proxy environment routing by default for authenticated local/tailnet API traffic unless explicitly configured, and do not follow cross-origin authenticated redirects.

Retain the original command families, with feature gates. Add the missing defined flags to the contract: `--qos`, `--scratch`, `--kill-grace`, `--retry-on`, and `--needs-rw`. `--array` and throttling exist only after the group/array phase. Use `fq explain` consistently rather than an undocumented `fq why` spelling.

Choose exactly one job form: explicit argv, SCRIPT, or `--wrap`. SCRIPT is a relative path inside the declared code root or an explicitly validated in-place path. Environment files use a documented KEY=VALUE parser; they are never sourced as shell. Reserve scheduler-owned FQ/CUDA/Slurm variables, reject accidental overrides, and redact secret values from display and audit.

Every submission has an idempotency key. Agents supply it; the CLI generates and saves it before network transmission for humans. The canonical immutable spec and bundle digest define the request hash. A replay returns the original identity even after a lost response. The same key with different semantics returns 409. Retain keys at least while their jobs/uncertainty exist and through the job-history retention; expiry must not silently re-execute an old long-pending request. Expose any finite guarantee horizon.

Keep the original CLI exit codes. For `--json`, stdout is exactly one final envelope; progress goes to stderr. `logs --json` is one bounded cached response; reject `--follow --json` until a separately named/versioned JSON-lines streaming contract exists. `--propagate-exit` is a deliberately separate human/script mode whose child-code behavior does not redefine the JSON API's transport/error codes. Preserve job ID on wait timeout or interruption. A wait timeout never cancels.

### 6.2 API contract

Retain `/api/v1` job/read/wait/log/bundle/node/admin operations and typed `schema`/`ok` envelopes. Use bounded pagination, request sizes, JSON depth/string limits, and consistent status/error classes. Add capabilities/feature flags, observation age/completeness, artifact transfer status, and operation status for asynchronous actions.

Long-polls use subscribe-then-recheck to avoid a lost wakeup, release DB transactions before waiting, and clean up futures on timeout/disconnect. Preserve 55-second HTTP waits with a larger transport deadline and a client total deadline. Bound 128 global and 16 per-token waiters, plus per-owner limits so multiple tokens cannot evade quotas. Terminal response caching must respect authorization and token revocation.

Job JSON distinguishes `execution.success`, `remote_may_be_live`, `job.terminal`, `artifacts.required_satisfied`, and `job.success`. A requested collection is required by default unless explicitly marked optional. Default `wait` finishes after required finalization reaches a conclusive success/failure. It must not spin forever on an artifact failure; return the structured failure while preserving the successful execution record.

Mutations return an operation ID or the durably updated desired state. Logs are never accepted as trusted HTML. Use an explicit resource version for modifying operations, and return 409 on stale writes.

### 6.3 Snapshot construction and safe extraction

Keep content-addressed snapshots, but fully define canonicalization: sorted relative paths, normalized uid/gid/user/group names, stable mtime, normalized modes preserving executability, selected tar format/PAX handling, and deterministic gzip header including mtime/name. The bundle identity is the canonical uncompressed tar hash; the upload also records compressed byte count, expanded byte count, member count, and format version.

Exclude fleetctl sync defaults plus `.fqignore`, explicit excludes, local environment directories, caches, and known secret locations as policy. A matching exclude list is a compatibility check, not proof that no secrets exist. Offer a local included-file manifest/dry run. Record file contents and git provenance independently; a git commit plus dirty bit is not the source snapshot.

Detect files changing during snapshot creation using opened-file identity and before/after metadata; fail/retry a bounded number of times rather than claiming atomic repository capture. Record submodule state and untracked included files. Do not follow directory symlinks. For supported v1, allow only regular files and directories in bundles; report excluded links. Internal-symlink support is a later explicit format change, not a permissive extraction shortcut.

Validate on the server and again at extraction: reject absolute paths, `..`, NUL/control characters, duplicate/conflicting paths, symlinks, hardlinks, devices, FIFOs, sockets, sparse payload tricks, and forbidden metadata. Extract into a newly created private directory without following pre-existing links, then atomically publish it. Do not use unrestricted `tar -xf` on an uploaded archive. Python's extraction filters do not independently prevent all denial-of-service cases. [R20]

Proposed limits: original 256 MiB compressed, 100 MiB per file, and 50,000 entries, plus 1 GiB expanded total, bounded path depth/length, extraction wall/CPU limits, and owner/global storage quotas. Verify actual bytes while streaming; never trust client counts or Content-Length alone. Multipart framework spooling and reverse-proxy body limits must be configured to avoid unexpected RAM/disk copies.

Upload goes to a uniquely named private temporary file, is checked and fsynced, and is atomically published. Concurrent PUTs for one digest converge safely. Authenticate HEAD, PUT, and reads; cross-owner hash knowledge grants no access. Physical deduplication can exist behind independent owner references and logical quota charging. Jobs never extract into or modify the cache itself.

### 6.4 Logs and artifacts

A scheduled collector maintains log deltas and final tails on numpi. `fq logs` reads that cache and reports age, completeness, source availability, and any dropped range. `--follow` does not accelerate polling. A fast interactive user experience cannot be promised beyond the approved cluster cadence.

Offsets identify `(attempt, stream, generation, byte_offset)`. Define rotation, truncation, retention gaps, binary bytes, and partial UTF-8 behavior. Return an explicit gap/reset response for evicted offsets; do not replay or skip bytes silently. Bound remote read bytes and per-cycle collection work. A suggested local capture cap is 64 MiB per stream with bounded segments; retain the original 16 MiB archived tail/job and 5 GiB total tail-cache caps unless storage sizing changes. Continue draining or explicitly truncate excess output so a full log cache does not deadlock the workload.

Artifact collection is a durable state machine: enumerate an approved relative-path manifest, enforce file/total/owner limits, transfer to `.partial`, verify size/hash, atomically publish, then mark the manifest complete. Resume/retry collection without rerunning computation. No shell-expanded user glob may escape the attempt root. No symlink following into home, SSH keys, or arbitrary filesystem paths. In-place jobs need an explicit allowed output root because their code directory may contain unrelated files.

Default per-job collected-output allowance is a proposed 2 GiB, overrideable within owner/global quota. Large checkpoints need explicit approved quotas or durable external storage; do not quietly truncate them. Stop automatic retries after a configured deadline and surface `ARTIFACT_FAILED`/`EXPIRED` as finalization failures, distinct from compute outcomes.

Garbage collection requires no live/unknown references and successful required pulls or an explicit approved discard. Shared bundles are reference-counted transactionally. Cache refcounts are periodically checked against authoritative references. A 7-day terminal cache grace does not expire an active pin. `fq fetch` normally serves collected artifacts; a missing remote artifact can enqueue a budgeted transfer operation but cannot synchronously launch an unbudgeted SSH read.

### 6.5 Authentication and browser surface

Prefer `127.0.0.1:8089` behind tailnet-only HTTPS Serve with a fixed canonical origin. Do not enable public Funnel. Tailscale documents Serve as tailnet sharing and HTTPS termination; deployment still needs explicit network policy and API authentication. [R21]

fleetqd independently authenticates all mutations, details, logs, artifacts, and bundles. Anonymous tailnet summaries, if enabled, expose counts/health only—no command, name, owner, path, environment, or log text. Health endpoints reveal minimal information. Token secrets are high-entropy and hash-only in the DB; compare securely, validate expiry, revoke promptly, and rotate service tokens.

Retain read/log/submit/manage-own/manage-all/nodes/admin scopes, but bind agent management to the exact submitting token unless an explicit owner policy says otherwise. Target, account, resource, priority, storage, API-rate, and active-job restrictions apply by both token and owner. The original 20 active jobs/4 concurrently committed GPUs/10 submissions per minute are proposed agent defaults, not permission to exceed an owner's cap. Count unknown attempts and materialized groups. Cluster permission defaults off.

Browser action pages arrive after the CLI control plane. Use dedicated human authentication mapped to a principal and a secure, HttpOnly, SameSite session cookie; CSRF synchronizer token, exact Origin/Host checks, no state changes via GET, and restrictive CSP. Basic authentication is acceptable only in an explicitly tested HTTPS deployment, not as an anonymous link-to-action shortcut. fleetmon's service token cannot authorize user mutations.

Store secrets outside bundle/spec display paths. Use protected environment files or supported credentials delivery where applicable; never log raw bearer tokens or SSH passwords. Backups containing secrets need encryption and access controls. Do not promise that arbitrary application logs cannot reveal values the application prints itself.

## 7. fleetmon integration and notifications

The capacity feed is versioned and contains raw values plus source capability, per-sample identity/time/boot, completeness, and failure reasons. Serve history or identifiers sufficient for fleetqd to build real idle windows. New response timestamps do not refresh old samples. Vendor the schema with compatibility tests and bounded optional fields, not private Python imports.

fleetqd polls the local feed every 10 seconds. A 30-second feed-transport failure blocks shared placement; independently stale GPU samples also block it. Exclusive nodes may proceed using their validated immediate gate. An outage does not kill existing jobs. Restart clears the shared idle window until fresh observations rebuild it.

fleetmon's Queue page consumes cached fleetqd data with the existing short timeout/cache, displays unavailable/stale status honestly, and exposes pending reasons, unknown attempts, artifacts, per-GPU reservations, and query budgets. Existing Slurm rows are populated from the managed observer, not a second poll. Browser cancel/hold links go to fleetq confirmation pages; they are not mutations themselves.

Keep notifications in a persistent transactional outbox with deduplication, backoff, rate caps, and a digest. Count repeated foreign-PID or auth events by incident, not every poll. Separate transient waiting from actionable blocked reasons. Critical scheduler-down alerts require an independent observer because a stopped fleetqd cannot send its own notification. Do not place secrets or full command lines in ntfy payloads by default.

## 8. fleetctl changes — revised P0 contract

Preserve the subprocess boundary but do not assume separate processes fix races in shared `routes.json`, known_hosts, audit trimming, or ControlMaster setup. Make writes atomic and lock the actual shared resources. Test concurrent fleetmon/fleetqd/inventory operations and keep lock acquisition ordered and bounded.

Deliver versioned JSON for submit/exec/sync/job status/cancel plus capabilities/profile queries. The envelope distinguishes local refusal, authenticated transport failure, remote command rejection, budget refusal, malformed response, timeout, and **operation may have executed**. Do not collapse remote uncertainty into an ordinary retryable transport class. Capture stdout/stderr with byte limits while continuing to drain pipes. [R22]

Add end-to-end deadlines to all transport-bearing verbs, including nested copy/preflight/submit operations. A local process-group kill does not establish remote death. Keep outer CLI exit conventions for compatibility while exposing precise typed fields to fleetq.

Add validated account **and QOS** support; normalize resource units and native/wrapped precedence. Inline only appropriately small control payloads using a safe literal representation, with delimiter-collision/quoting tests. Do not imply large input bundles are inline. Fix remote-home path expansion through canonical path resolution rather than unsafe shell interpolation.

Add centralized managed-cluster permit acquisition, canonical endpoint aliases, operation cost classes, bounded wait, cancellation priority, and accounting of all SSH/RPC/transfer work. Interim local buckets are labeled local. `exec --admin` is not universally a read: allowlisted scripts declare their operation class, and unrecognized cluster admin execution is denied or conservatively charged under policy.

Keep `local_target=numpi`, caller attribution, and reduced audit-trim churn, with a dedicated development identity. Preserve the workstation role matrix; detached execution stays in fq-node. Agents receive the safe queue/wait workflow as soon as that workflow exists, not only in the final phase.

## 9. Robustness and recovery matrix

| Failure or race | Required state/hold | Permitted recovery |
|---|---|---|
| Busy GPU at gate | Local pending; placement refusal recorded | Release safe reservation, cooldown, choose another candidate |
| Client loses POST response | Durable original job may exist | Replay same request key/body |
| Crash after intent before stage | Dispatching/reconciling | Resume verified staging; no fresh logical job |
| Node launch response lost | Existing attempt possibly live | Inspect manifest, deterministic unit, launch claim, boot/cgroup evidence |
| Runner claim exists, no result | Reconciling | Never re-enter that attempt payload; establish stopped status first |
| sbatch accepted but receipt lost | SUBMISSION_UNKNOWN | Recover identity; never use empty lookups to authorize replay |
| Slurm job name has multiple matches | Submission conflict; all relevant holds retained | Human reconciliation, exact-ID cancellation only |
| Node unreachable | Running/cancelling with stale evidence | Backoff; no replacement |
| Node boot changes | Old local execution no longer live | Record best supported outcome, then approved retry |
| Unknown exceeds 24 h | Reconciling with escalated incident | Human evidence, not timed automatic retry |
| Cancel races launch | Durable desired CANCEL; local tombstone | Prevent payload when possible, otherwise stop and confirm |
| scancel succeeds, job completing | CANCELLING | Wait for no-longer-live evidence before alternative placement |
| Payload rc zero but scheduler OOM | Evidence conflict, scheduler failure outcome | Do not report success; retain both records |
| Shared feed stale/replayed | Candidate unknown | No shared launch until new independent history |
| Foreign GPU process | Running with contention incident | Notify, never kill labmate |
| Unit gone, result missing | Unknown exit unless stopped status established | Do not invent exit code or assume safe GPU release |
| Probe stuck in I/O | Mount/node quarantined; probe budget occupied | No child accumulation; controlled recovery |
| Unsupported GPU metric | Capability unknown/unsupported | Reject required capability, not zero-value idle |
| Severe driver/storage health event | Manual/classified recovery drain | No reset/repair/reboot automation |
| numpi low disk/inodes | New uploads/admission/staging stopped | Preserve metadata reserve; pin-aware cleanup |
| State volume absent/wrong | Service not ready; no DB creation | Restore correct volume, no fallback empty DB |
| Budget exhausted | Deferred operation | Retry at supplied time; no busy loop |
| Auth expired/MFA required | Site auth block | Human restores approved credentials; no prompt hang |
| Required output pull fails | Execution recorded; finalization retry/failure | Retry transfer only; keep remote source pinned |
| Controller restored from old DB | Dispatch paused, discovery incomplete | Fence and enumerate remote namespace; import missing attempts |
| Pause/dispatch-disable | No new workload creation | Cancellation/reconciliation/collection continue |
| Duplicate/out-of-order telemetry | Ignore stale sequence for state progression | Keep raw audit and monotonic evidence rules |
| Disk/DB commit fails before remote mutation | Intent not durable | Do not send mutation |
| Clock unhealthy after reboot | New dispatch gated | Reconcile/cancel; rebuild clocks/windows conservatively |

## 10. Blockers to verify before enabling execution

Create an `evidence/` record per target: command/profile version, capture time, output digest, reviewer, and approval. A field not checked is `unknown`, not inferred from an old fleetmon note.

Required numpi evidence: actual Pi model/RAM/architecture, OS/Python/systemd/SQLite build, SSD mount UUID and filesystem, free bytes/inodes, state path, backup destination, tailnet/HTTPS setup, canonical fleet inventory digest, port availability, clock provider, and the ability to reach each approved destination. An unreachable site is disabled; a relay is only added after policy and bounded-operation review.

Required workstation evidence: owner approval and shared/exclusive map; UUID inventory and reserved display devices; process visibility; baseline memory; cgroup enforcement and parent limits; linger/logout survival; healthy local control/job roots; configured fleetctl transfer budget; environment/setup behavior; runtime deadlines and cancellation of grandchildren; durable result collection; and boot-ID reconciliation.

Required cluster evidence: owner-approved login automation policy/budget; exact login endpoint/controller identity and credentials; account/partition/QOS/GRES; accounting/visibility; no-requeue behavior; durable compute-visible control storage; output quota/purge policy; and supported query/cancel formats. Verify whether the stated chiru cap is a shared account total, per user, QOS-specific, or another association limit. Never hardcode the example `2` as a verified fact.

Inspect the real fleetctl/fleetmon source and lockfiles before patching: the claimed missing flags, helper placement, parser behavior, sync exclusions, mutable state, existing service hardening, and uncommitted work. Commit or isolate unrelated changes. The reported wellsfargo helper/role inconsistency requires an owner-approved resolution, not automatic removal of an unknown service.

**Fail-closed defaults:** every node and cluster disabled until its record is approved; shared mode requires explicit owner approval; agent clusters disabled; no unverified account-wide promise; no inferred filesystem fallback. Unknown infrastructure facts do not prevent offline implementation, but they prevent enabling the corresponding backend.

## 11. Package layout, deployment, and operations

Keep the original package split and add explicit protocol/evidence/operation modules rather than hiding state transitions in HTTP handlers. `engine/state.py` is the sole state reducer; `executors` return typed observations/effects; `fleetctl.py` is the only transport spawner. `contracts/` covers client API, node protocol, site profile, capacity feed, and artifact manifests. Build the single-file client from maintained modules with reproducible generation rather than hand-copying logic where practical.

Reused fleetmon subprocess code/parsers are adapted behind independent tests. Pin their provenance/license and failure behavior. A copied function is not automatically correct for write-side uncertainty, durable jobs, or hard I/O hangs.

Run fleetqd as the agreed numpi user service only after deployment tests establish linger, startup ordering, runtime limits, and hardening support. User-service mount namespacing and mount dependencies are not presumed equivalent to a privileged system service. Add an explicit prestart and runtime check for the intended filesystem identity, sentinel, path ownership, permissions, and actual DB location. Some systemd sandbox options depend on user-namespace support. [R23]

Set proposed initial daemon `MemoryMax=384 MiB`, `Nice=5`, `Restart=on-failure`, bounded restart backoff, and restrictive umask. Validate memory under simultaneous uploads, waits, reconciling, hashing, and log capture; the original <150 MiB idle target is a benchmark target, not an achieved fact. Separate bounded hashing/extraction workers prevent blocking the API event loop. Direct all writable paths, temporary uploads, shared fleet state, SSH sockets, and logs to explicitly approved locations.

Upgrade: stop new admission or drain in-progress submissions to a durable boundary; pause new dispatch; finish or record uncertainty for inflight mutations; create and verify a backup; switch the immutable release; migrate under lock; restart; fence/reconcile; check readiness; resume only eligible targets. Do not wait forever for a remote operation to become certain before an emergency upgrade. Reverting application files is not a database downgrade strategy; define forward-compatible migrations or restore with full remote discovery.

Readiness means DB/config/auth are healthy and startup discovery is complete for enabled targets. A quarantined unreachable site need not prevent service for other verified targets. Expose separate `live`, `ready`, `dispatch_ready`, and per-target reconciliation state.

Daily online backups to another filesystem retain 14 copies. Proposed additional policy: weekly encrypted off-device backup and a monthly restore exercise. Daily backups imply up to a day's unrecoverable new control-plane history after total storage loss unless a more frequent backup scheme is chosen. A different partition on the same SSD is not an independent failure domain. Restore tests include orphan discovery, idempotency tombstones, unresolved attempts, and bundle availability.

Keep terminal job history for at least 180 days, detailed events for at least 90 days, bounded archived tails, and seven-day unreferenced bundle grace, subject to quotas and privacy. Retain compact active/uncertain identity records longer as needed; do not expire live safety state to satisfy a retention timer. Job deletion or compaction beyond 180 days must preserve idempotency replies and dependency outcomes; indefinite retention is safe until that contract exists but may exhaust storage. Garbage collection has a dry-run mode, ownership/root checks, reference proofs, and a separate budget.

Kill switches: accept-off stops new jobs; pause/dispatch-disabled stops new launches; node/cluster/account freeze stops new allocation in that scope. All retain observation and cancellation by default. A separate explicit network-lockdown stops remote operations and warns that cancellation cannot be applied. Stopping fleetqd leaves detached remote jobs running. Check emergency flags on the new-launch path, not indiscriminately before every fleetctl read or cancel.

Inventory deployment remains laptop-rendered, offline-validated, atomically activated, digest-tracked, and secret-redacted. Validate reachability separately after authorization. Node shim upgrades preserve old per-attempt runner versions until all dependent jobs finish. Install one node at a time; no fleet-wide implicit deployment from `doctor`.

## 12. Delivery phases and release gates

| Phase | Build scope | Evidence required to exit |
|---|---|---|
| P0 — contracts and containment | Verify repository assumptions; typed fleetctl errors/deadlines; account/QOS; local interim budget plus central authority design; capability schemas; network-isolated test harness; resolve numpi structural blockers | No test egress; protocol golden tests; concurrency/fault tests; no unknown fields silently accepted; approved inventory/evidence template |
| P1a — durable control plane | DB constraints, controller identity/fencing, API/token ownership, admission/idempotency, safe bundles, quotas, desired state, waits, fake executors, notification outbox, backups | Crash tests around every commit/send boundary; lost-wakeup and revoked-token tests; archive attack tests; restore-discovery tests; zero duplicate payload entries in the supported model |
| P1b — exclusive workstation canary | systemd executor, durable runner/finalizer, GPU/resource gates, logs, required artifacts, cancel, walltime/OOM, environment and stuck-probe bounds, rollout controls | All offline tests plus real disposable local-systemd integration; then an explicitly approved exclusive workstation canary; survive controller/SSH loss; cancel descendants; correct reboot/unknown behavior |
| P2 — Slurm without blind replay | Managed observer handoff, centralized permits, site profiles, staged inputs, submit-once receipt, unknown state, scheduler evidence, caps, exact-ID cancellation, no-accounting and native-script support | Fakes accept-and-drop/expire/lag/requeue scenarios; real local Slurm integration without campus egress; administrator policy approval; first production site job initiated by the user, one site at a time |
| P3 — shared workstations | Independent idle histories, approved GPU baselines/reservations, complete-visibility checks, verified resource enforcement, attribution, drains, fleetmon allocation display | Every idle-table scenario plus repeated cached samples, hidden-small-allocation ambiguity, memory oversubscription, PID reuse, post-launch contention, bounded probe leaks; explicit shared-map approval |
| P4 — operator UI | fleetmon Queue/Nodes/Status, fleetq action pages, CSRF/session tests, incident dedupe, remote-cost ledger | No hidden second poller; no UI-driven remote calls; stale/unknown visible; supported browser security tests; notification-soak results |
| P5 — workflow conveniences | Cross-backend DAGs, `--each`, arrays/throttle, richer retries, aging reservations, pending edits | Cycle/missing-parent authorization tests; atomic fan-out admission; quotas include groups; safe failure/dependency semantics; no barrier around uncertain parents; cancellation race properties |
| P6 — adoption and maintenance | Finalized agent skill, quota/token UI, operation runbooks, compatibility matrices; MCP only if evidence warrants it | Sustained audited use without agent babysitting loops; restore and upgrade exercises; complete enabled-target evidence records |

Safety-critical RAM enforcement, environments, bounded probes, output durability, tokens, quotas, and basic agent instructions are no longer deferred behind an unsafe MVP. Feature count does not determine readiness; the backend's safety gate does.

## 13. Verification and acceptance tests

### 13.1 No accidental university contact

Run default tests in a network namespace/container with no external egress, empty credentials/SSH-agent environment, temporary HOME/state, and no mounted real inventory or secrets. PATH fakes remain useful but are not the security boundary: absolute executables and direct sockets can bypass them. Test mode restricts the transport executable and target allowlist. Production canaries are separate commands with exact target confirmation and never execute from ordinary pytest.

### 13.2 Model, crash, and fault properties

Model jobs, attempts, operations, remote claims, pending start actions, budgets, evidence, transfers, and clocks. Generate reordered/duplicated observations, late receipts, partition/reconnect, controller restart, storage loss, budget denial, and cancel/hold races. Assert the invariants in §1.2 over **possibly-live** attempts, not only RUNNING rows.

Inject failures before/after each local commit; before/after remote manifest/claim fsync; before/after unit creation; between runner claim and payload; after payload completion before result persistence; before/after sbatch acceptance and receipt; during cancellation; during hash/rename/GC; and during DB migration/backup restore. Include duplicate unit recreation and late finalizers in the crash matrix; neither may overwrite the winning invocation's result. Count actual workload-entry events separately from transport invocations. Several idempotent status/launch RPCs are acceptable; a second payload entry for the same attempt is not.

The fake Slurm must allow duplicate job names, delayed and absent accounting, queue disappearance, accepted-then-lost responses, all active transitional states, multiple same-name matches, submit timeout while the remote command continues, false-negative/default-window queries, and scheduler requeue. A fake that deduplicates names would validate the wrong algorithm.

Add security tests for owner/token boundaries, cluster and account authorization, malicious archive members and bombs, upload hash races, symlink replacement, secret redaction, malicious log text, output traversal, cross-origin redirects, pagination caps, and storage quota races.

### 13.3 Real semantics without campus access

Fakes cannot validate systemd lifecycle, cgroup OOM, filesystem behavior, or Slurm state semantics by themselves. Use a disposable local Linux VM with systemd/cgroup v2, and a separate local Slurm installation/container matching the needed command behavior. These environments have no university route or credentials. Validate actual cancellation, finalizer behavior, unit collection, signal propagation, timeout classification, logout survival, and no-requeue handling.

Only then run a user-authorized workstation canary and user-initiated cluster production job. Store each result as `verified_by_run` with versions, time, target, and evidence digest. A documentation conclusion is `verified_by_docs`; a repository inspection is `verified_by_source`; neither is a hardware test.

### 13.4 Concrete release checks

A backend is enabled only when: repeated submission replay returns one job; delayed responses do not create another execution; 50 concurrent waiters and repeated UI/log requests leave the same scheduled remote-call count; every request respects the configured authority budget; cancelling an unreachable job remains cancelling; uncertain submissions never trigger automatic alternatives; cached telemetry cannot manufacture an idle window; required artifact failure cannot rerun compute; controller restore discovers remote orphans; and a paused system can still cancel.

Benchmark the actual Pi: idle RSS/CPU, p95 API latency, DB queue latency, hashing/upload memory, simultaneous waiters, reconciliation duration, and per-cluster hourly RPC/SSH/byte totals. Retain the original 5–12-second start and <150 MiB/<3% idle figures as hypotheses to measure, excluding cold history establishment, uncached transfers, setup time, and queue wait. Do not advertise them as service guarantees before measurement.

## 14. Risks and operator runbooks

Residual risks are explicit: cooperative GPUs can be taken by outsiders; trusted same-UID code can access that identity's resources; storage hardware can fail despite fsync; uninterruptible I/O may need administrator recovery; accounting-free clusters can lose exit evidence; uncertain submission can require human resolution; purgeable output can disappear during an outage; and numpi is a single controller. None is disguised as automatically solved.

For unknown submission: keep dispatch blocked for that job, inspect the receipt/claim/profile, query exact identity within budget, retain artifacts and reservations, and record evidence before resolving. Never “try again and see.”

For stuck cancellation: preserve intent and reservation, inspect the exact execution identity, escalate if cgroup/scheduler teardown cannot be confirmed, and do not move the job elsewhere.

For state-volume/DB failure: stop new dispatch, preserve remote jobs, repair or restore the control plane, fence and discover remote state, reconcile lost history, then resume approved targets. Do not start an empty database as recovery.

For low disk: stop new data admission, preserve control-plane reserve, inspect pinned references, move or collect outputs, and run dry-run GC before deletion. Never free an active/unknown pin to meet a watermark.

For shared-node contention or severe GPU health: alert the owner, keep outsiders untouched, quarantine only the necessary scope, and require evidence appropriate to the drain reason before resuming.

## Appendix A. Build-versus-buy, corrected

Retain the agreed build direction, but describe it as a fit/operations decision rather than a proven statement that no existing system can work. The custom requirement is the combination of cooperative raw-workstation GPU placement, fleetctl policy mediation, strict university login behavior, numpi hosting, cached agent waits, and the required audit/recovery semantics.

Current SkyPilot documentation describes Slurm support over SSH; therefore the original one-line rejection is not an adequate current comparison. Evaluate a pinned release's actual runtime, permissions, polling, and failover semantics before making stronger claims. This review does not claim SkyPilot meets the full constraint set. [R24]

jobflow-remote uses a Mongo-backed queue. MongoDB's current ARM requirements exclude Pi 4 for supported modern server builds, but numpi's Pi model was not supplied and a remote MongoDB server is logically another deployment option. Rejecting extra infrastructure is a reasonable preference; claiming that this makes jobflow-remote categorically impossible is not. [R25, R26]

HTCondor/Bosco, HyperQueue/Dask/Parsl, Nomad/Flux, and pueue remain candidates whose original objections must be version- and deployment-specific. Do not carry forward unverified blanket claims about their architecture as researched facts. No broad migration is recommended here; the retained build decision rests on the explicit integration and deployment constraints above. Borrow ideas, not unstated guarantees.

## Appendix B. Primary-source registry

Sources checked on 23 September 2026. “Latest” documentation may describe capabilities absent from the deployed versions; release profiles and local tests remain mandatory. URLs are recorded for auditability; citations in the plan refer to these IDs.

- **R01 — SchedMD, sbatch:** submission acknowledgement, job names, singleton, no-requeue, output handling, and script-only movement. `https://slurm.schedmd.com/sbatch.html`
- **R02 — SchedMD, squeue:** state/query formatting and controller-RPC performance warning. `https://slurm.schedmd.com/squeue.html`
- **R03 — SchedMD, sacct:** accounting visibility, time windows, duplicate identities, and structured formats. `https://slurm.schedmd.com/sacct.html`
- **R04 — SchedMD, job state codes:** allocation outcomes and state flags. `https://slurm.schedmd.com/job_state_codes.html`
- **R05 — NVIDIA, nvidia-smi:** supported metrics, N/A behavior, device identity, compute mode, sample intervals, health limitations, and driver/toolkit distinction. `https://docs.nvidia.com/deploy/nvidia-smi/index.html`
- **R06 — NVIDIA, CUDA environment variables:** UUID-based CUDA visibility and enumeration order. `https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html`
- **R07 — NVIDIA Container Toolkit, specialized Docker configurations:** runtime-level device exposure. `https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html`
- **R08 — systemd, Control Group APIs and Delegation:** hierarchy and controller delegation. `https://systemd.io/CGROUP_DELEGATION/`
- **R09 — Linux kernel, cgroup v2:** memory.max, memory.high, memory events, and resource-controller semantics. `https://cdn.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html`
- **R10 — systemd project, systemd.service manual source:** service lifecycle, timeout and finalizer semantics. `https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.service.xml`
- **R11 — systemd project, systemd-run manual source:** transient-unit collection and remain-after-exit behavior. `https://raw.githubusercontent.com/systemd/systemd/main/man/systemd-run.xml`
- **R12 — Python, tomllib:** standard-library availability since 3.11. `https://docs.python.org/3/library/tomllib.html`
- **R13 — Linux NFS manual:** hard/soft retry behavior and integrity caveats. `https://man7.org/linux/man-pages/man5/nfs.5.html`
- **R14 — OpenBSD/OpenSSH, ssh_config:** BatchMode, authentication, liveness, and transport settings. `https://man.openbsd.org/ssh_config`
- **R15 — SchedMD, job exit codes:** application/batch/step exit evidence and derived exit codes. `https://slurm.schedmd.com/job_exit_code.html`
- **R16 — SchedMD, job reason codes:** aggregate-limit versus request-limit distinctions. `https://slurm.schedmd.com/job_reason_codes.html`
- **R17 — SQLite, WAL:** local filesystem, synchronization, checkpoints, and retention of WAL state. `https://sqlite.org/wal.html`
- **R18 — SQLite, 3.51.3 release and WAL-reset advisory:** fixed release and backport verification. `https://sqlite.org/releaselog/3_51_3.html` and `https://sqlite.org/wal.html#walreset`
- **R19 — SQLite, online backup API:** coherent live backup. `https://sqlite.org/backup.html`
- **R20 — Python, tarfile extraction safety:** extraction-filter limitations and additional validation. `https://docs.python.org/3/library/tarfile.html`
- **R21 — Tailscale Serve:** tailnet sharing and HTTPS service termination. `https://tailscale.com/docs/features/tailscale-serve`
- **R22 — Python, asyncio subprocesses:** timeout handling and pipe backpressure. `https://docs.python.org/3/library/asyncio-subprocess.html`
- **R23 — systemd project, systemd.exec manual source:** user-service sandboxing limitations and environment/credential behavior. `https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.exec.xml`
- **R24 — SkyPilot, getting started on Slurm:** current SSH-based Slurm support and runtime prerequisites. `https://docs.skypilot.ai/en/latest/reference/slurm/slurm-getting-started.html`
- **R25 — jobflow-remote, JobController:** Mongo-backed queue-store requirement. `https://matgenix.github.io/jobflow-remote/api/jobflow_remote.jobs.jobcontroller.html`
- **R26 — MongoDB, production notes:** current ARM microarchitecture requirements and Pi 4 limitation. `https://www.mongodb.com/docs/manual/administration/production-notes/`

## Final go/no-go rule

Build the proposed core, but enable a destination only when its facts, resource controls, recovery protocol, remote-call policy, and required output handling are evidenced. When evidence is missing, preserve the job and explain the block. Prefer a recoverable pause to a plausible but unjustified launch, cancellation claim, resource release, or retry.
