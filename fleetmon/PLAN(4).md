# Fleet Usage Monitor — KISS implementation plan

## 0. Final architecture decision

Run exactly one long-lived service: the **hub** on the always-on machine
(intended to be the Raspberry Pi).

Direct compute machines receive a small, stateless `fleetmon snapshot` helper.
The hub invokes it through `fleetctl`, receives one bounded JSON document, stores
the result in SQLite, and serves the dashboard. The helper exits after every
snapshot.

```text
DIRECT WORKSTATION/COMPUTE HOSTS          SCHEDULER LOGIN TARGETS
no Fleetmon service                       no Fleetmon service
fleetmon snapshot                         squeue / sacct
         |                                      |
         | fleetctl exec every ~60 s            | fleetctl exec --admin
         +------------------+-------------------+
                            v
                      ONE HUB SERVICE
              scheduler + SQLite + FastAPI
                            |
                            v
                     local dashboard
```

This deliberately removes remote daemons, spools, byte cursors, segment
rotation, remote service persistence, and recovery synchronization. If the hub
is stopped or a host is unreachable, that interval is an explicit data gap.
Do not add remote spooling until real operation proves those gaps unacceptable.

Both hub and helper require **Python 3.10+**. A virtual environment does not
upgrade its base interpreter. A host without an available Python 3.10+
interpreter is reported as `unsupported_python`; Fleetmon never installs or
replaces system Python automatically.

### Implementation checkpoint

Updated 2026-09-05 after the authorized canary gate on `rtx2080ti` passed.
The bounded snapshot, strict protocol, discovery/admission, poller, SQLite
storage (schema v2 with online backup), Slurm allowlist, read-only web API
with charts, local hub installer (lingering check, upgrade backup/rollback),
and the full local test suite (208 tests on Python 3.10 and 3.14) are
implemented. The canary gate passed end to end: user-local Python discovery,
hash-verified helper install through explicit-target `fleetctl`, two valid
polls, bounded failure injection, kill switches, hub restart integrity, and
dashboard visibility.

Still deliberately open:

- rtx3090-1 is out until its home filesystem's I/O errors are repaired
  (`unsupported_python`/transport failures root-caused to `/data` failing);
  rtx6000 is offline and intentionally excluded by the owner;
- the fleet has polled for under an hour; the one-week observation window
  and fleet-scale measurement refinement (helper CPU budget at 1–1.2% vs
  the 1% target) remain open.

Deployed and verified live (2026-09-05): the hub runs as a systemd user
service on numpi (lingering enabled), serving the dashboard on the Tailscale
address with Bearer authentication; helpers are installed on all ten
reachable workstation/compute targets; Slurm accounting polls the site's
Slurm 22.05 login node with timezone-correct windows; measured hub load is
0.4% of one core and 49 MiB RSS (budgets 5% / 250 MiB); dashboard queries
over the tailnet answer in 15–55 ms (budget 250 ms); projected 30-day
database size is under 1 GiB; and GPU-free notifications flow through a
self-hosted ntfy server on the same host (tailnet-only), two-consecutive-
observation semantics, zero extra remote polls.

The GPU-free notification feature was implemented at explicit owner request,
overriding this plan's original deferral; its evidence rules (committed
samples only, two consecutive FREE observations, stale/partial as UNKNOWN,
no retry-until-success) are unchanged from section 13.

Scheduler targets are verified live against the site's Slurm 22.05 login
node: `squeue --json` (with a bounded text fallback), timezone-correct
`sacct` accounting windows (`[polling] scheduler_timezone`; Slurm 22.05
accepts only naive local timestamps) with a tested compatibility field-set
fallback, and queue freshness kept separate from accounting degradation.

`fleetmon smoke TARGET` now implements the full ten-phase acceptance
procedure (admission, helper presence, two interval-spaced polls, storage,
budget, local-only failure injection, integrity, dashboard visibility, kill
switch) and passed end to end on the canary.

