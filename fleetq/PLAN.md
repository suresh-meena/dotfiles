# Plan: `fleetq` (`fq`) — a Slurm-like scheduler for the fleet, hosted on numpi

## Context

Running jobs across ~11 GPU workstations and 3 university Slurm clusters is manual, and it is actively harmful.

- **Workstations.** `fleetctl submit` *blocks* until the script exits. It records no job id and cannot detach, reattach, report status or cancel, and the `job` verbs are refused on workstations.
- **Clusters.** Jobs are babysat by hand. The laptop's fleetctl audit log shows ~460 `exec --admin` calls to kiac-ayand and wellsfargo, in bursts ~8 s apart. That is agents polling `squeue`: the "constant helper on kiac" load the login-node rules forbid.

**Goal:** one always-on scheduler on **numpi**, the aarch64 Pi on the LAN and tailnet that already runs the fleetmon hub and ntfy, with 222 GB free.
- Humans and agents on any machine submit to it, and it holds a queue they can manipulate.
- It dispatches to two kinds of node:
  - **Bare-metal workstations.** *We are the allocator*: fq must find GPUs that are genuinely idle, never launch blindly, and coexist with labmates on shared boxes.
  - **Slurm clusters.** Only `sbatch` through a login node; Slurm does the allocating. Nothing is installed on the cluster and nothing runs there persistently.
- It is the **single, rate-budgeted poller** of each login node. Agents block on `fq wait`, never on `squeue`.
- Jobs are visible in fleetmon. fleetctl stays an independent core utility underneath, used only as a subprocess.

## Decisions (with the user)

