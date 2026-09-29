# fleetq implementation status

This records repository work against [REFINED_PLAN.md](REFINED_PLAN.md), not a
release approval. Source inspection is not evidence that a remote machine,
filesystem, systemd version, Slurm site, or university policy has the required
behavior. Every production target remains disabled until its own evidence and
canary are recorded.

| Phase | Local implementation | Evidence still required |
|---|---|---|
| P0 | fleetctl JSON envelopes, deadlines, Slurm account/QOS handling, central permit client, and local fallback are present in the fleetctl submodule. The capacity-feed v1 parser now rejects unknown fields at every nested level. Local protocol, concurrency, and fault tests passed with egress blocked. A read-only local fleetctl inventory check found 16 enabled targets and no doctor problems. | Resolve fleetctl's stale-key and missing-binding warnings, bind this project if needed, verify target facts, and configure the central authority on every participating client. |
| P1a | fleetqd has one SQLite owner, durable jobs/attempts, token scopes, admission, bundles, idempotency, reconciliation, fencing, backups, a notification outbox with backoff/rate cap/digest, and conservative local bundle GC. Local crash tests now cover rollback/reopen, interrupted staging, uncertain launch with one payload entry, delayed replies and observations, migration, backup restoration, and restore refencing/orphans. Revoked tokens cannot receive a sleeping wait's result. Uploads have token/owner/global byte caps, serialized no-overwrite publication, and a physical free-space reserve. Offline GC removes only aged, owned upload parts. A restored database has a persistent admission/dispatch gate; remote orphans keep that gate active. | Real power-loss/fsync and restore exercises; obtain SQLite WAL-fix provenance, state-volume identity, and backup evidence on the deployment host. |
| P1b | Exclusive workstation executor, fq-node runner/finalizer, GPU gate, resource reservations, and cancellation paths are present. Stage upload writes to a unique temporary path; publication occurs only under an exact-epoch shim lock. Interrupted extraction cannot mark an attempt ready. The fake-node fault matrix covers duplicate unit recreation, runner claim, late finalizer, and stale-epoch cancellation/release. Artifact pulls have durable token/owner/global byte reservations and a local free-space gate; a replay reuses only identical published bytes. Local fake workstation end-to-end tests pass. Ada1 was authorized for a canary under `ayand`; linger was enabled, a transient user unit enforced `MemoryMax=64M` and `MemorySwapMax=0`, and stopping another unit removed its two-process cgroup. A configurable `node_python` now supports its installed Python 3.12. | Ada1's clock is about 43 minutes behind numpi with NTP disabled; the operator said not to synchronize it. Numpi still lacks an approved ext4 SSD state path. Retain target evidence and run the exclusive canary only after these deployment gates are resolved. |
| P2 | Slurm site profiles, fenced staging, submit-once claims and receipts, unknown submission state, exact-ID cancellation, centralized permits, and one cached queue observer with fleetmon handoff are present. Fakes cover accepted-then-lost replies, an in-flight remote submit after client timeout, accounting-window misses, queue disappearance, live transitional states, stale-stage refusal, and a result published during scheduler observation. | Real local Slurm semantics exercise; administrator policy and budget approval; one user-initiated production site job, one site at a time. |
| P3 | Independent shared-GPU idle history, immediate launch gate, reserves, attribution alerts and drains are present. The feed rejects unknown v1 fields, concurrent CPU jobs cannot overcommit declared RAM, and uncertain PID identity drains the GPU. The protected runtime keeps a bounded probe ledger with timeout quarantine; local probe and attribution matrices pass. | Prove process visibility, hidden-small-allocation behavior, and post-launch contention on real hardware; validate GPU baselines and memory enforcement; explicit shared-map approval. Shared targets stay disabled until these are satisfied. |
| P4 | Fleetmon read-only Queue/Nodes/Status surfaces, managed-snapshot outage incidents, a persistent notification outbox, fleetq human action pages, and a scoped per-site remote-cost ledger with current bucket capacity are present. Browser security and queue-ledger tests pass; a simulated ten-hour notification soak and stale-feed/no-second-poller regressions pass. Fleetmon also alerts on a stopped fleetqd through the existing managed-site poll or, with no managed sites, one health check on that same cadence. | Confirm deployed notification delivery and stale-state display. |
| P5 | Dependencies, groups, arrays/throttle, retry policy, and pending-job edits are implemented behind admission and state checks. Local checks cover unauthorized/missing parents, atomic quota rejection, corrupt cycles, cancellation before/during launch, unreachable cancellation, array cancellation, and dependency/required-artifact failure races. Seeded fault cases check array throttle, mixed dependency gates, clock loss/recovery, and state invariants through cancellation and legacy dependency cycles of several sizes. Seeded fake-Slurm cases combine lost submit replies, queue/accounting gaps, and cancellation. | Sustained use and broader generated property/fault exercises remain before this phase can exit. |
| P6 | Setup, target-evidence, upgrade, incident, and local bundle, artifact, abandoned-upload, event-detail, and remote bundle-cache GC runbooks and a compatibility matrix exist. Remote cache purge is an explicit inspect-then-one-digest operation behind exact fencing, reference checks, a seven-day grace, and terminal artifact-finalization markers. The daemon dependency lock is pinned with package hashes, and the installer requires them. The fleetctl agent skill was updated and validated. | Exercise cache retention on disposable targets; validate the locked install on the target Python/architecture; finalize the agent/operator workflow from sustained use; record real target, restore, and upgrade evidence. |