Do not deploy fleet-wide; roll out one target at a time and observe.

## 1. Scope

### MVP

The first release provides:

1. dynamic discovery through `fleetctl`;
2. direct-host CPU, load, RAM, root-disk, NVIDIA GPU, user, and bounded process
   snapshots;
3. Slurm queue/accounting status from scheduler login targets;
4. 60-second historical samples in SQLite for 30 days;
5. overview, host, jobs, and hub-status pages;
6. one hub systemd service, helper installation, smoke tests, and clear failure
   states.

Not in the MVP:

- remote background agents or telemetry spools;
- automatic workload kill, restart, renice, or scheduler cancellation;
- automatic fleet-wide installation or upgrade;
- full process command lines or environment variables;
- suspected-stale classification;
- phone notifications;
- compressed archives, Prometheus, Grafana, brokers, containers, or a JS build
  chain.

### Non-negotiable guardrails

These are enforced in code and covered by tests:

- Runtime remote operations are read-only.
- Only enabled `workstation`/`compute` targets on a direct protocol may run the
  snapshot helper. Never run it on `login`, `bridge`, or `storage` roles.
- Slurm login commands require `fleetctl exec --admin` and are restricted to
  scheduler inspection.
- Subprocesses receive argv arrays; never use `shell=True` or interpolate
  target/output data into a shell command.
- Poll interval, concurrency, launch rate, timeout, stdout/stderr, JSON depth,
  process/user counts, DB batches, API rows, chart points, and logs are bounded.
- A global polling kill switch and per-target disable stop new polls without
  deleting history or editing fleet inventory.
- There is one SQLite writer. Each accepted snapshot is committed in one
  transaction.
- The local hub installer supports `--dry-run` and atomic rollback. A real
  helper installer may be enabled only after its one-target, canary, and
  rollback contract is implemented and tested.
- No retry loop runs until success. Failures back off and never block other
  targets.
- Missing, partial, stale, truncated, and unsupported data are explicit; they
  are never converted to zero or silently discarded.
- Remote strings are treated as untrusted in logs, SQL, JSON, and HTML: use
  parameterized SQL, contextual escaping/`textContent`, and sanitized error
  classes rather than raw stdout/stderr.
- The default web listener is loopback-only. Non-loopback access is refused
  unless authentication is configured.

## 2. Project layout and commands

Use one CLI with small modules. KISS means few states and processes, not putting
the entire application in one untestable file.

```text
fleetmon/
├── pyproject.toml
├── requirements-helper.lock
├── requirements-hub.lock
├── src/fleetmon/
│   ├── cli.py
│   ├── snapshot.py
│   ├── discovery.py
│   ├── poller.py
│   ├── database.py
│   ├── slurm.py
│   └── web/
│       ├── app.py
│       ├── templates/
│       └── static/
├── packaging/fleetmon-hub.service
├── scripts/install-helper
├── scripts/install-hub
├── tests/
├── PLAN.md
└── README.md
```

Commands:

```text
fleetmon snapshot
fleetmon hub
fleetmon doctor [--target TARGET]
fleetmon status
fleetmon maintain
fleetmon install-helper [--dry-run] TARGET
fleetmon install-hub [--dry-run]
fleetmon smoke TARGET
```

`snapshot` imports only helper dependencies (`psutil` and the NVML binding).
Hub-only dependencies such as FastAPI are never installed or imported on
compute machines. Lock helper and hub dependencies separately.

Non-secret configuration lives in `~/.config/fleetmon/config.toml`. Web or
notification secrets, if later enabled, live in a mode-`0600` credentials file.
Configuration validation fails closed: an invalid interval, limit, target
filter, bind address, or executable path prevents startup.

## 3. Fleet discovery and admission

Use the installed machine-readable interface:

```text
fleetctl list --json
```

Do not parse the human table. It returns protocol names, so resolve each distinct
protocol once per five-minute inventory refresh with:

```text
fleetctl protocol show NAME --json
```