| Question | Decision |
|---|---|
| Code movement | Snapshot by default: `submit` bundles the cwd (with fleetctl's sync excludes) to numpi, and numpi pushes it at dispatch. `--in-place <path>` runs code already on the node; it requires `--on`/`--each`, because placement can't know where the path exists. |
| Multi-target | `--on a,b` = any one of them (runs once). `--each a,b` = one job per machine, grouped. |
| Shared workstations | Per-node `mode = shared \| exclusive`. Shared nodes only use GPUs verified idle, and every job is pinned by GPU UUID. |
| Clusters | Opt-in per job (name the cluster or pass `--allow-clusters`). Caps and preflight always apply. |
| Build vs buy | Build a thin daemon; no existing system fits (Appendix A). |
| Stack | Python 3.11+, asyncio, SQLite, FastAPI/uvicorn (pinned to fleetmon's `requirements-hub.lock`, already vetted on numpi). The work is I/O-bound SSH, so Rust/Go gains nothing, and this reuses fleetmon's bounded-subprocess runner and Slurm parsers. |
| Names | Package/project `fleetq` at `/home/suresh/Projects/dotfiles/fleetq/`. CLI `fq` plus a `fleetq` alias, because `fq` is also wader/fq in some distros; `fq doctor` checks which one is on PATH. Daemon `fleetqd`, node shim `fq-node`. |

## Architecture

```
 laptop / agents / any machine                      numpi
 ┌───────────────┐   tailnet HTTP, bearer token   ┌─────────────────────────────────┐
 │ fq (one file, │ ─────────────────────────────▶ │ fleetqd (systemd --user,asyncio)│
 │ stdlib only)  │   PUT /api/v1/bundles/sha256:… │ API · scheduler · dispatcher ·  │
 └───────────────┘                                │ reconciler · SQLite WAL (FULL)  │
                                                  │        │ subprocess only        │
 fleetmon hub ──── GET /api/feed/v1/capacity ────▶│        ▼                        │
   (Queue page reads fleetqd's read API)          │  fleetctl: admission, routes,   │
                                                  │  sync, preflight, sbatch, budget│
                                                  └──────┬──────────────────┬───────┘
                                   ssh ControlMaster      │                  │ ssh → login node only
                     ┌────────────────────────────────────▼──┐  ┌────────────▼─────────────┐
                     │ workstation: fq-node = one-shot shim   │  │ cluster: sbatch/squeue/  │
                     │ (not a daemon) + systemd --user        │  │ scancel. All job logic   │
                     │ transient unit per job attempt         │  │ lives INSIDE the batch   │
                     └────────────────────────────────────────┘  │ script (Slurm spools it) │
                                                                 └──────────────────────────┘
```

- **fleetqd is separate from the fleetmon hub**, for four reasons:
  - fleetmon's guardrail is "runtime remote ops are read-only", and fq writes: launch, sbatch, cancel.
  - fleetmon's web server shares its event loop with its poller and caps concurrency at 32, which is no place for hundreds of long-polls.
  - Its CSP (`form-action 'none'`, `connect-src 'self'`) forbids action forms.
  - Telemetry can drop a sample, but the queue cannot (`synchronous=FULL`).
- **fleetctl is only a subprocess**, never an import. Its admission rules, audit log and role guardrails then apply to every fq action. We rejected importing it because it has unlocked shared state (`routes.json`, known_hosts creation, audit trim), and because `build_plan` reads `Path.cwd()` and mutates args.
- **Both daemons share `FLEET_STATE_HOME`/`FLEET_CACHE_HOME` on numpi.** They therefore share SSH ControlMaster sockets and the login-node budget.
- **Nothing is installed on clusters.** Bundle extraction, the rc file and `$SLURM_JOB_ID` capture all happen inside the batch script. Slurm copies the script into its own spool at submit time, so fleetctl's 14-day stage prune can't break a long-pending job.

## The hard part: bare-metal GPU placement

On Slurm, the cluster allocates GPUs. On a workstation, fq must *prove* a GPU is free before launching, and keep checking afterwards. Each subsection below covers one pitfall and the mechanism that handles it.

### 1. Identity — always the GPU UUID
- **The pitfall:** `nvidia-smi` and NVML (and fleetmon) number GPUs in PCI-bus order, while CUDA defaults to `CUDA_DEVICE_ORDER=FASTEST_FIRST`. On a mixed-GPU box, "index 2" names different cards to the two.
- **Mechanism:** every allocation row, lock and marker is keyed by `GPU-<uuid>`. At launch fq sets:
  - `CUDA_VISIBLE_DEVICES=GPU-<uuid>,…`
  - `CUDA_DEVICE_ORDER=PCI_BUS_ID`
  - `NVIDIA_VISIBLE_DEVICES`, for containers
  - `FQ_GPUS` (UUID plus index, for people)
- **Other cases:**
  - MIG GPUs are skipped in v1 (fleetmon flags `mig_detected`).
  - `compute_mode` is probed. An `EXCLUSIVE_PROCESS` GPU is reported, because multi-process CUDA jobs fail on it.
  - v1 bare metal is NVIDIA-only. The MI210s are behind Slurm; an owned AMD box would later get `ROCR_VISIBLE_DEVICES` by UUID.

### 2. "Idle" — a stricter predicate than fleetmon's
fleetmon's `classify_gpu` (`fleetmon/src/fleetmon/notify.py`) says idle after two fresh readings with **util == 0 and compute_process_count == 0**. It ignores `vram_used_bytes` (which it records in `snapshot.py`), and NVML utilization is an instantaneous sample.

| Case | fleetmon says | Reality | What catches it |
|---|---|---|---|
| Labmate's idle Jupyter kernel holding 20 GB at 0% | busy if the process is visible, idle if not | **busy**: they own it | `memory.used > idle_mem_mib` |
| Process in another PID namespace (container) or `hidepid` /proc | idle | busy | memory used |
| Xorg/display holding 200–500 MiB on GPU 0 | idle | usable | threshold (default 1024 MiB, per-node override) or `reserve_gpus` |
| Labmate's loop relaunching every run with a ~10 s gap | idle during the gap | about to be busy | sustained-idle window |
| Our own job during its first seconds (0%, ~0 MiB) | idle | **ours** | fq allocation table, which is authoritative |
| Zombie context holding memory after a crash | mixed | unusable | memory used → GPU drained with a reason |

**Placeable(g) on a *shared* node** requires all of:
1. g has no fq allocation in DISPATCHING, RUNNING or CANCELLING, and is not in the post-job cooldown (default 60 s).
2. g is not reserved and not drained.
3. No compute apps are on g.
4. `memory.used ≤ idle_mem_mib`.
5. Utilization ≤ 5 %.
6. Items 3–5 held in **every** observation over `idle_window` (default 120 s, at least 2 observations).
7. The newest observation is ≤ 90 s old.

Stale or untrusted data counts as *unknown*, and unknown is **never placeable on shared nodes** (fail safe).

**On an *exclusive* node,** only items 1 and 2 apply, plus items 3 and 4 checked at launch. A foreign process there means someone ran something outside fq: fq won't place on that GPU and raises a warning.

### 3. Two stages: pre-filter on numpi, gate on the node
**Pre-filter (fleetqd, cheap):**
- Reads fleetmon's new read-only, versioned `GET /api/feed/v1/capacity`: raw per-GPU util, vram used/total, process count and owners, availability and reason, plus age, disks and host state, built from fleetmon's `_freshen_host`, `idle_gpus()` and `current_disks`.
- Also uses fq's own last `fq-node probe`. From both it picks candidate GPUs.
- fleetmon samples each host only every ~20–60 s, so this is a hint, not proof.

**Gate (`fq-node launch`, authoritative):**
1. Take `flock <job_root>/.lock`, so two fq dispatches never collide.
2. Re-check the chosen UUIDs with **3 `nvidia-smi` samples 1 s apart**, against predicate items 3–5.
3. Check the node-local allocation markers `<job_root>/alloc/<uuid>`. Each marker carries the fleetqd *instance id*, so a stray second scheduler (for example a dev instance) sees them as foreign and treats the GPU as busy.
4. Write the markers, start the unit, release the lock.
5. If any GPU fails, exit **75 (EX_TEMPFAIL)** with a JSON reason (`gpu_busy:<uuid>`).
   - fleetqd re-places the job *without consuming a retry*, and puts that GPU on a 5-minute cooldown (anti-thrash).

`nvidia-smi` can itself hang on a wedged driver. It always runs under the bounded-probe pattern (§6), and a hang drains the node.

**Latency target**, submit to start on an idle node: bundle push plus launch ≈ 2 SSH calls, roughly 5–12 s including the 3-sample check. Measured in P1.

### 4. Races and contention after launch
- A labmate who doesn't use fq can still grab "our" GPU. The gate shrinks that window to seconds; it can't close it.
- **Foreign processes:** every `fq-node status` call lists the compute apps on each allocated UUID. A PID outside our job's cgroup or process tree triggers `foreign_process_on_allocated_gpu`, an event plus ntfy. fq **never kills** it.
- **The reverse case:** our job's PIDs on a UUID we didn't allocate (code that ignores `CUDA_VISIBLE_DEVICES`) raises a `gpu_escape` warning.
- fleetmon's host and idle-GPU pages mark GPUs "allocated by fq". Labmates then see the GPU is taken even while our job is still loading data.

### 5. Non-GPU contention on shared boxes
- **Checked at launch:** `MemAvailable ≥ --mem` (default is the node's `mem_per_gpu × gpus`), and `free(job_root) ≥ 2 × bundle + --scratch`. Load average is advisory only.
- **Enforced with a transient unit:**

  ```
  systemd-run --user --unit=fq-<job>-<attempt> --collect \
    -p MemoryMax=<mem> -p MemorySwapMax=0 -p CPUQuota=<cpus×100>% \
    -p TasksMax=4096 -p RuntimeMaxSec=<time+grace> -p KillMode=control-group
  ```

  This stops our job from OOM-killing labmates' processes.
- **Delegation caveat:** `MemoryMax` only works when the memory controller is delegated to the user manager (cgroup v2 with a recent systemd). Otherwise it is silently ignored.
  - `fq-node probe` reports `cgroup_memory_enforced`.
  - Where it is false, fq tracks unit RSS on every status poll and flags jobs over `--mem`: a warning in v1, and an opt-in kill with `--mem-hard` in P5.
  - The UI marks such nodes.

### 6. Mounts, failing drives, stale NFS
Real case: rtx3090-1's `/data` home filesystem is a failing ext4, and fleetmon moved its helper to `/data2/ayand`.

- **Local, healthy `job_root` per node.** The default is `~/.local/share/fq`; rtx3090-1 uses `/data2/ayand/fq`. The shim, its cwd and every job dir live there, never on NFS. numpi is tagged `nfs`, so workstations may mount it.
- **Bounded probes.** A stale hard NFS mount puts `stat` or `mountpoint` into D-state, which would hang the SSH command itself. So:
  - **Presence:** parse `/proc/self/mountinfo`, which never blocks.
  - **Liveness:** `stat` in a background subshell that writes to a result file, polled for ≤ 5 s. On timeout, report `hung` and exit anyway, abandoning the stuck child.
  - **Writability:** `touch`/`rm` in `job_root` under the same bound, plus `ro` detection from mountinfo.
- **Per-job paths.** `--needs PATH` failing its probe is a **precheck failure**: retryable, no attempt consumed, with reason `path_missing`, `path_hung` or `path_ro`. The node goes on cooldown.
- **job_root itself.** If `job_root` fails, the node becomes `storage_degraded`: not placeable, and an ntfy goes out.

### 7. Node environment — the gotchas that silently break jobs
- **Wrong environment.** systemd user units get the *user manager's* environment, not your `.bashrc`/`.profile`: no conda, no venv PATH, no module or CUDA `LD_LIBRARY_PATH` exports.
  - The job wrapper runs `bash -l -c` with `--setup "source .venv/bin/activate"` and explicit `--env`/`--env-file`.
  - The submitter's environment is never inherited.
  - `fq-node probe` records `bash -lc 'command -v python3 nvidia-smi'` so `fq why` can explain failures.
- **Login shell on a failing disk.** On rtx3090-1 the home directory is on the failing disk, so even `bash -l` can hang reading `~/.profile`. Per-node `login_shell = false` and `home_override` handle it, and HOME gets its own bounded probe.
- **`systemctl --user` over SSH.** It fails without `XDG_RUNTIME_DIR=/run/user/$UID` (and a DBus address). fq-node exports both before every systemd call.
- **Linger.** Without it, `user@.service` stops when the last SSH session closes, killing a `systemd-run --user` job. The probe picks the executor mode:

  | Probe finding | Mode |
  |---|---|
  | user manager present **and** linger on | `systemd` (preferred) |
  | no linger, logind `KillUserProcesses=no` | `setsid` fallback |
  | no linger, `KillUserProcesses=yes` | **refused**: jobs would die at logout |

  `fq node install` tries `loginctl enable-linger`; lab machines may need their admin to do it.
- **Slurm environment variables.** fq never sets `SLURM_*` on owned nodes. Frameworks such as Lightning auto-detect Slurm from those. Arrays use `FQ_ARRAY_TASK_ID`.

### 8. Teardown, cancel, walltime, leaks, health
- **Cancel:**
  - *systemd mode:* `systemctl --user stop <unit>`, which kills the whole cgroup, including children that `setsid` themselves.
  - *setsid mode:* `kill -TERM -<pgid>`, then KILL after `--kill-grace` (default 60 s), then a `/proc` sweep for processes whose environment carries `FQ_JOB=<id>`.
- **Walltime:** `RuntimeMaxSec`, or `timeout --kill-after=60` in setsid mode. The job ends as **TIMEOUT**.
- **Leaked memory:** if a finished job's UUIDs stay above the memory threshold past the cooldown, the GPU is **drained** (`leaked_memory`) and an ntfy goes out. It auto-resumes after 2 clean idle windows.
- **Per-GPU drains:** a probe error or hang, "fallen off the bus", Xid/ECC or page retirement (`nvidia-smi -q -d ECC,PAGE_RETIREMENT`), leaked memory.
- **Node drains:** 2 failed probes (unreachable), `storage_degraded`, no `nvidia-smi`, a `boot_id` change that lost jobs, or `fq nodes drain`.
- Each drain records who, why and when. Automatic drains clear automatically; manual drains clear only manually.
- **Cancel while the node is unreachable:** the job goes to `CANCELLING` with the intent persisted. The stop is applied on reconnect, and the job is never reported CANCELLED before the stop is confirmed.

## Slurm clusters: the details that still bite

- **How a job is submitted:**
  1. fq renders a small *entry payload*: make the attempt dir, write `$SLURM_JOB_ID`, extract the bundle, run the command under `--setup`, write the rc file atomically.
  2. It calls `fleetctl submit <entry> --target <login> --queue <preset> --account <acct> --time … --gpus … --job-name fq-<job>.<attempt> --output <cluster_job_root>/logs/fq-<job>.<attempt>-%j.out --json`.
  3. The P0 fleetctl change *inlines* the payload into the wrapper, so Slurm's spool holds everything.
  4. wellsfargo (`native_batch_required`) gets a fully native script, rendered by fq from `fleetctl queue list --json`, and submitted with `--native-batch`.
  5. fleetctl preflight runs on every submission, and fq also runs it at submit time. An ERROR is refused immediately (`preflight_refused` plus rule IDs).
- **`--account` is missing today.** fleetctl's wrapper writes `--account` only from `queue.account`. KIAC h200 needs `chiru` with no preset, so h200 is currently unreachable through the queue path. P0 adds `submit --account`, checked by the existing preflight account rules.
- **cluster_job_root** must be on a filesystem the compute nodes can see, set per cluster.
  - KIAC: `/storage` writability is unconfirmed, so default to home.
  - AMD: `/rhome` is 20 GB; `/scratch` is purged weekly.
  - Bundles count against quota, so they are garbage-collected right after the final pull.
  - Slurm opens the output file at job start, so `logs/` is created once per cluster by an idempotent `mkdir -p`.
- **Uncertain submission** (a timeout, or ssh exit 255 after sbatch may have run): never resubmit blindly. First look the job up by name:
  - `squeue -h -n fq-<job>.<attempt>`;
  - `sacct -n --name` where accounting exists;
  - the attempt dir's `slurm_job_id`.

  Resubmit only if all three are empty.
- **Budgeted polling: one SSH call per cluster per cycle.**
  - The call: `fleetctl exec --admin --target <login> -- sh -c '<script>' fq <ids…>`, which runs `squeue --me -h -j <ids> -o '%i|%T|%r|%S|%N'` and then `cat`s the rc and `slurm_job_id` files of attempts that have left squeue.
  - Cadence: every 30 s for 5 minutes after any submit or transition, every 120 s while jobs are live, and **zero calls when fq has no live jobs on that cluster**.
  - `fq wait`/`logs` requests never trigger remote calls.
  - Per-cluster and per-hour call counts are shown in the UI. `fq cluster freeze <name>` is a kill switch.
- **Completion evidence, in order:** rc file (works everywhere) > `sacct` State/ExitCode (not on wellsfargo) > output-tail markers ("DUE TO TIME LIMIT", "oom-kill") > `UNKNOWN_EXIT`.
- **Pending reasons that never resolve:** `AssocGrpGRES` (chiru's 2-GPU group limit), `QOSMaxGRESPerUser`, `PartitionTimeLimit`, "account not permitted to use this partition".
  - The job goes to **BLOCKED(reason)** and an ntfy goes out.
  - If the job listed alternatives (`--on kiac:a100,rtx4090`), fq cancels the cluster copy and re-places the job. There are never two copies alive at once.
- **Caps**, enforced by fq before any submit:
  - per (cluster, account) concurrent GPUs, e.g. `kiac/chiru = 2`, *counting labmates' usage* from fleetmon's squeue data where it is visible;
  - per-cluster maximum queued jobs.
- **Site quirks:**
  - AMD runs Slurm 22.05 with no `--json`, so fq uses pipe formats everywhere.
  - wellsfargo has no sacct, so the rc file plus squeue is the only source of truth.
  - kiac uses sshpass/password auth. A 2FA prompt or a password change becomes BLOCKED(`auth`) plus `cluster_auth_failed`, never a hang.
  - `--collect <glob>` pulls outputs back to numpi on completion. This is essential on AMD `/scratch`.

## Job model, state machine, scheduling

**States:**
- Non-terminal: `HELD`, `PENDING(reason)`, `DISPATCHING`, `SUBMITTED` (pending in a remote Slurm), `RUNNING`, `CANCELLING`, `BLOCKED(reason)` (needs the user).
  - `PENDING` reasons: Dependency, Priority, Resources, GpusBusy, NodeDown, ClusterCap, NoCandidate, ClockUnsynced.
- Terminal: `COMPLETED`, `FAILED(exit)`, `CANCELLED`, `TIMEOUT`, `OUT_OF_MEMORY`, `NODE_FAIL`, `LOST` (revisable if evidence arrives later).
- Each job document carries a monotonic `version`, used by long-polling.

**Rules:**
- **Every dispatch is an attempt row, and its intent is committed before any remote action.** Remote idempotency comes from `mkdir <job_root>/jobs/<job>/<attempt>` (atomic) on nodes, and from the job name on clusters.
- **Retries:** `--retry N` (default 0) covers NODE_FAIL, LOST and Slurm PREEMPTED. A nonzero exit is retried only with `--retry-on=exit`. Gate refusals (exit 75) and precheck failures never consume an attempt.
- **Node unreachable mid-job:**
  - The job stays RUNNING with reason `node_unreachable (since T)`.
  - It becomes NODE_FAIL only with *proof of death*: `boot_id` changed and no rc, or the node is reachable and the unit or pid is gone with no rc.
  - It becomes LOST after 24 h, and is revised if the rc file turns up later.
  - Without proof of death, a requeue needs `fq requeue --force`. A job never runs twice concurrently.
- **Dependencies:** `afterok|afterany|afternotok|after`, resolved on numpi across backends. If an `afterok` parent fails, the child goes to BLOCKED(`DependencyNeverSatisfied`), as in Slurm.

**Scheduling loop:**
- Triggers: a submit, a job ending, a node or feed change, or a 10 s tick.
- Order: `(priority + age boost, submit_ts)`. The age boost is +1 per hour pending, capped.
- Per job:
  1. **Candidates:** the `--on` list, or else all enabled owned nodes, plus clusters if allowed.
  2. **Filter:** per-GPU VRAM/model from probes (most workstations declare no fleetctl capabilities, and those are per-target scalars anyway); fleetctl `--require` semantics for other keys; `--mem`; `--needs` and in-place path present; node up, not drained, not cooling down for this job.
  3. **Concrete GPU set:** chosen by the predicate.
  4. **Score:** exclusive over shared (spares labmates), best-fit VRAM, bundle already cached, fewer recent failures.
- **Greedy backfill:** a lower-priority job may start if it fits. A job pending longer than `starve_after` (6 h) reserves its best node.
- **Concurrency:** 1 dispatch in flight per node, 4 globally.
- **Clock gate:** dispatch waits until `timedatectl` reports NTP synced (the Pi has no RTC), notifying after 10 minutes. All durations use the monotonic clock.

## Data model (SQLite WAL, `synchronous=FULL`, single writer thread)

| Table | Key contents |
|---|---|
| `jobs` | id, name, owner, submitter_token, group_id, spec_json, state, reason, version, priority, bundle_sha, in_place, times |
| `attempts` | job_id, n, node or cluster, backend, remote_id (unit / slurm id), boot_id, state, started, ended, rc, signal, outcome_class |
| `allocations` | attempt_id, node, gpu_uuid, state (reserved / active / cooldown), since |
| `deps`, `groups` | dependency edges; `--each`/array groups |
| `bundles` | sha256, owner, size, refcount, path, last_used |
| `nodes`, `node_gpus` | mode, job_root, exec_mode, state, drain {by, reason, at}, last_probe_json, boot_id; per-GPU uuid, model, vram, drain, cooldown_until |
| `caps` | `cluster:account` → max_gpus, max_queued |
| `idempotency` | (token_id, key) → request_hash, response, expires (7 days) |
| `tokens` | id, owner, kind (human / agent / service), label, secret_sha256, scopes, quotas, expires, revoked |
| `events` | append-only audit and UI feed; 90 days |
| `outbox` | ntfy notifications with bounded retries |
| `remote_calls` | target, hour, count, exit class (the per-login-node ledger) |

Writes are a few rows per state change. Logs never enter the DB; they stay on the nodes, with tails cached on numpi.

## Interfaces

**Client `bin/fq`:**
- A single stdlib-only file, like fleetctl, supporting Python 3.9 and later.
- Config: `~/.config/fleetq/client.toml` (`url` list), `FQ_URL`, `FQ_TOKEN_FILE`. The token file must be mode 0600.
- `--json` on every command returns one object with `"schema": "fq.<kind>/v1"` and `"ok"`.

**Commands:**
- **`fq submit`**, with flags grouped by purpose:
  - Placement: `[--on a,b | --each a,b]`, `[--allow-clusters]`, `[--queue kiac:a100]`, `[--account X]`.
  - Resources: `[--gpus N]`, `[--vram 24G]`, `[--gpu-model M]`, `[--cpus N]`, `[--mem 32G]`, `[--time 4h]`, `[--require EXPR]`.
  - Code and data: `[--dir PATH | --in-place PATH]`, `[--needs PATH]…`, `[--collect GLOB]`.
  - Environment: `[--setup CMD]`, `[--env K=V]`, `[--env-file F]`.
  - Control: `[--after ID[:afterok]]`, `[--priority N]`, `[--name S]`, `[--hold]`, `[--retry N]`, `[--notify end,fail]`.
  - Agents: `[--idempotency-key K]`, `[--wait [--timeout S]]`, `[--dry-run]`.
  - The job itself: `-- argv…`, or `SCRIPT`, or `--wrap 'cmd'`.
- **Jobs:** `fq queue` · `fq show` · `fq explain ID` (per-node rejection reasons) · `fq wait ID… [--any|--all] [--timeout S] [--propagate-exit]` · `fq logs ID [--err] [--tail N] [--follow]` · `fq cancel|hold|release|requeue|top` · `fq priority ID N` · `fq modify ID …` (pending jobs only) · `fq fetch ID [paths] -o DIR` · `fq history [--summary]`
- **Nodes and admin:** `fq nodes [show|drain|resume]` · `fq node install|probe NAME` · `fq cluster freeze|thaw` · `fq admin pause|resume|accept on|off` · `fq doctor` · `fq whoami`

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | ok (and, for wait: completed with exit 0) |
| 1 | the job ended unsuccessfully |
| 2 | refused (usage, forbidden, quota, cluster not allowed, unsatisfiable, preflight) |
| 3 | not found |
| 4 | wait timed out while the job is still active — call again |
| 5 | fleetqd unreachable (safe to retry with the same idempotency key) |
| 6 | auth |
| 7 | version mismatch |
| 8 | rate limited (`retry_after`) |
| 130 | interrupted (the job keeps running) |

**Bundles:**
- **Built by the client:** a deterministic tar (sorted, uid/gid 0, gzip level 1, sha256 of the uncompressed stream).
- **Excluded:** fleetctl's `DEFAULT_SYNC_EXCLUDES` (which includes `.env`), plus `.fqignore` and `--exclude`. A test asserts fq's copy of the list equals fleetctl's.
- **Symlinks** that point outside the tree are skipped, which blocks exfiltration of `~/.ssh`.
- **Upload:** `HEAD` first, then `PUT`, streamed to disk with a hash check. Unchanged trees and `--each` fan-outs upload once.
- **Caps:** 256 MiB compressed, 100 MiB per file, 50k files. Above that, the client exits 2, lists the largest paths and suggests `--in-place` or `--needs`.
- **On nodes:** tarballs are cached by sha and extracted *per attempt*, so a job can't corrupt the cache.
- **Provenance** (git commit, dirty flag) is recorded with each job.

**API:** `/api/v1` on port 8089, bound to numpi's tailnet IP (optionally behind `tailscale serve` for TLS).
- **Auth, checked by fleetqd itself.** fleetmon decides auth from its bind address, so on the tailnet every caller is unauthenticated to it; fq can't rely on that.
  - Summary reads with no token are allowed on the tailnet.
  - Job detail, logs, bundles and **every mutation** need a bearer token, even on the tailnet. A token means code execution on all your machines.
- **Tokens:**
  - Format `fq_<id>_<secret>`; the DB stores the hash only.
  - Issued only on the Pi, with `fleetqd token create --owner … --kind agent --label laptop/claude [--clusters]`.
  - Scopes: `read`, `logs`, `submit`, `manage_own`, `manage_all`, `nodes`, `admin`.
  - An agent token may manage only jobs it submitted.
- **Quotas:** agents default to 20 active jobs, 4 GPUs, 10 submissions per minute, and **no clusters unless the token was created with `--clusters`**. Quotas are also summed per owner.
- **Endpoints:**
  - `POST /jobs` with an `Idempotency-Key` header. The same key and body replays the original response; the same key with a different body gets 409.
  - `GET /jobs[/{id}]`
  - `GET /jobs/{id}/wait?until=terminal|started|change&since_version=N&timeout=55`, a long-poll woken by in-process futures (128 waiters in total, 16 per token)
  - `POST /wait` for several ids
  - `GET /jobs/{id}/logs?offset=&max_bytes=&wait=` (offset-based)
  - `GET /jobs/{id}/explain`
  - `POST /jobs/{id}/{cancel,hold,release,requeue,top}`, `PATCH /jobs/{id}`
  - `HEAD|PUT /bundles/sha256:…`
  - `GET /nodes`, `POST /nodes/{n}/{drain,resume}`
  - `GET /status`, which includes the remote-call ledger
  - `GET /limits`, `GET /whoami`
- **Refused at submit if it could never run:** a job no candidate could ever satisfy gets **422 `unsatisfiable`**, instead of pending forever.
- **Browser actions** are only on fleetq-served `/ui/jobs/<id>` pages: Basic auth, a synchronizer CSRF token, an Origin check, `form-action 'self'`.

**Agents:**
- The single call is `fq submit --wait --json --timeout 540 --idempotency-key "<task>-<cfghash>" … -- cmd`. The timeout is set below the agent's 10-minute tool limit; exit 4 means "call `fq wait ID --timeout 540` again". Each wait is a cheap long-poll with zero cluster traffic.
- The JSON exposes `job.terminal`, `job.success`, `job.exit.code` and `job.placement.{node,gpus,slurm_job_id}`. Schemas live in `fleetq/contracts/`.
- The skill update in `shared/fleet-dotfiles/codex/skills/remote-fleet-operator/SKILL.md` adds a "Running work: submit to the fleet queue, then wait" section:
  - compute goes through `fq`;
  - never poll `squeue`/`sacct` via `fleetctl exec` in a loop;
  - always pass an idempotency key;
  - read JSON only;
  - a refusal is the answer;
  - use your own `FQ_TOKEN_FILE`.

  Without `fq`, the fallback is `fleetctl submit` plus at most one status check per 5 minutes.
- No MCP server in v1. The CLI covers Claude and Codex through the shared skill; revisit in P6 if transcripts show misuse.

## fleetmon integration
- **New read-only `GET /api/feed/v1/capacity`** in `fleetmon/src/fleetmon/web/app.py`, backed by a `HubRuntime` provider in `service.py`.
  - It is versioned in the path and additive within v1.
  - Its JSON Schema lives in fleetmon, and fleetq keeps a vendored copy (a test asserts they are equal).
  - It has a `complete` flag for truncated squeue data, and an explicit mapping from target to Slurm cluster name (`slurm_jobs` is keyed by cluster).
  - fleetqd polls it every 10 s. If it is stale for more than 30 s, errors, or has the wrong version, shared GPUs become *unknown*, so no dispatch goes to shared nodes; exclusive nodes still dispatch, gated on the node.
- **Optional fleetmon fix:** add a `vram_used` threshold to `classify_gpu`, so its own `gpu_free` notifications stop firing for GPUs that hold memory.
- **Queue page:** fleetmon's server calls fleetqd's read API with a `service` token (2 s timeout, 2 s cache). If fleetq is down, the page says "scheduler unavailable".
  - Tabs: Pending, Running, Recent, and a Nodes view per GPU showing the fq job vs labmate processes.
  - The job page has an explain panel and a log tail.
  - The Hub Status page gets a card with each login node's call counter.
  - The Slurm Jobs page links rows named `fq-*`.
  - Cancel and Hold are plain links to fleetq's own pages.
- **Config:** a new `[scheduler] url` key in fleetmon config, with the token from `FLEETMON_SCHEDULER_TOKEN`.
- **Notifications:** fleetqd posts to the same ntfy on its own topic, through a persistent outbox (3 attempts over 15 min, 30/h cap with a digest).
  - Events: job completed / failed / timeout, BLOCKED (sent immediately for never-run reasons), pending for 2 h, `node_lost`, `foreign_process_on_allocated_gpu`, `cluster_auth_failed`, `fleetq_degraded`.

## fleetctl changes — P0 (all general-purpose, useful standalone)
In `shared/fleet-dotfiles/bin/fleetctl`:
1. **`--json` results for `submit`, `exec`, `sync`, `job status` and `job cancel`.**
   - `exec --json` is an opt-in envelope with bounded capture.
   - Errors come back as `{"error":{"class":"refused|transport|timeout|remote|budget",…}}`.
   - This removes the exit-2/255 ambiguity without changing exit codes.
2. **`--timeout S` on `exec`, `script`, `sync` and `submit`,** killing the process group; exit 124.
3. **`submit --account`,** checked by the existing preflight rules. This unblocks KIAC h200 with `chiru`.
4. **Inline small payloads (< 64 KiB) into the Slurm wrapper as a heredoc.** Saves 4 SSH round-trips, and a long-pending job can no longer lose its payload to the 14-day prune.
5. **Fix `cd {shlex.quote(cwd)}` in `render_slurm_wrapper`** (it breaks `~/…`) to use `quote_remote_path`.
6. **Per-target control budget** (a token bucket declared on the protocol):
   - `[control_budget] monitor_per_minute`, `action_per_minute`, `burst`.
   - State in `$FLEET_STATE_HOME/budget/<target>.json`, under flock.
   - `exec --admin` counts as `monitor`; `submit` and `cancel` count as `action`.
   - An empty bucket gives exit 75, or `--budget-wait S`.
   - **This throttles the laptop agents' polling immediately, before fq exists.** fleetmon and fq share it on numpi.
7. **`[defaults] local_target = "numpi"`:** on that host, `via:<self>` resolves as `direct`, and `doctor` warns if it is unset while a reach list names the host.
8. **(Optional) `FLEETCTL_CALLER` in `audit.jsonl`,** so fq, fleetmon and agent calls are distinguishable — it would have identified today's KIAC polling instantly. Plus audit trimming every 200 appends, not every append, to cut SD churn.

**Kept out of fleetctl:** detached workstation execution lives in `fq-node`, and fleetctl's role matrix is unchanged (workstation `job` stays DENY).

## Robustness matrix (detect → state → action)

| Failure | Detection | State | Automatic action |
|---|---|---|---|
| Node down before dispatch | probe / feed `unreachable` | node DOWN | skip; retry the node at 2/5/10 min |
| Node down mid-job | 2 failed status calls | RUNNING (`node_unreachable since T`) | keep; NODE_FAIL only on proof of death; LOST after 24 h |
| Node rebooted mid-job | `boot_id` ≠ launch boot_id, no rc | NODE_FAIL | requeue within `--retry`; clear GPU markers |
| fleetqd crash / numpi reboot | startup | — | reconcile every non-terminal attempt (node attempt dir / cluster name lookup) before accepting dispatch |
| Crash after intent, before launch | attempt `launching`, no remote dir | → PENDING | re-dispatch the same attempt (the `mkdir` guard) |
| Crash after launch, before commit | remote dir or unit exists | → RUNNING | adopt the running unit |
| Crash after sbatch, before commit | name lookup finds the job | → SUBMITTED | adopt, never resubmit |
| Labmate grabs the GPU before launch | gate exit 75 | PENDING (GpusBusy) | re-place; 5 min GPU cooldown; no attempt consumed |
| Foreign process on our GPU | status compute-apps | RUNNING + event | notify only |
| `job_root` / data unmounted, read-only, hung NFS | bounded probes | precheck fail / `storage_degraded` | cooldown, try elsewhere, notify |
| `nvidia-smi` hangs or errors | bounded probe | GPU or node drained | notify; auto-clear when healthy |
| Disk full (node) | free-space precheck | PENDING (Resources) | another node |
| Disk full (numpi) | reserve check (2 GB) | submits get 507 | notify; GC bundles |
| State SSD not mounted | `RequiresMountsFor` + `.fleetq-volume` UUID sentinel | fleetqd refuses to start | never creates an empty DB on the SD card |
| Clock not synced at boot | `timedatectl` | PENDING (ClockUnsynced) | wait; notify after 10 min |
| fleetmon down or stale | feed error, or age > 30 s | — | shared nodes unplaceable; exclusive gated on the node |
| sbatch timeout / 255 | fleetctl `--json` error class | SUBMITTED? | name lookup → adopt or resubmit, never both |
| Cluster job pending forever | squeue reason | BLOCKED | notify; re-place if alternatives were given |
| Login node down / auth fail | error class | stale | back off 5→30 min; BLOCKED(`auth`) after 2 h |
| Budget exhausted | fleetctl exit 75 | — | defer the poll; never loop |
| Walltime / OOM | RuntimeMaxSec / cgroup OOM / sacct | TIMEOUT / OUT_OF_MEMORY | retry only with `--retry-on=exit` |
| Duplicate submit by a retrying agent | (token, idempotency key) | — | return the original job |
| Cancel while dispatching or unreachable | intent flag | CANCELLING | apply when possible; confirm first |
| Dependency failed | parent terminal ≠ COMPLETED | BLOCKED | notify |
| Second scheduler instance | foreign instance id on markers | — | GPU treated as busy; `doctor` warns |

## Blockers to verify before building (the user runs these on numpi — no dev-session host contact)
1. **Can numpi reach every node?** The laptop reaches the KIAC, AMD and wellsfargo login nodes and the cloud lovelace boxes "direct"; if that relies on a campus VPN or the laptop's network, numpi may not have it. **This is the biggest structural risk.** Check with `fleetctl doctor --probe <target>` on numpi for each target. A cluster numpi can't reach would need an alternative, such as a relay through a workstation that can reach it.
2. **numpi's inventory and secrets.** Does numpi's `~/.config/fleet` contain kiac-ayand and wellsfargo (fleetmon's notes mention only amd-login being polled)? Check that numpi has `sshpass` and the KIAC password, and the SSH keys for all nodes. Python ≥ 3.11 is required for fleetctl.
3. **Storage.** Is `~/.local/state` on the SSD or the SD card? That decides the `RequiresMountsFor` path and the backup target. Also the Pi model and RAM (for `MemoryMax`), and whether port 8089 is free.
4. **Linger** on each workstation to be enabled (`loginctl enable-linger` without sudo?). Lab machines may need their admin.
5. **An inventory inconsistency:** wellsfargo is `role=login`, yet fleetmon's notes list a helper installed on it.
6. **Uncommitted trees:** land fleetmon 0.3.1 and fleet-dotfiles' pending `CHANGELOG`/`setup.sh` changes before P0/P1, so changes don't get mixed.

## Package layout & deployment
- **`fleetq/` layout:**
  - `src/fleetq/`:
    - `cli.py` — `fleetqd serve|doctor|token|node|smoke|maintain`
    - `config.py` — strict TOML; unknown keys rejected
    - `db/{schema,migrations,store}.py`
    - `api/{app,auth,csrf,longpoll,uploads,ratelimit}.py`
    - `ui/` — action pages
    - `engine/{scheduler,placement,state,reconcile}.py`
    - `executors/{bare,slurm}.py`
    - `fleetctl.py` — the *only* spawner of fleetctl, with allowlisted argv builders; the bounded runner is copied from `fleetmon/src/fleetmon/poller.py:run_command` + `PollController`
    - `feeds/fleetmon.py`, `bundles.py`, `logs.py`, `notify.py`, `quotas.py`
    - `shim/fq-node` — POSIX sh + python3 stdlib
  - Also: `bin/fq`, `contracts/`, `packaging/fleetq.service`, `scripts/{install-daemon,install-client,push-inventory}`, and `tests/`.
- **Unit `fleetq.service`,** modelled on `fleetmon/packaging/fleetmon-hub.service`:
  - `RequiresMountsFor=%h/.local/state/fleetq`, `ProtectSystem=strict`
  - `ReadWritePaths` for the fleetq state and fleet state/cache
  - `KillMode=mixed` (jobs are detached on the nodes, so they survive)
  - `Restart=on-failure`, `MemoryMax=384M`, `Nice=5`
  - Add the same `RequiresMountsFor` to fleetmon's unit.
- **Install and upgrade** follow fleetmon's `install-hub` pattern: releases, a `current` symlink, a locked venv.
  - Upgrade sequence: `fq admin pause` → wait until in-flight remote ops reach 0 → online DB backup → switch → restart (**which runs full reconciliation**) → `/readyz` → resume.
- **Operations:**
  - Daily online backup to a different filesystem, keeping 14.
  - Retention: terminal jobs 180 days, events 90 days, archived log tails 16 MiB per job and 5 GiB total, bundles 7 days after their last reference.
- **Kill switches:**
  - per node / cluster / account
  - `accept off` (stop taking submissions)
  - `pause` (stop dispatching)
  - the file `~/.config/fleetq/DISPATCH_DISABLED`, checked right before *every* fleetctl launch
  - `systemctl --user stop fleetq` (running jobs continue)
- **numpi's inventory:** `scripts/push-inventory` on the laptop, which is the source of truth via pass. It renders with `deploy-config` into staging, overlays `local_target = "numpi"`, runs an offline `doctor`, then does `fleetctl sync push … --admin` and an atomic activate. A `.rendered-from` digest lets `fleetqd doctor` flag drift.
- **Node onboarding:** `fleetqd node install <name> --i-authorize-target-<name>`, one node at a time, mirroring fleetmon's `install-helper`.
  1. Hash-verified `fleetctl sync push` of the shim into `job_root`, with an atomic `current` switch.
  2. `fq-node self-check`.
  3. A probe. It reports linger / `KillUserProcesses`, cgroup delegation, `nvidia-smi`/driver/compute_mode, python3, `bash -l` health, job_root/HOME health, and `XDG_RUNTIME_DIR`. From these it chooses the exec mode.

## Phases (each independently useful)

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0** | fleetctl changes 1–7 (+8 optional) with tests; interim skill rule: at most one status check per 5 min, and what exit 75 means; the blocker checklist above, run by the user | fleetctl suite green; a concurrency test proves the bucket holds; the laptop audit log shows agent polling throttled; blockers 1–3 answered |
| **P1 — MVP** | fleetqd, DB, API, tokens, `fq` CLI, bundles, `fq-node`, **exclusive** bare-metal executor, reconciler, wait / logs / cancel / hold / priority / explain, unit / install / doctor / backups / kill switches, fakes plus crash and property suites | All suites green; crash matrix shows exactly-once launch; then (with your go-ahead) one real exclusive workstation: submit from 2 machines, reboot mid-job → NODE_FAIL, pull the network → stale, not failed; fleetqd under 150 MiB RSS and under 3% CPU idle on the Pi |
| **P2** | **Shared-node GPU placement:** the predicate, fleetmon's capacity feed, the gate with exit 75 and cooldown, foreign-process and escape detection, cgroup limits with RSS fallback, bounded mount / NFS / `nvidia-smi` probes, drains, per-node env handling (login shell, HOME override) | A fake `nvidia-smi` scenario per row of the idle table (Xorg, hidden process, zombie memory, labmate race, MIG, fallen off the bus, hang); you approve the shared/exclusive map and `reserve_gpus` |
| **P3** | Slurm executor: `--account`, inline payload, name dedupe, rc files, batched budgeted polling, BLOCKED reasons, caps (including labmates' usage), wellsfargo native path, `--collect`, cluster log tail via allowlisted `dd` | Fake-Slurm suite (accounting on and off, 22.05 formats, lost sbatch response, AssocGrpGRES, disallowed-account reason text); **the first real cluster job is started by you** (AMD, then KIAC `chiru` + `h200_qos`) and recorded as verified-by-run |
| **P4** | fleetmon Queue / job / nodes pages, fleetq action pages with CSRF, Hub Status card, ntfy outbox | JS harness and CSRF tests; a week of notifications with no floods |
| **P5** | Dependencies, `--each` groups, arrays (`%throttle`), retry classes, aging / starvation reservation, `modify`, `--mem-hard` | Property invariants extended; a week of real use |
| **P6** | Skill section (full), token scopes and quotas UI, MCP only if agent transcripts justify it | A month with zero cluster-babysitting calls in the audit logs |

**Your inputs:**
- P0: budget numbers per login node, and approval of the fleetctl changes.
- P1: the canary node, bind mode (tailnet vs `tailscale serve`), and bundle caps.
- P2: the shared/exclusive map, `reserve_gpus`, and `idle_mem_mib`.
- P3: per-cluster caps and defaults, whether the chiru 2-GPU limit is a lab-wide total, and whether any agent token gets clusters.

## Verification (never touches KIAC/AMD/wellsfargo during development)
- **Enforced in code:**
  - `fleetq/tests/conftest.py` puts `tests/fakes/bin` first on PATH, shadowing `fleetctl`, `ssh`, `sshpass` and `rsync`, and points all `FLEET_*`/`FLEETQ_*` dirs at a temp path.
  - An autouse fixture fails any test whose subprocess resolves outside the fakes.
  - In test mode, fleetqd refuses a fleetctl path outside the temp root.
  - The live smoke refuses login and scheduler targets.
- **Fakes:**
  - `fake-fleetctl`: per-target sandbox homes that run real processes, with a `calls.jsonl` log.
  - Fault injection via `faults.json`: unreachable, timeout, exit 255, garbage or truncated stdout, **sbatch accepted then connection dropped**.
  - Fake node: a scripted `nvidia-smi`, fake `systemd-run`/`loginctl`, and an overridable `boot_id`.
  - `fakeslurm`: `sbatch`/`squeue`/`sacct`/`scancel` over a flocked JSON state, running real job subprocesses. It enforces the account/QOS/GrpTRES fixture, reproduces the real quirks (a disallowed account pends forever with KIAC's reason text, MinJobAge drop-off, sacct lag, naive-local sacct times, `--test-only` passing), and has 22.05 golden outputs.
  - Fake fleetmon feed: idle / busy / stale / wrong-version / truncated scenarios.
- **Suites:**
  - unit / api (auth tiers, CSRF, idempotency, upload caps)
  - client (golden `--json` against `contracts/`, exit codes, bundle determinism and excludes, symlink refusal)
  - property: Hypothesis state machine. Invariants:
    - no GPU UUID allocated twice;
    - at most one live attempt per job;
    - caps never exceeded;
    - no cluster without opt-in;
    - `--each` = N jobs;
    - idempotent replays;
    - no dispatch while held or blocked on dependencies.
  - crash: `FQ_CRASHPOINT` at every commit point of both dispatch protocols, then restart → no duplicate launch (from `calls.jsonl`) and orphans adopted.
  - integration: submit → wait end to end, and 50 concurrent waiters produce ≤ budget calls at the fake login node.
- **Commands:** `cd fleetq && python3 -m pytest -q` (`-m property`, `-m crash`); fleetctl: `cd shared/fleet-dotfiles && python3 -m pytest tests -q`; fleetmon: its existing runner.
- **Real hardware:** only one owned workstation, only with your explicit go-ahead, only after every fake suite passes. Cluster use is a production action you start yourself, never a test.

## Risks (accepted or mitigated)
- **Agents bypassing fq via `fleetctl submit`:** the skill forbids it and the fleetctl budget caps the damage; this can't be prevented on the same Unix account. Likewise, an agent can read the human token file; tokens give attribution, not a security boundary.
- **numpi is a single point of failure for the queue:** jobs are detached on the nodes, and the DB is backed up. Accepted.
- **Shared nodes without cgroup delegation:** memory caps are not enforced there, only flagged.
- **Shared-node placement stops when fleetmon's feed is down:** this fails closed on purpose, and `fq explain` says why.

## Appendix A — why not an existing system
- **HTCondor + Bosco:** needs startd on every workstation and a BLAHP process on the login node while jobs run.
- **HyperQueue, Dask, Parsl:** compute nodes must connect back.
- **SkyPilot:** installs k3s and needs sudo; keeps a long-running job on clusters.
- **Nomad, Flux:** need an agent on every node.
- **jobflow-remote:** the right architecture, but needs MongoDB, which has no Pi 4 (ARMv8.0) builds.
- **pueue:** one daemon per machine, not distributed.
- **What we borrow:** HTCondor's queue verbs, jobflow-remote's `REMOTE_ERROR`-style states, submitit's sentinel files.