The phase exit criteria in the refined plan remain authoritative. Do not mark a
phase released because its code or this table exists.

## Local verification completed

The critical remote paths were reviewed again: fleetctl transfer fallback,
bundle upload and download, admission replay, bare-node fencing, release and
cancel, and Slurm fence, submit and cancel. This found and fixed a relay retry
after a dispatched peer rsync failure, client credentials over non-loopback
HTTP, unverified artifact replacement, duplicate submit rejection behind
admission gates, stale-controller cancellation/release, an incorrectly cached
bare-node fence epoch, and malformed Slurm fence/job IDs. Focused regressions
cover these boundaries. The bundle upload and Slurm stage hash now stream from
disk; bundle HEAD verifies that the referenced file still exists.

The later fault-matrix pass found delayed launch replies that could roll back
an observed running or released attempt and release its reservation. Those
replies now leave state intact and produce a stale-reply audit event. The API
rechecks bearer authorization when a sleeping wait wakes. Bundle PUTs keep one
validated compressed encoding per digest and count token, owner, and global
stored bytes; valid objects left by a crash before the DB commit count toward
the global cap. Artifact collection validates staged manifests and reserves
pending bytes transactionally before pulling. A replay can reuse an identical
published artifact tree, but a conflicting tree remains in place and defers
finalization. These checks remain subject to the supported single-daemon and
trusted-same-UID assumptions in the plan.

The full suites passed in Linux network namespaces with loopback only:
**678 fleetq tests**, **401 fleetctl tests**, and **347 fleetmon tests**.
The fleetq runner additionally used a temporary HOME, no inherited
credentials, and temporary files on the workspace volume. Fleetmon reported
500 dependency deprecation warnings. The
final fleetq run started after all code edits and covered workstation and
Slurm end-to-end paths. Build freshness, Python compilation, the JavaScript
harness, and `git diff --check` passed. The updated remote-fleet-operator
skill previously passed the skill validator.

The fleetq runner now puts pytest's temporary files on the workspace volume,
so a busy `/tmp` tmpfs cannot trigger the daemon's 2 GiB upload reserve during
an otherwise valid local test.

The subsequent seeded P5 fault cases passed in the same no-egress runner, and
the full fleetq suite passed again after the later deletion and clock guards
were added.

A further critical-path pass added read-only root checks before workstation
and Slurm file transfers, rejected HOME, protected user configuration, and
system directories as control/state roots, and removed fq-node bytecode writes
before root validation. Slurm submit now requires a responsive controller
before creating the submit-once claim; its extra scheduler RPC is charged to
the permit budget. fleetctl rejects `sync --delete` against HOME and protected
remote paths before transfer. Local and remote cache cleanup now requires
terminal artifact finalization before releasing per-attempt bundle pins; the
controller records acknowledgments durably and retries them fairly after
crashes or transport failures. The operator-invoked Slurm purge additionally
requires a stopped daemon, a matching target authorization, and a central
permit. Controller lock files refuse symlink and hard-link aliases before
truncation. These changes are covered by focused safety and end-to-end tests.