Default direct-target admission requires:

```text
enabled == true
role in {workstation, compute}
resolved protocol kind == direct
```

An optional include/exclude tag may narrow the set. Protocol name alone is not
enough because a bridge can also use a direct protocol. Never hard-code a host
name such as `amd-login` to determine safety.

Scheduler collection admits only enabled `login` targets whose resolved
protocol kind is scheduler-backed.

On hub startup:

1. use an absolute configured path to `fleetctl`;
2. require `fleetctl doctor` to report no problems;
3. validate the JSON inventory/protocol fields;
4. use explicit target names in every command—never a project default;
5. verify transport is non-interactive under the service environment.

If inventory refresh fails, retain the last valid inventory and show it as
stale. Newly admitted targets show `helper_missing` until installed. Removed,
disabled, or role-changed targets stop receiving new polls and are marked
`retired`; history is not deleted.

## 4. Stateless direct-host snapshot

`fleetmon snapshot` performs one bounded observation and prints one JSON object
to stdout. It writes no telemetry state, opens no port, starts no child metric
commands, and exits.

### Observation method

To measure current CPU without persistent remote state:

1. record host CPU counters and visible process CPU times;
2. wait for a short monotonic measurement window (initially 250 ms);
3. read host/process counters again;
4. compute host CPU and per-process CPU cores from the deltas;
5. collect RAM, root-filesystem `statvfs`, and NVML data;
6. aggregate all visible processes per user, select bounded process detail,
   serialize, and exit.

Two short process-counter passes once per minute are acceptable only if the
canary load test meets the CPU budget. Do not call `ps`, `top`, or `nvidia-smi`
per process. Initialize NVML once per snapshot and query each physical GPU and
its compute-process list once.

### Wire document

The response includes:

- wire schema version;
- UTC capture time, monotonic observation duration, and OS boot ID;
- helper version and collection duration;
- CPU logical count, busy fraction, and 1/5/15-minute load;
- RAM/swap totals and root-filesystem total/free bytes;
- per physical GPU: stable UUID, current index, model, utilization, VRAM,
  temperature, power, compute-process count, and support/error flags;
- per visible user: UID, username when visible, CPU cores, summed process RSS,
  process count, GPU-process count, and attributed VRAM;
- bounded process detail: PID, create time, UID/username, process name,
  executable basename, CPU cores, RSS, GPU UUID/index, and VRAM;
- visibility, truncation, permission, and unsupported-field metadata.

Metric rules:

- `1.0` process CPU means one fully busy CPU core.
- Store bytes; convert units only in the UI.
- Summed process RSS can double-count shared pages and is labeled sampled RSS.
- NVML device utilization is device-wide and is never attributed to a user or
  process. Per-user GPU data is limited to visible process occupancy/VRAM.
- Unsupported, permission-denied, unavailable, and first-window values are
  `null` with a reason, not zero.
- GPU UUID is identity; index is only the current display position.
- NVML/driver absence does not break CPU/RAM collection. It makes GPU data
  unavailable and the snapshot partial; a GPU-declared target shows a prominent
  capability error.

MIG is not implemented speculatively. If MIG mode is detected, report
`mig_detected=true`, expose only metrics known to be correct at the physical-GPU
level, and mark instance availability unsupported. Add MIG-instance collection
only after confirming it is enabled and defining non-double-counting tests.

### Privacy and bounds

Aggregate every visible process before selecting detail. Prioritize:

1. GPU-compute processes;
2. top CPU processes;
3. top RSS processes.

Initial hard caps:

```text
measurement window       250 ms (maximum 1 s)
process detail           80 records
user aggregates          128 records
encoded JSON             256 KiB
collection deadline      5 s cooperative; late snapshots are rejected
hub outer timeout        20 s, including a stuck library/kernel call
```

If a cap is reached, return visible/emitted counts and `truncated=true` while
preserving aggregates where possible. The UI says “bounded process snapshot”
and never claims to list every process.

Collect process name and executable basename by default. Full arguments often
contain credentials, dataset paths, or private identifiers, so do not collect
them. Never read environment variables. `/proc` restrictions, process exit
races, and NVML permission/errors produce partial data rather than a failed
snapshot when safe to do so.

Store UID and username, but aggregate fleet-wide identities only through an
explicit mapping. Equal UIDs or usernames on different hosts are not assumed to
be the same person. Slurm accounts are not merged with direct-host users by name.

## 5. Helper installation

There is no remote Fleetmon service.

`fleetmon install-helper TARGET`:

1. resolves and re-checks target role/protocol;
2. refuses login/bridge/storage targets;
3. checks for an explicitly available Python 3.10+ interpreter, NVML
   library/driver visibility, and a writable install directory; missing NVML is
   recorded but does not block CPU/RAM-only snapshots;
4. builds/transfers a locked helper wheel through explicit-target `fleetctl`
   operations—never raw SSH/SCP;
5. creates a versioned user-local virtual environment;
6. runs `fleetmon snapshot` and validates its schema/limits;
7. atomically switches a `current` symlink;
8. records the verified absolute helper path in hub-local operational state;
9. rolls back the symlink if the new helper fails.

It never uses root, modifies system Python, installs a service, starts a
background process, or changes fleet inventory. `--dry-run` resolves and prints
the target, Python, paths, and commands without changing the host.

Roll out manually: one canary, inspect results, then one target at a time. Keep a
local outcome table: installed, updated, unchanged, skipped, unreachable,
unsupported_python, incompatible, or rolled_back. No automatic update loop.

## 6. Hub poller

Run one `fleetmon hub` process with an async scheduler, one SQLite writer, and
one-worker Uvicorn. Blocking subprocess/SQLite work must not block the HTTP
event loop.

Direct-host defaults:

```text
poll interval             60 s (hard minimum 30 s)
inventory refresh         5 min
SSH launch rate           <= 1 command / 2 s
SSH concurrency           <= 2 fleet-wide
in-flight polls           <= 1 per target
outer process timeout     20 s; kill the complete process group
stdout                    <= 256 KiB plus small fleetctl overhead
stderr                    <= 64 KiB
```

Evenly stagger targets over the poll interval using deterministic target-based
jitter. Invoke the verified absolute helper path with
`asyncio.create_subprocess_exec()` and explicit argv. Stream stdout/stderr with
limits; do not call `communicate()` into an unbounded buffer.

Validate exit status, JSON size/depth, schema version, types, finite numeric
values, field ranges, record counts, boot ID, and capture-time sanity before
queuing a DB transaction. A malformed document inserts only a bounded poll-error
record; it never partially inserts telemetry.

Failure backoff is approximately 2, 5, 10, then 20 minutes with bounded jitter.
Do not retry immediately. A successful valid snapshot resets backoff. One slow
or unreachable host never blocks another.

The hub generates a unique `poll_id` before each command. A valid response and
all child rows commit once under that ID. There is no remote cursor and no retry
of an old snapshot. If the hub dies before commit, that one sample is lost and
the gap is explicit; the next scheduled poll is independent.

Global `polling_enabled=false` and per-target disables prevent new commands.
They do not terminate workloads, alter inventory, or delete data. The hub checks
this guard immediately before every subprocess launch. A service stop is the
emergency global stop; disables live in validated local configuration and take
effect after a hub restart. Dynamic configuration reload is intentionally not
part of the MVP.

## 7. SQLite and retention

At a 60-second cadence, store one resolution only. Do not build raw/minute tiers
or compressed archives in the MVP.

Core tables:

```text
schema_migrations
hosts                 # inventory, helper version/path, health, backoff
polls                 # poll_id, target, start/end, outcome, bounded error
host_samples          # one row per accepted direct-host poll
gpu_samples           # one row per poll/GPU UUID
user_samples          # one row per poll/host-local UID
current_processes     # replace bounded snapshot after each valid poll
slurm_jobs
slurm_poll_state
```