A further destructive-path review found and fixed a malformed bundle digest
that could escape the bare-node cache directory during preparation, symlinked
attempt directories on bare nodes, Slurm output staging through a symlinked
attempt or outbox directory, and an artifact-GC path assembled from a corrupt
attempt number. fleetctl now resolves remote delete destinations through the
active connection route before transfer, checks the actual remote HOME and
workdir, and refuses local pull deletes of HOME, its ancestors, and protected
system or HOME configuration directories even with `--force`. Protected HOME
directories that are themselves symlinks are covered. The extra remote path
probe is included in sync's control-budget estimate. Regressions cover these
cases without contacting a real remote target.

Production startup now checks enabled fleetq routes against fleetctl's local
topology before opening the state database: bare nodes must be enabled
workstations, and Slurm sites must be enabled login targets. Duplicate enabled
routes, quoted boolean flags, and unknown controller or limits keys fail config
loading. The normal daemon refuses enabled remote nodes in development mode,
which otherwise skips production evidence. Focused tests check that a role
mismatch stops runtime construction before state is opened.
Every bare exec and transfer, including onboarding, now requires fleetctl to
resolve the target as a workstation at call time. Slurm exec and transfers
require a login target. Fleetctl checks that role before connecting, so an
inventory change after daemon startup fails closed; local regressions cover
exec, push, pull, and the role-drift case without remote contact.
Slurm submit profiles reject shell wrappers, relative `sbatch` paths, and
`--wrap`, and require the staged script as the final argument. The real remote
`sbatch` executable still needs site validation. Artifact cleanup and
publication reject symlinked staging or destination parents, malformed staging
names, and output path traversal. These fixes have no-egress regressions.
Production runtime now requires an explicit Chrony or systemd-timesyncd clock provider. A cached,
bounded probe starts unhealthy and gates new dispatch and expiring-token
authentication until it succeeds; a failed or stale sample closes the gate
again. Readiness reports clock health separately from controller recovery.
The no-egress suite passed after this wiring, including startup, token, API,
and probe regressions.
Fleetq now rejects incomplete or contradictory fleetctl result envelopes and
checks the reported verb and target before trusting whether a remote mutation
may have executed. Fleetctl Slurm profiles reject relative `sbatch` paths, and
interactive SSH to a login target requires an explicit `--admin` override.
The full fleetctl and fleetq suites passed after these changes.

A final destructive-path pass made online backups and artifact fetches refuse
existing destinations by default. Backups publish a verified SQLite copy with
an atomic no-replace link; `fq fetch --overwrite` is required to replace a
local artifact. The installer uses unique temporary unit and release-link
paths, and failed-install cleanup checks the release directory's identity.
The bare-node shim removes private subtrees through pinned directory
descriptors, closing a parent-symlink swap during cleanup. Slurm staging
rejects a symlinked stage source, and artifact collection allocates a unique
temporary directory rather than removing a predictable PID-based one.
Fleetctl's destructive sync uses the checked canonical remote path and, for
pulls, the checked canonical local path. Focused regressions cover the ordinary
clobber, PID-collision, and symlink-swap cases. The full suites were rerun in
no-egress namespaces after these fixes.

A read-only `fleetctl smoke numpi` reached the controller host. Its first JSON
clock probe exposed missing capture-limit arguments in fleetctl's SSH helper;
the helper now forwards both bounded capture limits, with a no-network
regression and a passing full fleetctl suite. The repaired JSON path then ran
the exact systemd-timesyncd service-and-sync predicate on numpi successfully.
The new provider and further seeded P5 cases have focused no-egress coverage.

## External gates

Production daemon configuration requires an explicit clock provider. Startup
rejects a missing provider; the cached health probe rejects missing, stale, or
unhealthy samples. Unhealthy clock status blocks new dispatch and
expiring-token authentication while preserving cancellation and recovery paths.
A read-only live numpi check on 2026-09-28 found `systemd-timesyncd` active,
`NTPSynchronized=yes`, and no `chronyc` executable; the earlier Chrony
assumption did not match the host. The exact timesyncd service-and-sync
predicate passed through fleetctl JSON execution. Revalidate during deployment,
set the provider explicitly in the private fleetqd config, and retain target
evidence before dispatch.