Use WAL mode, foreign keys, a busy timeout, prepared statements, and one writer.
Commit a snapshot's poll/host/GPU/user/current-process updates in one
transaction. API handlers expose query-only methods and run their bounded
SQLite calls outside the HTTP event loop. Add separate SQLite read-only
connections only if measured contention justifies the extra lifecycle code.

Indexes are limited to actual queries: target/time, GPU UUID/time,
target+UID/time, poll outcome/time, and active Slurm state. Verify them using
`EXPLAIN QUERY PLAN`.

Retention:

- keep host/GPU/user/poll history for 30 days initially;
- keep current processes only until the next valid snapshot or until their
  source poll expires, whichever happens first; a truncated or partial process
  snapshot replaces the view but remains clearly labeled;
- keep active Slurm jobs regardless of age and terminal jobs for 30 days;
- delete expired rows daily in bounded batches;
- run passive WAL checkpoints; never routine `VACUUM`;
- keep journal/file logs size-bounded.

Store capture time and hub receive time. Use receive/progress time for liveness
so a bad host clock cannot mark itself fresh. Display capture-time skew. All
stored timestamps are UTC; the browser converts for display.

Place DB/WAL files on a local filesystem with directory mode `0700` and files
mode `0600`. The hub holds a singleton lock and performs routine maintenance
itself. The manual `maintain` command refuses while the hub lock is held and runs
only while the service is stopped; it never opens a competing writer.

Monitor DB/WAL size and free disk. Below a configured reserve, stop launching
new polls before collecting data that cannot be committed, checkpoint the WAL,
and show a critical status. Never silently shorten retention. Back up SQLite
with its online-backup API before schema migrations and test restoration.

If historical telemetry matters beyond operational convenience, configure one
daily online backup to a separate filesystem and retain a small fixed number
(initially seven). If no separate backup path is configured, report
`backup_disabled`; copying onto the same failing disk is not a backup.

Thirty-day storage feasibility is an acceptance test using the observed/worst
configured GPU and user cardinality. If the measured DB is too large, first
shorten retention or add hourly rollups; do not introduce an archive subsystem
preemptively.

## 8. Slurm collection

Do not install the helper on a scheduler login target. Use explicit targets and
control-plane commands only:

- `squeue --json` every 60 seconds when supported;
- `sacct` every 5 minutes with explicit parsable fields and a small overlap;
- tested parsable fallback if scheduler JSON is unavailable.

Run these through `fleetctl exec --admin`. Persist the accounting watermark and
upsert idempotently using cluster/job/array/step identifiers. The overlap must
refresh running/recent jobs rather than only discover new start times. Query
failures preserve last-known state and use backoff. Apply the same bounded
process-group timeout, stdout/stderr, and launch-rate controls used for direct
polls.

On first start, query only a configured recent window (initially 24 hours).
Catch up older accounting history only through an explicit maintenance command,
in bounded time chunks. Scheduler output has a separate 1-MiB hard cap.

Clearly distinguish allocated/requested resources from measured usage.
Scheduler fields do not provide AMD GPU utilization or prove that a job is
compute-idle. Cross-user visibility depends on site permissions and is tested
during smoke setup.

Heuristic scheduler flags such as `LONG PENDING` or `NEAR LIMIT` are deferred
until real scheduler data provides defensible thresholds. When added, they use
scheduler fields only and are labeled `REVIEW`, never `STALE`.

## 9. Health states

Keep transport and telemetry meaning explicit:

```text
live                recent valid snapshot progressed
stale               no valid snapshot for about 2 poll intervals
unreachable         repeated fleetctl/SSH transport failures
helper_missing      admitted target lacks the snapshot helper
unsupported_python  no configured Python 3.10+ interpreter
unsupported         NVML/helper prerequisite failure
version_mismatch    helper wire version incompatible with hub
partial             fresh snapshot with restricted/unsupported fields
retired             target no longer admitted
slurm_stale         scheduler query exceeded freshness threshold
polling_disabled    global or per-target kill switch active
```