This workspace has no usable user systemd bus or Slurm command installation;
Podman cannot initialize its runtime here. The disposable systemd/cgroup v2
and local Slurm semantics exercises in §13.3 therefore remain unperformed.
No workstation or cluster canary has run, and no site budget or automation
policy has been approved in this repository. The local fleetctl inventory has
16 enabled targets but no binding for this project; its doctor reports stale
configuration-key and missing-directory warnings. No fleetqd config or
enabled-target evidence was present for this workspace. A read-only numpi check
also found no installed fleetqd config, executable, or user service. Enabled-target
evidence is required before production dispatch. The actual Pi performance targets, real delivery
soak, restore/upgrade exercises, and sustained agent use remain unmeasured.
The first rtx2080ti candidate timed out on direct fleetctl smoke, and numpi
could not reach its SSH port on 2026-09-28. rtx3090-1 was reachable but both
GPUs were busy. The operator selected rtx4090 instead; it was reachable and
idle, and a disposable no-workload user-systemd unit succeeded. Its linger
setting was off. The installed uv-managed Python 3.12.14 meets the runtime
minimum, but its durable `/data` filesystem had only 189 MiB available
(reported 100% full); the shim rejects `/var/tmp` as a control root, and no
other owner-writable durable location with free space was found. Do not stage
or enroll a control root there until storage and linger are addressed. No GPU
canary command has been sent. A read-only alternate scan found ada1 idle with
537 GiB free and an installed Python 3.12. The operator authorized ada1 as
`ayand`, and `loginctl enable-linger ayand` succeeded. A live user unit enforced
`memory.max=67108864` and `memory.swap.max=0`; stopping a disposable shell with
one child removed its cgroup. Ada1's wall clock was 42 minutes 48 seconds
behind synchronized numpi on 2026-09-28, with `NTP=no` and
`NTPSynchronized=no`. The operator directed us not to synchronize it. No
GPU workload or fleetq submission was sent. The other reachable scanned
workstations had busy GPUs or insufficient access.

Numpi's home and default fleetq state path are on `/dev/mmcblk0p2` (SD card).
Its ext4 `/mnt/D` and `/mnt/E` volumes are on a rotating USB hard drive.
Its USB SSD `/mnt/t7` is exFAT and contains existing data. Thus the approved
ext4 SSD state/backup path required by the plan has not been provided; no
fleetqd release, config, database, or user service was installed on numpi.

Fleetctl now refuses direct `script` execution on login targets even with
`--admin`. Administrative `exec` commands and interactive SSH still exist
there, both behind an explicit `--admin` override. Preventing a human from manually running compute
requires site policy and account controls beyond fleetq's job-dispatch path.
An isolated local installer exercise could build the generated files, but the
locked dependency install stopped because its pinned wheels are not cached in
this offline workspace. It did not touch a deployed service. The available
Python 3.12 links SQLite 3.53.1, while the populated test virtualenv uses
Python 3.14 with SQLite 3.51.2; the target interpreter and SQLite provenance
still need a locked installation and deployment check. On numpi, system Python
3.12.3 links SQLite 3.45.1 without a WAL-reset backport recorded in the
installed package changelog. An already installed uv-managed Python 3.12.14
links SQLite 3.53.1 and can be selected explicitly for installation; the
locked dependency install and state-volume check remain to be exercised there.
Artifact retention uses
an offline, operator-invoked two-pass expiry and purge after 180 days; old
terminal-job event details are compacted after 90 days while the event timeline
stays. Neither operation has been exercised against a deployed state volume.
Remote cache GC is now implemented for bare nodes and Slurm shared roots, but
has not been exercised on a disposable remote target. Live or uncertain
references and unresolved artifact finalization remain pinned; job history is
retained. A corrupt cached bundle requires the explicit operator
repair in [OPERATIONS.md](OPERATIONS.md). Current behavior keeps data and fails
or defers safely under quota.