Show last captured, last received, last successful poll, current backoff,
collection duration, clock skew, truncation/visibility, and bounded error class.
Never reduce all of these to one green/red dot.

## 10. Dashboard and access

MVP pages:

- **Overview:** host, state, freshness, CPU, load, RAM, root disk, GPU
  utilization/VRAM, active visible users, and error.
- **Host:** CPU/load/RAM/disk charts, per-GPU charts, current per-user totals,
  and bounded current processes.
- **Jobs:** Slurm jobs. Bounded current processes stay on each direct host page
  so the MVP does not duplicate the same data and controls.
- **Hub status:** polling enabled, queue/in-flight counts, poll latency/errors,
  DB/WAL/free-disk size, hub CPU/RSS, versions, retention, and backup status.

Use plain server templates, CSS, and small JavaScript. Vendor one pinned chart
library locally; do not require a CDN or npm build. Use compact tables, solid
chart lines, tabular numerals, text/icons as well as status color, keyboard
accessibility, and a collapsible navigation drawer on small screens.

Browser/API guards:

```text
overview refresh        30 s
host/jobs refresh       60 s
chart points            <= 2,000 per series
table/API rows          paginated and hard-capped
time ranges             validated and capped
Uvicorn workers         exactly 1
```

Pause browser polling while the tab is hidden. Only the active page polls. One
request returns a complete chart group; never one request per GPU/metric.
Browsers query only the hub and can never trigger a remote poll directly.
Render all target, process, user, scheduler, and error strings as text; never
place remote values into `innerHTML`, script, style, or URL contexts.

Default listen address is `127.0.0.1:8088`, accessed through an SSH tunnel. On a
dedicated single-user hub, this is the simplest default. Any non-loopback bind
requires authentication and must be protected by a maintained TLS reverse proxy
or encrypted trusted VPN. State-changing future endpoints require CSRF
protection. Health endpoints reveal only readiness status unless authenticated.

## 11. Hub service and upgrades

Install only `fleetmon-hub.service` as a long-running service. Use a dedicated
Python 3.10+ virtual environment and locked dependencies.

Preferred startup is `systemd --user` with lingering explicitly verified, or a
system unit under a dedicated unprivileged account if an administrator chooses
that model. Do not call `nohup` a service. A cron fallback is not part of the
MVP.

The unit sets explicit `HOME`, `PATH`, fleet config/state/cache paths, umask, and
working directory. It must not depend on an interactive shell or SSH agent.
Use `Restart=on-failure`, bounded restart delay, graceful SIGTERM, and readiness
only after inventory, DB migration, writer, scheduler, and web listener start.

Upgrade flow:

1. disable new polling and drain the DB writer;
2. create an online SQLite backup if the schema changes;
3. stage the new release/venv;
4. run migrations transactionally;
5. switch atomically and start;
6. require readiness plus one canary snapshot;
7. roll back application/schema when the migration explicitly supports it,
   otherwise restore the backup.

## 12. Tests and rollout gates

### Local tests

- inventory/protocol role admission and role change;
- helper timeout, output overflow, invalid JSON/schema/types/non-finite values;
- process exit/permission races, PID reuse, large process/user counts;
- NVML missing/error, GPU UUID/index reorder, and MIG-detected partial mode;
- DB transaction rollback, duplicate poll ID, writer restart, and migration
  backup/restore;
- clock skew, UTC/browser conversion, retention batches, WAL/free-disk guard;
- scheduler JSON/parsable fixtures, arrays/steps, overlap, and permissions;
- global/per-target kill switches and browser inability to initiate remote polls;
- authentication requirement for non-loopback binding.

### One-host smoke test

Use an explicitly admitted canary such as `rtx2080ti`:

```text
fleetmon smoke rtx2080ti
```

It must verify:

1. role/protocol admission and non-interactive `fleetctl` transport;
2. Python 3.10+, psutil, NVML, and helper path;
3. bounded snapshot schema and privacy fields;
4. two valid polls about 60 seconds apart;
5. transactional host/GPU/user/current-process storage;
6. CPU/RSS/remote-duration budget;
7. live/stale/unreachable/malformed-response states;
8. hub restart without corruption;
9. API/chart/dashboard visibility;
10. global polling kill switch stops new remote commands.

Then test one host for each production Python version and one high-GPU/high-
process-count host. Only then install the helper on remaining targets one at a
time.

### Hard limits

```text
direct poll interval        >= 30 s (default 60 s)
SSH launch rate             <= 0.5/s
SSH concurrency             <= 2
in-flight per target        <= 1
remote timeout              <= 20 s
snapshot stdout             <= 256 KiB
snapshot process rows       <= 80
snapshot user rows          <= 128
Slurm squeue interval       >= 60 s
Slurm sacct interval        >= 300 s
scheduler stdout            <= 1 MiB
scheduler query chunk       <= 24 h
chart points                <= 2,000/series
all queues/API rows/logs     explicitly bounded
```

Measured acceptance targets:

- one snapshot normally completes in under 2 seconds and consumes less than 1%
  of one compute-host core averaged over a minute;
- hub at roughly 12 hosts stays below 5% of one hub core when the dashboard is
  idle and below 250 MiB RSS excluding page cache;
- indexed dashboard queries normally complete below 250 ms;
- projected 30-day DB size leaves the configured disk reserve.

If measurements fail, reduce process detail or polling/retention before adding
infrastructure.

## 13. Deferred features and the evidence required

Add a deferred feature only when its trigger is observed:

- **Remote spool/service:** only if hub outages create unacceptable gaps or a
  measured requirement needs sub-30-second samples.
- **Hourly rollups/archive:** only if the measured 30-day SQLite size or query
  time exceeds its budget.
- **MIG instance monitoring:** only after MIG is detected on a target and tested
  without double-counting physical and instance capacity.
- **GPU-free phone notification:** only after base telemetry is reliable. Reuse
  committed samples, require two or more consecutive FREE observations, treat
  stale/partial as UNKNOWN, and add zero remote polls.
- **Suspected-stale workload evidence:** only after enough baseline data exists
  to choose thresholds. Use sustained CPU/GPU/VRAM evidence and coverage; never
  auto-kill or label the result as proof.
- **External metrics stack:** only if a concrete integration consumer appears.

## 14. Implementation order

1. Define the bounded snapshot JSON schema and metric semantics.
2. Implement/test `fleetmon snapshot` on Python 3.10+.
3. Implement exact `fleetctl` discovery and one-target polling.
4. Implement SQLite transaction, health state, retention, and disk guard.
5. Pass the one-host smoke test, including kill switch and failure injection.
6. Add the minimal overview/host/hub-status dashboard.
7. Add Slurm polling and Jobs page.
8. Implement helper/hub dry-run installers and rollback.
9. Run the Python-version and worst-host load matrix.
10. Roll out one target at a time and observe for at least one week before any
    deferred feature.

## 15. Known limitations accepted for simplicity

- No hub means no samples; downtime becomes a visible gap.
- A one-minute snapshot can miss short-lived processes and utilization spikes.
- The 250-ms CPU observation is noisy and represents a sample, not accounting.
- Process detail is bounded and current-only.
- Device-wide NVIDIA utilization cannot be assigned accurately to a user or
  process; MPS/containers can further limit attribution.
- Host rename is a new logical target unless an explicit alias is configured.
- Slurm data is scheduler/accounting data only; AMD compute-node utilization is
  not measured.
- Hosts without Python 3.10+ remain unsupported until separately upgraded.
- The resource budgets must be measured on the actual hub and busiest hosts.

These limitations are preferable to hiding complexity inside background agents,
distributed state, or recovery protocols before there is evidence they are
needed.
