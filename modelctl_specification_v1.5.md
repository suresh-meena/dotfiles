# modelctl — Industry-Grade Model Control, Remote Serving, and Delegation Skill/CLI Specification

**Status:** Proposed v1.5 design  
**Date:** 2026-08-19  
**Execution backends:** vLLM on remote Linux GPU machines; OpenCode Go through `opencode run`  
**Control paths:** local `modelctl` → SSH → vLLM supervisor; Codex → `modelctl delegate` → OpenCode Go  
**Default data policy:** local vLLM for private inference; cloud delegation only after routing/privacy gates

`modelctl` should behave as a deterministic model control plane with two execution paths. For local/private inference it manages machine-specific vLLM deployments on remote GPU machines. For delegated coding work it brokers bounded jobs to configured OpenCode Go models through `opencode run`, classifies those models into `frontier`, `driver`, and `worker` bins, enforces privacy/cost/tool policies, validates returned work, and records provenance. The local vLLM path remains the preferred path for data that may not leave the user's machines.

\[
\boxed{
\texttt{target}
=
(\texttt{model},\ \texttt{machine},\ \texttt{artifact},\ \texttt{runtime profile})
}
\]

The central engineering principle is **fail closed with explicit postconditions**. `modelctl` cannot guarantee that underlying drivers, SSH, vLLM, or hardware never fail; it can guarantee that ambiguous state does not silently become a successful operation, that failures are classified, that owned resources are reconciled, and that a command reports success only after its required postconditions are verified.

---

## 1. Intent and Product Contract

Scope: this section converts the requested behavior into explicit system responsibilities.

The desired operator experience is:

```bash
modelctl start qwen-72b --machine gpu-a
modelctl connect qwen-72b --machine gpu-a
```

The operator should not need to remember:

- SSH hostnames or jump hosts.
- Remote model paths.
- CUDA device selections.
- Tensor/pipeline parallel sizes.
- vLLM ports.
- Python environments or containers.
- Per-machine vLLM flags.
- Log locations.
- Process IDs.
- Which machine currently has which model artifact.
- Whether a prior failed launch left a process or GPU allocation behind.

`modelctl` is responsible for five distinct layers:

1. **Inventory:** what machines exist, what local model artifacts are observed on each machine, and what delegated/cloud models are currently available.
2. **Resolution:** the exact target-specific vLLM configuration for `(model, machine)` or the exact delegated `provider/model` selected from a requested bin.
3. **Lifecycle:** local start/readiness/connect/status/stop/recovery/garbage collection plus delegated run/cancel/history semantics.
4. **Routing:** classify delegated work into `frontier`, `driver`, or `worker`; enforce eligibility; and select an explicit model deterministically.
5. **Safety:** ownership proof, GPU reservation, cloud data-classification gates, least-privilege delegate tools, isolated workspaces, no accidental remote exposure, no request-content logging by default, deterministic cleanup, and explicit handling of suspicious residual resources.

### 1.1 Success criterion

For a valid configured target, this command:

```bash
modelctl start qwen-72b --machine gpu-a
```

must either:

- return a structured success result for a verified healthy deployment, or
- return a stable error code describing exactly which invariant failed.

It must never print success merely because a process was spawned.

### 1.2 Reliability objective

“Fool proof” is interpreted as the following engineering properties:

- Safe defaults.
- Strict schema validation.
- No implicit target guessing for destructive actions.
- Idempotent operations.
- Bounded timeouts.
- Per-target and per-GPU locks.
- Ownership-based cleanup only.
- Reconciliation after client/SSH crashes.
- Verified start and stop postconditions.
- Machine-readable errors.
- No arbitrary shell construction from agent text.
- No silent fallback to a different model or machine.
- No automatic killing of unknown GPU processes.
- No persistent model in VRAM after a successful `stop`.
- No network exposure beyond loopback unless explicitly configured.

---

## 2. Non-Negotiable Safety Invariants

Scope: these invariants define conditions that every implementation path must preserve.

### 2.1 Target identity invariant

A deployment is uniquely identified by:

```text
target_id      = model_alias + "@" + machine_alias
config_digest  = SHA256(canonical_resolved_target_config)
deployment_id  = target_id + ":" + config_digest + ":" + launch_nonce
```

The same target with the same digest is idempotent. A target with a different digest is a different deployment intent and must not silently replace the old one.

### 2.2 Artifact identity invariant

A logical model alias is not enough to prove availability. Each target resolves to an observed model artifact:

```text
artifact = {
  machine,
  canonical_path,
  model_alias,
  size_bytes,
  manifest_fingerprint,
  observed_at,
  observation_status
}
```

A start must fail if the selected artifact is missing, stale beyond policy, ambiguous, or changed unexpectedly after the target was resolved.

### 2.3 Ownership invariant

`modelctl` may terminate only processes it can prove belong to the deployment.

Proof should combine:

- supervisor unit identity;
- deployment metadata stored under a private remote state directory;
- process cgroup membership;
- launch token / deployment ID;
- PID start time or equivalent anti-PID-reuse metadata.

A PID number alone is insufficient ownership proof.

### 2.4 GPU reservation invariant

Before launch, all configured GPUs must be atomically reserved by `modelctl`.

A GPU cannot be assigned to a second managed target unless the target explicitly declares a sharing policy.

If an unmanaged process is already consuming a reserved GPU and the policy requires exclusivity, start fails with `E_GPU_BUSY_FOREIGN`. `modelctl` does not kill the foreign process.

### 2.5 Stop postcondition invariant

`modelctl stop` returns success only when all of the following are true:

1. The owned supervisor unit is inactive.
2. The owned cgroup contains no remaining process.
3. No process belonging to the deployment appears in the GPU compute-process inventory.
4. The deployment's GPU reservation is released.
5. The deployment metadata is finalized as stopped.

If the process is gone but GPU ownership cannot be verified conclusively, the state is not reported as cleanly stopped; it becomes `LEAK_SUSPECTED` or `RECONCILE_REQUIRED`.

### 2.6 Network isolation invariant

Remote vLLM binds to `127.0.0.1` by default.

A local user reaches it through an SSH tunnel. Non-loopback binding requires an explicit target-level setting such as:

```yaml
security:
  allow_remote_exposure: true
```

and should fail configuration linting unless an accompanying network policy is declared.

vLLM itself documents that its API-key protection does not cover every endpoint; `modelctl` therefore must not treat `--api-key` as a complete perimeter control. [vLLM Security](https://docs.vllm.ai/en/stable/usage/security/)

### 2.7 Prompt/output privacy invariant

Request and response bodies are not logged by `modelctl`.

Current vLLM exposes explicit request/output logging controls and request logging is disabled by default; `modelctl` should still set the safe values explicitly rather than relying on version defaults. [vLLM `serve` CLI](https://docs.vllm.ai/en/stable/cli/serve/)

### 2.8 No silent mutation invariant

The skill layer may not synthesize arbitrary vLLM flags from natural language and append them to a shell command.

Only schema-approved overrides are allowed.

---

## 3. Conceptual Architecture

Scope: this section defines the control plane, state stores, and remote execution boundary.

```text
┌──────────────────────────── Local workstation ─────────────────────────────┐
│                                                                            │
│  Human / agent                                                             │
│       │                                                                    │
│       ▼                                                                    │
│  modelctl CLI                                                              │
│       │                                                                    │
│       ├── Config Resolver ───────────────┐                                  │
│       ├── Inventory Registry (SQLite)    │                                  │
│       ├── State / Event Journal          │                                  │
│       ├── SSH Transport                  │                                  │
│       └── Tunnel Manager                 │                                  │
│                  │                       │                                  │
└──────────────────┼───────────────────────┼──────────────────────────────────┘
                   │ SSH                   │ localhost endpoint
                   ▼                       ▲
┌────────────────────── Remote machine: gpu-a ────────────────────────────────┐
│                                                                             │
│  probe helper / POSIX commands                                              │
│       │                                                                     │
│       ├── model artifact roots                                              │
│       ├── GPU/NVML inspection                                               │
│       └── runtime validation                                                 │
│                                                                             │
│  ~/.local/state/modelctl/                                                   │
│       ├── locks/                                                             │
│       ├── deployments/                                                       │
│       └── generated/                                                         │
│                                                                             │
│  systemd --user                                                             │
│       └── modelctl-<deployment>.service                                     │
│              └── vllm serve ...                                             │
│                    └── 127.0.0.1:<remote-port>                              │
│                                                                             │
│  NVIDIA GPU(s)                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 No permanent central daemon in v1

The recommended v1 architecture does not require a central always-on controller.

Authoritative configuration is local. Runtime truth is reconciled from:

- local inventory/event state;
- the remote supervisor;
- remote deployment metadata;
- GPU process state;
- vLLM health/model endpoints.

A later fleet daemon can reuse the same contracts.

---

## 4. Domain Model

Scope: this section defines the core entities so configuration and observed state are not conflated.

### 4.1 Machine

A stable alias for one remote host.

```yaml
machines:
  gpu-a:
    ssh:
      host: gpu-a.example.internal
      user: ink
    supervisor: systemd-user
    inventory:
      roots:
        - /models
        - /mnt/models
    runtime:
      type: venv
      activate: /opt/vllm/bin/activate
```

### 4.2 Logical model

A stable human-facing alias independent of where the artifact lives.

```yaml
models:
  qwen-72b:
    served_model_name: qwen-72b
    family: qwen
```

### 4.3 Model artifact

An observed or declared physical copy of model weights on one machine.

Example:

```text
artifact_id:        gpu-a:/models/Qwen2.5-72B-Instruct
model_alias:        qwen-72b
machine:            gpu-a
path:               /models/Qwen2.5-72B-Instruct
format:             safetensors
size_bytes:         ...
manifest_hash:      ...
observed_at:        ...
status:             AVAILABLE
```

### 4.4 Target

The deployable unit.

```text
target = logical model + machine + selected artifact + runtime profile
```

A model may have multiple targets:

```text
qwen-72b@gpu-a
qwen-72b@gpu-b
```

with different quantization, GPU count, model path, context length, and vLLM version.

### 4.5 Deployment

One concrete runtime instance of one resolved target.

A deployment records:

- deployment ID;
- target ID;
- config digest;
- artifact fingerprint;
- vLLM version;
- allocated GPU UUIDs;
- remote port;
- supervisor unit;
- start timestamp;
- readiness timestamp;
- current state;
- stop timestamp;
- exit status;
- cleanup result.

### 4.6 Lease

A lease is an optional lifetime policy used to prevent forgotten models from occupying VRAM indefinitely.

Recommended modes:

- `persistent`: remains up until explicit stop.
- `ttl`: stop after fixed time unless renewed.
- `session`: tied to a local managed connection/session; after the lease grace period expires, remote cleanup begins.

`persistent` should be explicit rather than the only implicit behavior on shared or expensive machines.

---

## 5. Machine/Model Inventory and Availability Ledger

Scope: this section defines how `modelctl` keeps track of which machine has which model.

The inventory must distinguish **declared state** from **observed state**.

### 5.1 Declared state

Configuration says where a model is expected:

```yaml
targets:
  qwen-72b@gpu-a:
    model: qwen-72b
    machine: gpu-a
    artifact:
      path: /models/Qwen2.5-72B-Instruct
```

This is intent, not proof.

### 5.2 Observed state

A probe confirms the artifact exists and records a fingerprint.

Recommended local database:

```text
~/.local/state/modelctl/modelctl.db
```

SQLite is appropriate for v1 because the controller is local, writes are transactional, inventory queries are relational, and schema migrations can be versioned.

### 5.3 Core inventory tables

```sql
machines(
  machine_id TEXT PRIMARY KEY,
  last_seen_at TEXT,
  last_probe_status TEXT,
  ssh_fingerprint TEXT,
  gpu_summary_json TEXT
);

artifacts(
  artifact_id TEXT PRIMARY KEY,
  machine_id TEXT NOT NULL,
  model_alias TEXT,
  canonical_path TEXT NOT NULL,
  format TEXT,
  size_bytes INTEGER,
  manifest_fingerprint TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  current_status TEXT
);

artifact_observations(
  observation_id INTEGER PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  exists_flag INTEGER NOT NULL,
  size_bytes INTEGER,
  manifest_fingerprint TEXT,
  probe_version TEXT,
  error_code TEXT
);

deployments(
  deployment_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  artifact_fingerprint TEXT,
  state TEXT NOT NULL,
  supervisor_unit TEXT,
  started_at TEXT,
  ready_at TEXT,
  stopped_at TEXT
);

events(
  event_id INTEGER PRIMARY KEY,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  target_id TEXT,
  deployment_id TEXT,
  machine_id TEXT,
  result TEXT,
  details_json TEXT
);
```

### 5.4 Inventory scanning policy

`modelctl` must not recursively inspect an entire remote filesystem.

Each machine declares allowed model roots:

```yaml
inventory:
  roots:
    - /models
    - /mnt/models
```

Scanning is restricted to those roots.

A probe detects recognizable model directories, for example by the presence of files such as:

- `config.json`;
- safetensors files or a safetensors index;
- tokenizer/configuration metadata;
- known quantization metadata.

Full model files are not transferred to the controller.

### 5.5 Artifact fingerprint

Hashing every weight byte on every probe is unnecessarily expensive.

Recommended fingerprint:

```text
SHA256(
  canonical_path
  + sorted(relative_filename, size, mtime_ns)
  + hash(config.json)
  + hash(weight-index file if present)
  + optional explicit model identity file
)
```

A stricter `--deep-hash` mode may hash all weight files when provenance verification is required.

### 5.6 Inventory freshness

Every observation includes a timestamp.

Policies:

```yaml
inventory:
  max_age_before_start_s: 3600
  auto_refresh_before_start: true
```

If the observation is stale, `start` refreshes it. If refresh fails, start fails unless a narrowly scoped operator override is used.

### 5.7 Inventory CLI

```bash
modelctl inventory sync
modelctl inventory sync --machine gpu-a
modelctl inventory list
modelctl inventory list --machine gpu-a
modelctl inventory list --model qwen-72b
modelctl inventory show qwen-72b --machine gpu-a
modelctl inventory history qwen-72b --machine gpu-a
modelctl inventory diff --machine gpu-a
modelctl inventory stale
```

Example:

```text
$ modelctl inventory list

MODEL       MACHINE   STATUS       PATH                                  LAST VERIFIED
qwen-72b    gpu-a     available    /models/Qwen2.5-72B-Instruct         2m ago
qwen-72b    gpu-b     missing      /srv/models/Qwen2.5-72B-Instruct     1d ago
coder-32b   gpu-b     available    /srv/models/coder-32b-awq            4m ago
```

### 5.8 Inventory history semantics

The system should preserve history rather than overwriting the last state.

This allows queries such as:

```bash
modelctl inventory history qwen-72b --machine gpu-a
```

to answer:

- when the artifact first appeared;
- when it disappeared;
- when its fingerprint changed;
- whether a launch used an older fingerprint.

### 5.9 Artifact change during deployment

If an artifact fingerprint changes while a deployment is running:

- record `ARTIFACT_CHANGED`;
- do not silently restart;
- mark deployment `DRIFTED`;
- require an explicit restart/replacement if the operator wants the new artifact.

---

## 6. Configuration Specification

Scope: this section defines the declarative source of truth for machines, models, targets, lifecycle policies, and security.

Recommended locations:

```text
~/.config/modelctl/config.yaml
./modelctl.yaml                       # optional project override
~/.local/state/modelctl/modelctl.db   # observed state
~/.cache/modelctl/                    # disposable probe caches
```

Configuration precedence:

```text
built-in safe defaults
< user config
< project config
< permitted CLI overrides
```

### 6.1 Example full configuration

```yaml
version: 1

defaults:
  startup_timeout_s: 1200
  graceful_stop_timeout_s: 30
  kill_timeout_s: 15
  cleanup_verify_timeout_s: 30
  bind_host: 127.0.0.1
  access: tunnel

  lifecycle:
    mode: persistent

  security:
    request_logging: false
    output_logging: false
    allow_remote_exposure: false
    state_file_mode: "0600"
    state_dir_mode: "0700"

machines:
  gpu-a:
    ssh:
      host: gpu-a.example.internal
      user: ink
      # Prefer ~/.ssh/config for IdentityFile / ProxyJump.

    supervisor: systemd-user

    inventory:
      roots:
        - /models
        - /mnt/models
      max_age_before_start_s: 3600
      auto_refresh_before_start: true

    runtime:
      type: venv
      activate: /opt/vllm/bin/activate

    gpu:
      sharing: exclusive-managed
      require_same_user_for_foreign_processes: false

    defaults:
      port: 8000
      gpu_memory_utilization: 0.90

models:
  qwen-72b:
    served_model_name: qwen-72b
    generation_config: vllm

targets:
  qwen-72b@gpu-a:
    model: qwen-72b
    machine: gpu-a

    artifact:
      path: /models/Qwen2.5-72B-Instruct
      require_observed: true

    gpus: [0, 1, 2, 3]

    lifecycle:
      mode: persistent

    vllm:
      tensor_parallel_size: 4
      max_model_len: 32768
      gpu_memory_utilization: 0.92
      dtype: auto
```

### 6.2 Strict schema behavior

The configuration parser must reject:

- unknown keys;
- duplicate aliases;
- nonexistent machine references;
- nonexistent model references;
- non-absolute artifact paths unless registry-style paths are explicitly enabled;
- impossible GPU indices;
- tensor-parallel settings exceeding declared visible GPUs;
- literal secret fields in config where a secret reference is expected;
- non-loopback bind hosts without explicit exposure approval;
- negative or unbounded safety timeouts.

### 6.3 Config digest

Canonicalize the fully resolved target and compute:

```text
config_digest = SHA256(canonical_json(resolved_target))
```

The digest is included in:

- remote deployment metadata;
- local deployment history;
- service unit metadata;
- CLI status.

---

## 7. CLI Contract

Scope: this section defines a stable operator and agent interface.

### 7.1 Top-level command tree

```text
modelctl
├── start MODEL --machine MACHINE
├── stop MODEL --machine MACHINE
├── restart MODEL --machine MACHINE
├── status [MODEL] [--machine MACHINE]
├── ps [--machine MACHINE]
├── connect MODEL --machine MACHINE
├── disconnect [MODEL] [--machine MACHINE]
├── endpoint MODEL --machine MACHINE
│
├── inventory
│   ├── sync
│   ├── list
│   ├── show
│   ├── history
│   ├── diff
│   └── stale
│
├── machines
│   ├── list
│   ├── show
│   └── probe
│
├── models
│   ├── list
│   └── show
│
├── targets
│   ├── list
│   └── show
│
├── gpu
│   ├── status
│   ├── reservations
│   └── reconcile
│
├── logs MODEL --machine MACHINE
├── events [--machine MACHINE] [--target TARGET]
├── reconcile [--machine MACHINE] [--fix-safe]
├── gc [--machine MACHINE]
├── doctor [--machine MACHINE] [--target TARGET]
├── config
│   ├── validate
│   ├── resolve
│   └── explain
└── version [--remote MACHINE]
```

### 7.2 Global automation flags

```text
--json
--non-interactive
--timeout <seconds>
--config <path>
--verbose
--trace-id <id>
```

Every command usable by an agent must support `--json`.

### 7.3 Start

```bash
modelctl start MODEL --machine MACHINE \
  [--wait | --no-wait] \
  [--replace] \
  [--ttl 2h] \
  [--persistent]
```

Default behavior:

1. Acquire target lock.
2. Load and validate configuration.
3. Refresh stale artifact inventory.
4. Resolve target and artifact.
5. Reconcile existing state.
6. If same digest is already healthy, return idempotent success.
7. If a conflicting managed deployment exists, refuse unless `--replace`.
8. Verify GPU ownership/reservation conditions.
9. Reserve GPUs atomically.
10. Create remote deployment directory.
11. Generate vLLM config.
12. Start supervisor unit.
13. Wait for process and vLLM health.
14. Verify served model identity.
15. Mark `READY`.
16. Return endpoint metadata.

### 7.4 Stop

```bash
modelctl stop MODEL --machine MACHINE [--force]
```

The normal stop algorithm is specified in Section 10.

`--force` means “escalate termination of this proven-owned deployment,” not “kill anything using these GPUs.”

### 7.5 Status

```bash
modelctl status qwen-72b --machine gpu-a
```

Status should show independent dimensions:

```text
CONTROL:       managed
PROCESS:       running
VLLM:          healthy
MODEL:         qwen-72b
ARTIFACT:      fingerprint matches
GPU:           reserved by this deployment
VRAM:          71.2 GiB / 80 GiB
NETWORK:       loopback only
TUNNEL:        local :18123
CONFIG DIGEST: 58b3...
STATE:         READY
```

### 7.6 `ps`

Fleet-level deployment view:

```text
TARGET             STATE    GPU(S)      VRAM       AGE      LEASE
qwen-72b@gpu-a     READY    0,1,2,3     286 GiB    1h12m    persistent
coder-32b@gpu-b    STOPPED  -           0          -        -
```

### 7.7 `reconcile`

`reconcile` compares:

- local DB;
- remote deployment metadata;
- supervisor units;
- processes/cgroups;
- GPU compute-process state;
- vLLM health.

It may repair only safe metadata inconsistencies automatically.

Examples of safe repair:

- local DB says `STARTING`, but remote unit is healthy → mark `READY`;
- local DB says `READY`, but unit is gone and no owned GPU process remains → mark `FAILED_EXITED`/`STOPPED_UNCLEAN`;
- stale local tunnel PID → remove local tunnel metadata.

Unsafe repair requiring operator intervention:

- unknown process owns configured GPU;
- process exists outside expected cgroup;
- model artifact fingerprint changed unexpectedly;
- GPU memory is occupied but no matching managed process can be identified.

---

## 8. Lifecycle State Machine

Scope: this section makes transitions explicit so crashes and retries remain deterministic.

```text
                 ┌──────────────┐
                 │   STOPPED    │
                 └──────┬───────┘
                        │ start
                        ▼
                 ┌──────────────┐
                 │  PREFLIGHT   │
                 └──────┬───────┘
                        │ verified
                        ▼
                 ┌──────────────┐
                 │   STARTING   │
                 └──────┬───────┘
                        │ /health + model identity
                        ▼
                 ┌──────────────┐
                 │    READY     │
                 └──────┬───────┘
                        │ stop / lease expiry
                        ▼
                 ┌──────────────┐
                 │   DRAINING   │
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐
                 │   STOPPING   │
                 └──────┬───────┘
                        │ process + GPU postconditions
              ┌─────────┴─────────┐
              ▼                   ▼
       ┌──────────────┐    ┌────────────────┐
       │   STOPPED    │    │ LEAK_SUSPECTED │
       └──────────────┘    └────────────────┘
```

Additional states:

- `FAILED_PREFLIGHT`
- `FAILED_START`
- `FAILED_HEALTH`
- `DRIFTED`
- `RECONCILE_REQUIRED`
- `UNKNOWN_REMOTE`

### 8.1 State transition rule

State is not inferred from one signal.

For example, `READY` requires:

```text
supervisor active
AND health endpoint healthy
AND served model identity correct
AND deployment digest correct
AND expected artifact fingerprint correct
```

The vLLM health endpoint checks engine health and can return HTTP 503 if the engine is dead. [vLLM Health API](https://docs.vllm.ai/en/latest/api/vllm/entrypoints/serve/instrumentator/health/)

---

## 9. Start Transaction and Preflight

Scope: this section specifies the exact sequence before GPU memory is committed.

### 9.1 Phase A — local validation

Fail before SSH when possible:

- config schema;
- target existence;
- allowed overrides;
- lifecycle policy;
- syntactic GPU declarations;
- required secrets references.

### 9.2 Phase B — remote identity

Verify:

- SSH connectivity;
- expected hostname/machine identity;
- remote user;
- expected architecture/OS;
- NVIDIA driver/NVML availability;
- runtime/vLLM executable.

Optional strict mode may pin the SSH host key/fingerprint in inventory.

### 9.3 Phase C — model artifact

Verify:

- canonical path exists;
- path remains inside an allowed inventory root;
- expected files exist;
- artifact fingerprint matches the selected observation or is refreshed;
- files are readable by the serving user.

### 9.4 Phase D — GPU preflight

Collect:

- GPU UUID/index mapping;
- total and free memory;
- compute processes;
- current managed reservations;
- MIG topology if applicable;
- driver-visible health signals available to the implementation.

For programmatic GPU process inspection, prefer NVML bindings over parsing human `nvidia-smi` output. NVIDIA states that `nvidia-smi` output is not guaranteed to remain backwards compatible, while NVML is the underlying stable API intended for maintained tooling. [NVIDIA NVSMI documentation](https://docs.nvidia.com/deploy/nvidia-smi/index.html)

### 9.5 GPU policy result

Each configured GPU must resolve to exactly one of:

- `FREE`
- `OWNED_BY_SAME_DEPLOYMENT`
- `RESERVED_BY_OTHER_MODELCTL_DEPLOYMENT`
- `BUSY_FOREIGN`
- `UNKNOWN`

Only the first two are normally startable.

### 9.6 Phase E — port and runtime

Verify:

- configured remote port is unused or belongs to the same deployment;
- state directory is writable;
- required environment exists;
- vLLM version is supported by the runtime adapter.

### 9.7 Phase F — reservation

Acquire locks in deterministic order:

```text
machine lock
target lock
GPU UUID locks sorted lexicographically
port lock
```

Locks must include ownership and expiry/recovery metadata.

---

## 10. Stop, Cleanup, and VRAM Release

Scope: this section is the core defense against a model remaining in VRAM after it should be gone.

### 10.1 Stop semantics

For `modelctl`, **stop means process termination and verified release**, not vLLM sleep mode.

vLLM Sleep Mode can release most GPU memory, and level 2 discards model weights and KV cache, but the server remains alive and some buffers may remain in CPU memory. Therefore sleep is a separate optional state and must not satisfy the `STOPPED` invariant. [vLLM Sleep Mode](https://docs.vllm.ai/en/latest/features/sleep_mode/)

### 10.2 Normal stop sequence

```text
1. acquire target lock
2. reconcile deployment ownership
3. mark DRAINING
4. stop accepting new managed connections
5. request graceful supervisor stop
6. wait graceful_stop_timeout_s
7. inspect owned cgroup
8. if owned processes remain:
      send SIGTERM to owned unit/cgroup
9. wait kill_timeout_s
10. if owned processes remain:
      send SIGKILL to owned unit/cgroup
11. verify owned cgroup is empty
12. query GPU compute processes
13. verify no deployment-owned PID remains on any reserved GPU
14. release GPU reservation
15. mark STOPPED
16. close/remove local tunnels
```

### 10.3 Supervisor containment

The preferred Linux implementation uses a dedicated systemd user service per deployment and explicitly configures group-based termination so child worker processes are part of the same lifecycle boundary.

The implementation should not rely on discovering all vLLM child PIDs manually.

### 10.4 VRAM postcondition

For each reserved GPU, record:

```text
before_stop:
  deployment_owned_gpu_processes
  gpu_used_memory

after_stop:
  deployment_owned_gpu_processes
  gpu_used_memory
```

Success requires:

```text
deployment_owned_gpu_processes_after_stop = ∅
```

The absolute GPU memory value does **not** need to become zero because desktop, display, system, or unrelated compute processes may use memory. The key condition is removal of all modelctl-owned compute processes and release of the modelctl reservation.

NVML exposes running compute processes and per-process GPU memory information, which is suitable for this ownership-oriented check. [NVIDIA NVML device queries](https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html)

### 10.5 `LEAK_SUSPECTED`

Enter this state if:

- the service appears stopped but an owned PID still appears in NVML;
- an expected owned process cannot be correlated cleanly;
- cgroup and GPU process views disagree beyond the cleanup timeout;
- an owned process is stuck in an uninterruptible/driver-failure condition.

Behavior:

```text
STATE = LEAK_SUSPECTED
GPU reservation remains held
automatic replacement start is blocked
event is recorded at ERROR severity
doctor command is suggested
```

`modelctl` must not free the reservation merely to make the UI look clean.

### 10.6 No automatic GPU reset

A GPU reset is a machine-wide/destructive administrative operation and can affect unrelated processes.

`modelctl` v1 should never automatically run GPU reset as cleanup.

If advanced recovery is later added, it must be a separate privileged command with explicit machine-wide impact confirmation.

---

## 11. Model Leakage Threat Model

Scope: “model leakage” is treated broadly as accidental exposure of model artifacts, prompt/output data, or residual runtime state.

### 11.1 Network leakage

Default:

```text
remote vLLM bind = 127.0.0.1
access           = SSH local forwarding
```

Current vLLM security documentation states that its API key does not authenticate every endpoint, so network isolation is the primary protection rather than relying only on the vLLM bearer key. [vLLM Security](https://docs.vllm.ai/en/stable/usage/security/)

### 11.2 Artifact leakage

`modelctl` does not:

- copy model files to the controller;
- upload model files elsewhere;
- include model file contents in inventory;
- print sensitive absolute paths in normal non-debug user output if path redaction is configured.

Inventory stores metadata and fingerprints only.

### 11.3 Prompt/output leakage

Safe vLLM defaults should be explicit in generated configuration:

```yaml
enable-log-requests: false
enable-log-outputs: false
```

Current vLLM documentation notes that enabling request logging at DEBUG can include prompt inputs; therefore `modelctl` must prevent request logging unless the operator explicitly opts in for diagnostics. [vLLM `serve` CLI](https://docs.vllm.ai/en/stable/cli/serve/)

If opt-in request logging is enabled, `modelctl` must print a warning that potentially sensitive prompt data may enter the journal.

### 11.4 Environment/secret leakage

Secrets must be passed through:

- environment variable references;
- protected credential files;
- OS-native credential mechanisms.

Never:

- store raw API keys in the event DB;
- echo them in command logs;
- include them in `--json` output;
- include them in generated unit names;
- include them in exception traces.

### 11.5 Residual GPU memory

A successful `stop` requires owned process disappearance as described in Section 10.

A sleeping vLLM server is not equivalent to stopped.

### 11.6 CPU memory residue

If a future `sleep` feature is implemented, vLLM level 1 can retain model weights in CPU memory while discarding KV cache; level 2 discards model weights and KV cache but may retain buffers. `modelctl sleep` therefore must report host-memory semantics explicitly. [vLLM Sleep Mode](https://docs.vllm.ai/en/latest/features/sleep_mode/)

### 11.7 Disk cache policy

Configuration should allow:

```yaml
cache:
  huggingface_offline: true
  allow_downloads: false
  temp_dir: ~/.cache/modelctl/vllm
  cleanup_generated_config: true
```

For the requested use case, v1 should default to no implicit model download.

---

## 12. vLLM Integration Contract

Scope: this section defines how `modelctl` safely adapts to a changing vLLM CLI.

### 12.1 Adapter boundary

All vLLM-specific behavior lives behind:

```python
class RuntimeAdapter:
    detect_version(...)
    validate_target(...)
    generate_config(...)
    build_launch_spec(...)
    check_health(...)
    check_model_identity(...)
```

The rest of `modelctl` must not directly construct vLLM flags.

### 12.2 Version capability table

Maintain a tested capability map:

```text
vLLM version range
→ supported flags
→ readiness endpoint behavior
→ sleep support
→ known incompatibilities
```

Unknown versions:

- warn and refuse in strict production mode;
- optionally allow with `--allow-untested-runtime` for an operator.

### 12.3 Generated vLLM configuration

Example:

```yaml
model: /models/Qwen2.5-72B-Instruct
host: 127.0.0.1
port: 8000
served-model-name: qwen-72b
tensor-parallel-size: 4
max-model-len: 32768
gpu-memory-utilization: 0.92
generation-config: vllm
enable-log-requests: false
enable-log-outputs: false
```

Current vLLM `serve` supports configuration through YAML via `--config`. [vLLM `serve` CLI](https://docs.vllm.ai/en/stable/cli/serve/)

### 12.4 Readiness

A deployment is not ready until:

1. TCP socket is listening.
2. `/health` returns healthy.
3. `/v1/models` or the runtime-equivalent identity check confirms the expected served model.
4. The supervisor unit is still active after those checks.
5. No fatal startup event was detected.

### 12.5 Health loop

Use bounded exponential backoff with jitter.

Example policy:

```text
initial delay: 250 ms
max delay:     5 s
deadline:      startup_timeout_s
```

On timeout, include the final relevant supervisor log tail in structured diagnostics, with secret redaction.

### 12.6 Shutdown timeout

The vLLM CLI exposes a shutdown timeout setting. `modelctl` may use it as part of graceful shutdown, but the final lifecycle guarantee remains the supervisor/cgroup and GPU postcondition rather than trusting one runtime flag. [vLLM `serve` CLI](https://docs.vllm.ai/en/stable/cli/serve/)

---

## 13. Remote Supervisor Specification

Scope: this section defines how vLLM becomes an owned, recoverable process tree.

Preferred supervisor:

```text
systemd --user
```

One deployment maps to one service.

Example conceptual unit properties:

```ini
[Service]
Type=simple
ExecStart=/path/to/modelctl-remote-launch <deployment-id>
Restart=no
KillMode=control-group
TimeoutStopSec=30s
EnvironmentFile=/private/modelctl/deployment.env
```

Generated unit/config files must be readable only by the serving user unless the operator deliberately changes policy.

### 13.1 Why a supervisor is mandatory

The local CLI can disconnect or crash after launching.

The deployment therefore cannot be owned by the lifetime of the SSH shell.

A supervisor provides a stable remote lifecycle object that can be re-discovered.

### 13.2 Unit identity

Unit name should be derived from a sanitized target ID plus deployment digest prefix, for example:

```text
modelctl-qwen-72b-gpu-a-58b3c7f1.service
```

The complete deployment ID lives in metadata, not only in the truncated service name.

---

## 14. Locks, Reservations, and Concurrency

Scope: this section prevents double starts, races, and two operators assigning the same GPU concurrently.

### 14.1 Remote lock directory

```text
~/.local/state/modelctl/locks/
```

Recommended locks:

```text
machine.lock
target/<target-id>.lock
gpu/<gpu-uuid>.lock
port/<port>.lock
```

### 14.2 Atomicity

Use an OS-backed file lock (`flock` or equivalent) on the remote host.

Do not implement concurrency control using “check if a file exists, then create it.”

### 14.3 Crash recovery

A lock file may contain metadata, but the kernel lock is authoritative while the process is alive.

Reservations stored in metadata are reconciled against actual supervisor/process state after crashes.

### 14.4 Multi-CLI behavior

If two callers concurrently run:

```bash
modelctl start qwen-72b --machine gpu-a
```

one acquires the lock. The other waits for a bounded period, then rechecks. If the first successfully reaches the same `READY` digest, the second returns idempotent success.

---

## 15. Lease and Auto-Cleanup Policy

Scope: this section prevents accidental indefinite VRAM occupancy.

A model remaining in VRAM because nobody remembered to stop it is an operational leak even if no process bug occurred.

### 15.1 Recommended lifecycle modes

```yaml
lifecycle:
  mode: persistent | ttl | session
  ttl_s: 7200
  lease_grace_s: 120
```

### 15.2 Persistent

```text
start → remains until explicit stop
```

Use for long-running personal services.

### 15.3 TTL

```bash
modelctl start qwen-72b --machine gpu-a --ttl 2h
```

The remote lease expires even if the local controller disappears.

The remote cleanup path must use the same stop postconditions.

### 15.4 Session lease

A managed `connect` session periodically renews the remote lease.

If the local machine disappears, the lease eventually expires and the remote model is stopped.

This is the safest default for ad-hoc use if the user does not need a persistent endpoint.

### 15.5 Idle timeout

Do not implement an “idle request timeout” in v1 unless `modelctl` controls the request proxy or has a reliable runtime-level last-request signal.

A plain SSH tunnel alone does not give the controller a trustworthy semantic definition of inference idleness.

---

## 16. SSH and Network Transport

Scope: this section constrains remote execution and endpoint exposure.

### 16.1 Use OpenSSH as transport

Preferred approach:

- reuse `~/.ssh/config`;
- support `ProxyJump`;
- support ControlMaster/ControlPersist where appropriate;
- avoid duplicating SSH authentication logic in Python.

### 16.2 No generic shell pass-through

There is no command like:

```bash
modelctl ssh gpu-a -- arbitrary command
```

in the agent-facing surface.

The internal SSH adapter executes predefined remote operations with strict argument encoding.

### 16.3 Tunnel

```bash
modelctl connect qwen-72b --machine gpu-a
```

returns a local endpoint:

```text
http://127.0.0.1:18123/v1
```

The remote vLLM endpoint remains:

```text
127.0.0.1:8000
```

### 16.4 Tunnel ownership

Store:

- local PID;
- SSH control socket/path if used;
- local port;
- target/deployment ID;
- creation time.

`disconnect` kills only the owned tunnel.

---

## 17. Error Model

Scope: this section prevents “weird errors” by making every failure class stable and actionable.

All CLI failures return:

```json
{
  "ok": false,
  "code": "E_GPU_BUSY_FOREIGN",
  "message": "GPU 2 on gpu-a is used by an unmanaged process.",
  "target": "qwen-72b@gpu-a",
  "retryable": false,
  "details": {
    "gpu_uuid": "...",
    "pid": 12345
  },
  "suggested_action": "modelctl gpu status --machine gpu-a"
}
```

### 17.1 Error taxonomy

```text
E_CONFIG_INVALID
E_CONFIG_UNKNOWN_FIELD
E_TARGET_NOT_FOUND
E_MACHINE_NOT_FOUND
E_MODEL_NOT_FOUND

E_SSH_UNREACHABLE
E_SSH_HOSTKEY_CHANGED
E_REMOTE_IDENTITY_MISMATCH

E_ARTIFACT_MISSING
E_ARTIFACT_STALE
E_ARTIFACT_CHANGED
E_ARTIFACT_AMBIGUOUS
E_ARTIFACT_UNREADABLE

E_GPU_NOT_FOUND
E_GPU_BUSY_MANAGED
E_GPU_BUSY_FOREIGN
E_GPU_STATE_UNKNOWN
E_GPU_RESERVATION_CONFLICT

E_PORT_BUSY
E_RUNTIME_NOT_FOUND
E_RUNTIME_VERSION_UNSUPPORTED
E_PREFLIGHT_FAILED

E_START_TIMEOUT
E_START_EXITED
E_HEALTH_FAILED
E_MODEL_IDENTITY_MISMATCH

E_STOP_TIMEOUT
E_PROCESS_OWNERSHIP_UNPROVEN
E_VRAM_RELEASE_UNVERIFIED
E_LEAK_SUSPECTED

E_TUNNEL_FAILED
E_STATE_CORRUPT
E_RECONCILE_REQUIRED
E_INTERNAL
```

### 17.2 Error rules

- No raw Python traceback in normal CLI output.
- Traceback only with `--debug`.
- Secrets always redacted.
- Human output includes one next command.
- JSON output never changes field meaning across patch releases.
- Unexpected exceptions map to `E_INTERNAL` and include a trace ID.

---

## 18. Event Journal and Auditability

Scope: this section records what happened without recording sensitive inference content.

Record control-plane events:

```text
INVENTORY_PROBE_STARTED
INVENTORY_ARTIFACT_FOUND
INVENTORY_ARTIFACT_MISSING
ARTIFACT_CHANGED

START_REQUESTED
PREFLIGHT_PASSED
GPU_RESERVED
SERVICE_STARTED
HEALTH_READY
DEPLOYMENT_READY

STOP_REQUESTED
SERVICE_STOPPED
GPU_RELEASE_VERIFIED
DEPLOYMENT_STOPPED

LEAK_SUSPECTED
RECONCILE_PERFORMED
LEASE_EXPIRED
```

Each event includes:

- timestamp;
- trace ID;
- machine;
- target;
- deployment ID;
- result;
- duration;
- error code if relevant;
- sanitized metadata.

Do not store prompts, completions, authorization headers, or raw secret-bearing environment variables.

---

## 19. Observability

Scope: this section defines operator-visible signals without turning the tool into a full monitoring platform.

### 19.1 Logs

```bash
modelctl logs qwen-72b --machine gpu-a
modelctl logs qwen-72b --machine gpu-a -f
modelctl logs qwen-72b --machine gpu-a --since 30m
```

Logs should be sourced from the supervisor/journal when possible.

### 19.2 Metrics

`status --json` can expose:

- state;
- uptime;
- configured GPUs;
- per-GPU total/used/free memory;
- deployment-owned process memory;
- restart count;
- vLLM health;
- lease expiry.

Do not expose request text.

### 19.3 Doctor

```bash
modelctl doctor --target qwen-72b@gpu-a
```

checks:

```text
configuration
SSH
machine identity
inventory freshness
artifact fingerprint
runtime/vLLM version
GPU mapping
GPU occupancy
supervisor
state directories
ports
remote loopback binding policy
stale deployments
stale reservations
owned residual GPU processes
```

---

## 20. Reconciliation and Garbage Collection

Scope: this section handles abnormal termination of the local controller or remote services.

### 20.1 `reconcile`

Read-only by default.

```bash
modelctl reconcile --machine gpu-a
```

With:

```bash
modelctl reconcile --machine gpu-a --fix-safe
```

it may repair only inconsistencies that do not require killing unknown processes.

### 20.2 `gc`

`gc` removes artifacts owned by the control plane that are no longer active:

- stale generated vLLM YAML;
- completed deployment metadata past retention;
- dead tunnel metadata;
- expired local caches;
- orphaned supervisor units proven inactive and owned;
- expired reservations proven not to correspond to a live deployment.

`gc` never deletes model weights.

### 20.3 Boot recovery

After remote reboot:

- supervisor units may be absent/inactive;
- GPU processes disappear;
- local DB may still say `READY`.

The first `status`, `start`, or scheduled inventory/reconcile operation detects this and corrects the state.

---

## 21. Agent Skill Specification

Scope: this section defines the behavior of the `modelctl` skill itself, separate from the CLI implementation.

The skill should invoke only the `modelctl` CLI.

### 21.1 Skill trigger

Use the skill when the user asks to:

- run a configured local model;
- access a model on one of the remote machines;
- inspect which machine has a model;
- start/stop/restart a model server;
- get the local endpoint;
- inspect model-serving status/logs.

### 21.2 Skill decision flow

```text
User intent
   │
   ├── exact model + machine known?
   │      │
   │      ├── yes → resolve target
   │      └── no  → query inventory/targets
   │
   ├── mutation requested?
   │      │
   │      ├── yes → require one exact target
   │      └── no  → safe discovery/status
   │
   └── invoke modelctl --json --non-interactive
```

### 21.3 Skill rules

The skill must:

- use `modelctl inventory list --json` or `targets list --json` when placement is ambiguous;
- never invent a machine alias;
- never invent a model alias;
- never modify config unless explicitly asked;
- never issue an arbitrary remote shell command;
- never pass free-form vLLM flags;
- surface the stable error code;
- recommend the CLI-provided diagnostic action;
- prefer `connect`/`endpoint` over direct remote network exposure;
- stop only the exact selected target.

### 21.4 Example skill behavior

User:

```text
Use qwen-72b.
```

If two machines have the model available, the skill discovers:

```text
qwen-72b@gpu-a  AVAILABLE
qwen-72b@gpu-b  AVAILABLE
```

It must not randomly choose one unless a configured scheduling/default policy explicitly authorizes that behavior.

User:

```text
Use qwen-72b on gpu-a.
```

Skill:

```bash
modelctl start qwen-72b --machine gpu-a --json --non-interactive
modelctl connect qwen-72b --machine gpu-a --json --non-interactive
```

---

## 22. Reference Project Structure

Scope: this section defines a maintainable implementation boundary.

```text
modelctl/
├── pyproject.toml
├── README.md
├── SKILL.md
├── src/modelctl/
│   ├── cli.py
│   ├── errors.py
│   ├── domain.py
│   ├── config/
│   │   ├── loader.py
│   │   ├── schema.py
│   │   └── resolver.py
│   ├── inventory/
│   │   ├── registry.py
│   │   ├── scanner.py
│   │   ├── fingerprint.py
│   │   └── migrations.py
│   ├── transport/
│   │   └── ssh.py
│   ├── supervisor/
│   │   └── systemd.py
│   ├── gpu/
│   │   ├── nvml.py
│   │   ├── reservations.py
│   │   └── policy.py
│   ├── runtime/
│   │   └── vllm.py
│   ├── lifecycle/
│   │   ├── start.py
│   │   ├── stop.py
│   │   ├── reconcile.py
│   │   └── lease.py
│   ├── tunnel.py
│   ├── state.py
│   ├── events.py
│   └── diagnostics.py
├── schemas/
│   └── modelctl.schema.json
├── examples/
│   └── modelctl.yaml
└── tests/
    ├── unit/
    ├── integration/
    ├── chaos/
    └── hardware/
```

Recommended implementation language: Python 3.11+ for v1, with strongly typed models and explicit adapters.

---

## 23. Testing and Quality Gates

Scope: this section defines what must be tested before calling the tool reliable.

### 23.1 Configuration tests

- unknown field rejected;
- duplicate target rejected;
- unsafe bind rejected;
- missing artifact reference rejected;
- deterministic config digest;
- CLI precedence deterministic;
- secrets redacted.

### 23.2 Inventory tests

- discover configured model artifact;
- mark missing artifact;
- retain history;
- detect fingerprint change;
- stale observation triggers refresh;
- scan cannot leave configured roots;
- unreachable machine does not falsely mark all models deleted;
- concurrent sync remains transactionally consistent.

### 23.3 Lifecycle tests

- same target/same digest start is idempotent;
- same target/different digest requires replacement;
- start crash before service creation;
- start crash after service creation but before local state update;
- health timeout;
- process exits during readiness;
- stop graceful success;
- stop SIGTERM escalation;
- stop SIGKILL escalation;
- child worker remains → cleanup catches it;
- unknown GPU process → never killed;
- reservation remains held on leak suspicion.

### 23.4 VRAM cleanup hardware tests

On real NVIDIA hardware:

1. Start target.
2. Record modelctl-owned GPU processes.
3. Verify expected VRAM allocation.
4. Stop target.
5. Poll NVML.
6. Assert all modelctl-owned GPU process entries disappear.
7. Assert target reservation is released.
8. Immediately start another target on the same GPU.
9. Repeat under:
   - normal stop;
   - local CLI killed mid-stop;
   - SSH link drop;
   - vLLM worker crash;
   - supervisor kill;
   - forced termination.

### 23.5 Security tests

- vLLM not reachable via remote non-loopback interface by default;
- no prompt/output body in control-plane event DB;
- no secret in logs;
- state files have expected permissions;
- malicious model alias cannot inject shell;
- malicious path cannot escape configured inventory roots;
- SSH command arguments remain correctly escaped;
- modelctl cannot kill a foreign process sharing a GPU.

### 23.6 Chaos tests

Inject:

- SSH packet loss;
- machine reboot;
- full disk in state directory;
- port race;
- SQLite write interruption;
- stale PID reuse;
- runtime environment missing;
- NVIDIA driver temporarily unavailable;
- corrupted artifact metadata;
- two concurrent starts;
- stop during startup.

Every case must end in a known state with a stable error code.

---

## 24. Acceptance Criteria

Scope: these are the release-level “done” conditions.

The v1 release is acceptable only if all are true:

1. `modelctl inventory list` accurately shows the last verified machine/model availability and timestamp.
2. `inventory sync` records appearance, disappearance, and fingerprint changes without copying model data.
3. `start MODEL --machine MACHINE` launches the exact target-specific configuration.
4. Two identical starts do not create duplicate servers.
5. A configuration or artifact mismatch never silently reuses the wrong deployment.
6. A busy foreign GPU causes a refusal, not a kill.
7. `READY` means process + health + model identity are all verified.
8. Remote vLLM is loopback-bound by default.
9. Prompt and output logging are disabled by default.
10. `stop` succeeds only after all modelctl-owned GPU processes are gone.
11. A residual owned GPU process produces `LEAK_SUSPECTED`.
12. A leaked/suspicious GPU reservation blocks automatic replacement.
13. Reconciliation can recover after the local CLI crashes.
14. A remote model can be leased so forgotten sessions eventually clean themselves up.
15. Every command has structured JSON and stable exit/error semantics.
16. The skill never constructs direct SSH/vLLM commands itself.
17. No model weights are deleted by normal lifecycle or garbage-collection commands.

---

## 25. Delivery Plan

Scope: this section orders implementation so safety primitives exist before real model launches.

### Phase 0 — contracts

Implement:

- domain models;
- YAML schema;
- config resolution;
- config digest;
- stable error object;
- JSON CLI output conventions.

Deliver:

```bash
modelctl config validate
modelctl config resolve
modelctl targets list
```

### Phase 1 — inventory

Implement:

- SQLite schema/migrations;
- machine probe;
- restricted-root scanner;
- artifact fingerprint;
- inventory history.

Deliver:

```bash
modelctl inventory sync
modelctl inventory list
modelctl inventory history
modelctl machines probe
```

### Phase 2 — transport and supervisor

Implement:

- OpenSSH adapter;
- remote private state directory;
- systemd-user adapter;
- deployment metadata;
- target/GPU/port locking.

Use a fake runtime first.

### Phase 3 — lifecycle state machine

Implement:

- transactional start;
- transactional stop;
- reconciliation;
- event journal;
- ownership proof;
- lease expiry.

Prove cleanup with a fake multi-process runtime before vLLM integration.

### Phase 4 — GPU/NVML safety

Implement:

- GPU UUID mapping;
- process inspection;
- reservations;
- VRAM cleanup verification;
- leak-suspected state.

Do not proceed to “production-grade” labeling without hardware tests.

### Phase 5 — vLLM adapter

Implement:

- vLLM version detection;
- capability matrix;
- generated YAML;
- `/health`;
- served-model identity;
- safe logging defaults;
- shutdown behavior.

### Phase 6 — tunnel and endpoint UX

Implement:

```bash
modelctl connect
modelctl disconnect
modelctl endpoint
```

### Phase 7 — skill

Create `SKILL.md` after the CLI contracts stabilize.

The skill should remain thin and deterministic.

### Phase 8 — chaos/release hardening

Run the failure matrix, lock down schemas and error codes, and document migration policy.

---

## 26. Recommended First Milestone

Scope: this is the smallest implementation slice that materially reduces future risk.

Build these components first:

```text
1. Config schema + resolver
2. SQLite inventory/event store
3. SSH machine probe
4. Restricted model-root scanner
5. Artifact fingerprinting
6. `inventory sync/list/history`
7. Fake supervisor/runtime
8. Remote locks and deployment metadata
9. Start/stop state machine against fake runtime
10. NVML ownership verification
```

Only after those pass should real vLLM startup be added.

This ordering prevents the project from becoming a collection of SSH shell snippets that later need to be retrofitted with ownership, inventory, and cleanup semantics.

---

## 27. Example End-to-End Operator Flow

Scope: this example demonstrates the intended normal UX.

### 27.1 Discover what exists

```bash
$ modelctl inventory sync --machine gpu-a
gpu-a: reachable
3 configured model artifacts verified
1 artifact changed since prior observation

$ modelctl inventory list --machine gpu-a
MODEL       STATUS      PATH
qwen-72b    available   /models/Qwen2.5-72B-Instruct
coder-32b   available   /models/Coder-32B-AWQ
vision-8b   missing     /models/Vision-8B
```

### 27.2 Start

```bash
$ modelctl start qwen-72b --machine gpu-a
[ok] configuration valid
[ok] artifact verified: 58a5...
[ok] GPUs 0,1,2,3 available
[ok] GPUs reserved
[ok] vLLM 0.x adapter supported
[ok] service started
[ok] /health ready
[ok] served model identity: qwen-72b

READY qwen-72b@gpu-a
```

### 27.3 Connect

```bash
$ modelctl connect qwen-72b --machine gpu-a --detach
http://127.0.0.1:18123/v1
```

### 27.4 Stop

```bash
$ modelctl stop qwen-72b --machine gpu-a
[ok] service stopped
[ok] process cgroup empty
[ok] no deployment-owned GPU process remains
[ok] GPU reservations released

STOPPED qwen-72b@gpu-a
```

### 27.5 Suspicious cleanup case

```bash
$ modelctl stop qwen-72b --machine gpu-a
[ok] supervisor inactive
[error] deployment-owned GPU process still present on GPU 2

E_LEAK_SUSPECTED
GPU reservation retained.
Run: modelctl doctor --target qwen-72b@gpu-a
```

The tool does not claim success and does not kill unrelated processes.

---

## 28. Primary Technical Evidence

Scope: these references anchor external runtime assumptions; the architecture itself is a proposed `modelctl` design.

1. **vLLM Security.** vLLM documents that `--api-key` protects only certain API path prefixes and should not be the sole security measure.  
   https://docs.vllm.ai/en/stable/usage/security/

2. **vLLM `serve` CLI.** Documents YAML `--config`, health/runtime flags, shutdown timeout, and request/output logging controls.  
   https://docs.vllm.ai/en/stable/cli/serve/

3. **vLLM Health API.** Documents `/health` and engine-dead behavior.  
   https://docs.vllm.ai/en/latest/api/vllm/entrypoints/serve/instrumentator/health/

4. **vLLM Sleep Mode.** Documents that sleep can release most GPU memory; level 1 offloads weights and discards KV cache, while level 2 discards weights and KV cache but is still a sleep state rather than process termination.  
   https://docs.vllm.ai/en/latest/features/sleep_mode/

5. **NVIDIA `nvidia-smi` / NVML documentation.** NVIDIA notes that `nvidia-smi` textual output is not guaranteed backward compatible and identifies NVML as the underlying management API appropriate for maintained tooling.  
   https://docs.nvidia.com/deploy/nvidia-smi/index.html

6. **NVIDIA NVML device queries.** Provides APIs for querying running compute processes and their GPU memory use.  
   https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html

---

## 29. Codex → OpenCode Delegation Architecture

Scope: this section defines the primary goal of the delegation subsystem: keep Codex-class models such as Sol/Terra focused on reasoning and judgment while moving repetitive execution to cheap OpenCode delegates.

```text
                           ┌───────────────────────────┐
                           │     CODEX / SOL / TERRA   │
                           │   brain / planner / judge │
                           └─────────────┬─────────────┘
                                         │
                              structured task graph
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │      modelctl       │
                              │ delegation runtime  │
                              └─────────┬───────────┘
                                        │
                         ┌──────────────┴──────────────┐
                         ▼                             ▼
                ┌─────────────────┐           ┌─────────────────┐
                │ DRIVER INSTANCE │           │ WORKER INSTANCES│
                │   Muse Spark    │           │   Muse Spark    │
                │ coherent chunk  │           │ repetitive jobs │
                └────────┬────────┘           └───────┬─────────┘
                         │                             │
                         └──────────────┬──────────────┘
                                        ▼
                              validation / reduction
                                        │
                                        ▼
                           ┌───────────────────────────┐
                           │     CODEX / SOL / TERRA   │
                           │ review / merge / next plan│
                           └───────────────────────────┘
```

\[
\boxed{
\text{Brain plans}
\rightarrow
\text{modelctl dispatches}
\rightarrow
\text{Muse executes}
\rightarrow
\text{modelctl validates/reduces}
\rightarrow
\text{Brain decides}
}
\]

OpenCode delegates are execution capacity, not peer planners.

### 29.1 Primary optimization target

The system optimizes **brain-token conservation**.

Codex/Sol/Terra should spend effort on:

```text
problem framing
requirements interpretation
architecture
task decomposition
dependency ordering
hard debugging
judgment
acceptance/rejection
integration
final answer
```

Muse Spark should spend effort on:

```text
repository search
file-by-file inspection
call-site enumeration
boilerplate
test generation
mechanical edits
small implementation chunks
documentation updates
log/error classification
schema conversion
candidate patches
repeated checks
parallel investigation
```

The core heuristic is:

```text
If a task is cheap to describe and cheap to verify, delegate it.
If a task is expensive to describe correctly or hard to verify, keep it in the brain.
```

### 29.2 OpenCode invocation contract

The backend command is OpenCode's non-interactive runner:

```bash
opencode --pure run \
  --model opencode-go/muse-spark-1.2-contributor \
  --agent <modelctl-role-profile> \
  --format json \
  --dir <isolated-workspace> \
  "<compiled-task-prompt>"
```

`modelctl` builds an argv vector directly and never constructs shell commands by interpolating model-generated text.

OpenCode currently documents `opencode run`, explicit model selection, agent selection, working-directory selection, JSON output, and `--pure`. Muse Spark 1.2 Contributor is currently available in OpenCode Go.

### 29.3 One model, multiple execution roles

`driver` and `worker` are role profiles, not separate model families.

Both use:

```text
opencode-go/muse-spark-1.2-contributor
```

They differ by:

- prompt contract;
- amount of repository context;
- permitted tools;
- writable path scope;
- expected task duration;
- validation depth;
- maximum patch size;
- concurrency policy.

## 30. Three-Tier Control Model

Scope: the three tiers describe orchestration authority rather than three separate provider models.

| Tier | Runtime | Role | Authority |
|---|---|---|---|
| frontier / brain | Codex, Sol, Terra, or current Codex-class model | planning, hard reasoning, judgment, integration | final authority |
| driver | Muse Spark via OpenCode | execute one coherent implementation unit | candidate producer |
| worker | Muse Spark via OpenCode | execute repetitive/bounded subtasks | candidate/fact producer |

\[
\boxed{
\text{frontier}=\text{brain},\qquad
\text{driver}=\text{Muse},\qquad
\text{worker}=\text{Muse}
}
\]

### 30.1 Brain responsibilities

The brain owns:

1. Understand the user's objective.
2. Build the task graph.
3. Identify delegatable nodes.
4. Define success criteria.
5. Choose worker versus driver granularity.
6. Review condensed delegate results.
7. Resolve conflicts.
8. Integrate accepted patches/findings.
9. Re-plan when evidence changes.
10. Produce the final result.

### 30.2 Driver responsibilities

A driver receives a coherent work package:

```text
implement one already-designed module
fix a reproducible bug and add tests
convert one package to a new interface
produce a candidate patch for one feature slice
analyze one subsystem and propose exact edits
```

Recommended envelope:

```text
parallelism: low
context: medium
write scope: bounded
task duration: medium
validation: compile/test/scope checks
output: patch + concise report
```

### 30.3 Worker responsibilities

A worker receives a small independent unit:

```text
find all call sites
inspect one directory
summarize one file
generate tests for one function
convert one config file
classify one batch of failures
update one repetitive documentation section
check one hypothesis
extract a structured list
```

Recommended envelope:

```text
parallelism: high
context: minimal
write scope: read-only or very narrow
task duration: short
validation: deterministic where possible
output: structured result
```

### 30.4 No worker-led planning

Workers do not decide what the project should do next. If a worker reaches project-level ambiguity, it returns a compact blocker to the brain.

## 31. Production OpenCode Model Policy

Scope: Muse Spark is the standard OpenCode execution model for both driver and worker roles.

| Role | Model | Exact reference |
|---|---|---|
| driver | Muse Spark 1.2 Contributor | `opencode-go/muse-spark-1.2-contributor` |
| worker | Muse Spark 1.2 Contributor | `opencode-go/muse-spark-1.2-contributor` |

Canonical configuration:

```yaml
delegation:
  brain:
    authority: codex
    labels: [sol, terra]

  roles:
    driver:
      backend: opencode
      model: opencode-go/muse-spark-1.2-contributor
      max_parallel: 3

    worker:
      backend: opencode
      model: opencode-go/muse-spark-1.2-contributor
      max_parallel: 12
```

### 31.1 Model selection is intentionally boring

`modelctl` should not spend reasoning budget ranking a large provider catalog on every task.

Normal routing is:

```text
coherent implementation with clear spec → Muse/driver
repetitive or parallel execution        → Muse/worker
judgment / architecture / integration   → Codex brain
```

A different OpenCode model is used only when configuration is explicitly changed.

### 31.2 Catalog synchronization

`modelctl delegates sync` still records the current OpenCode Go catalog so disappearance or renaming of Muse is detected. New provider models remain catalog-only and do not change normal orchestration automatically.

## 32. Delegation Catalog and Availability Ledger

Scope: the existing local machine/model inventory is extended with a separate cloud delegate catalog.

```text
local inventory:
    machine → artifact → vLLM target

delegate catalog:
    provider → model → bin → availability/privacy/cost/capability metadata
```

Recommended SQLite additions:

```sql
delegate_models(
  model_ref TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  bin TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  availability_status TEXT NOT NULL,
  metadata_json TEXT
);

delegate_model_policy(
  model_ref TEXT PRIMARY KEY,
  max_data_class TEXT NOT NULL,
  training_policy TEXT,
  retention_policy TEXT,
  tool_profile TEXT NOT NULL,
  max_parallel INTEGER,
  operator_notes TEXT
);

delegate_runs(
  run_id TEXT PRIMARY KEY,
  parent_trace_id TEXT,
  caller TEXT NOT NULL,
  requested_bin TEXT NOT NULL,
  selected_model_ref TEXT NOT NULL,
  task_class TEXT NOT NULL,
  workspace_mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  state TEXT NOT NULL,
  observed_cost REAL,
  validation_status TEXT,
  escalation_parent_run_id TEXT
);
```

Commands:

```bash
modelctl delegates sync
modelctl delegates list
modelctl delegates list --bin worker
modelctl delegates show opencode-go/<model-id>
modelctl delegates history
```

`delegates sync` runs `opencode models opencode-go --refresh --verbose`, optionally cross-checks the provider's `/zen/go/v1/models` endpoint, records exact provider/model IDs and timestamps, marks disappeared models unavailable rather than deleting history, and leaves newly discovered models unclassified/disabled.

For mutable policy metadata, store both `observed_at` and `valid_until` when the provider publishes an expiry. A privacy guarantee whose validity window has expired becomes `POLICY_STALE` and is ineligible for cloud routing until refreshed. This is important because provider agreements can be explicitly time-bounded.

---

## 33. Delegation Configuration

Scope: configuration describes brain authority and Muse role envelopes; it does not implement a multi-model tournament.

```yaml
delegation:
  enabled: true

  brain:
    owner: codex
    final_authority: true

  backend:
    type: opencode
    executable: opencode
    provider: opencode-go
    model: opencode-go/muse-spark-1.2-contributor

  roles:
    driver:
      max_parallel: 3
      max_files_read: 80
      max_files_write: 20
      max_patch_lines: 1500
      default_timeout_s: 1800
      workspace: isolated_worktree
      validation: strong

    worker:
      max_parallel: 12
      max_files_read: 20
      max_files_write: 5
      max_patch_lines: 300
      default_timeout_s: 600
      workspace: staged_or_worktree
      validation: deterministic

  orchestration:
    max_total_parallel: 12
    max_task_depth: 1
    worker_batch_size: 8
    brain_context_return_limit_tokens: 4000
    require_structured_results: true
    recursive_delegation: false

  execution:
    pure_mode: true
    auto_approve: false
    auto_update: false
    provider_allowlist: [opencode-go]

  validation:
    require_diff_capture: true
    reject_out_of_scope_changes: true
    run_declared_tests: true
```

The important tuning parameters are task granularity, parallelism, and return compression.

## 34. Brain-to-Delegate Task Graph

Scope: Codex turns a complex request into a dependency graph and delegates only nodes whose boundaries and acceptance tests are clear.

```text
User request
    │
    ▼
[Brain: understand + design]
    │
    ├── W1 search API call sites ───────────────┐
    ├── W2 inspect tests ───────────────────────┤
    ├── W3 enumerate config users ──────────────┤
    └── W4 summarize error paths ───────────────┤
                                                ▼
                                      [Brain: synthesize]
                                                │
                                                ▼
                                      D1 implement change
                                                │
                                  ┌─────────────┼─────────────┐
                                  ▼             ▼             ▼
                             W5 tests       W6 docs       W7 lint/fix
                                  └─────────────┼─────────────┘
                                                ▼
                                      [modelctl validate]
                                                │
                                                ▼
                                      [Brain: review/merge]
```

### 34.1 Worker node schema

```json
{
  "task_id": "W3",
  "role": "worker",
  "objective": "Find every config reader that consumes timeout_ms.",
  "inputs": {"paths": ["src/", "tests/"]},
  "constraints": [
    "Read only.",
    "Return file, symbol, and one-line use description."
  ],
  "deliverable": {"type": "json", "schema": "callsite-list-v1"}
}
```

### 34.2 Driver node schema

```json
{
  "task_id": "D1",
  "role": "driver",
  "objective": "Implement the already-approved timeout schema migration.",
  "depends_on": ["W1", "W2", "W3", "W4"],
  "inputs": {"brain_spec": ".modelctl/specs/change-42.md"},
  "allowed_write_paths": ["src/config/", "tests/config/"],
  "validation": [
    "pytest -q tests/config",
    "ruff check src/config tests/config"
  ],
  "deliverable": {"type": "candidate_patch"}
}
```

### 34.3 Delegation threshold

For a task \(t\), delegate when:

\[
\boxed{
C_{\mathrm{describe}}(t)+C_{\mathrm{verify}}(t)
<
C_{\mathrm{brain}}(t)
}
\]

where \(C_{\mathrm{describe}}\) is brain effort needed to specify the task, \(C_{\mathrm{verify}}\) is effort needed to check it, and \(C_{\mathrm{brain}}\) is expected effort if Codex performs it directly.

### 34.4 Fan-out

Independent worker nodes execute concurrently up to the worker limit. Tasks should be batched when setup/context overhead dominates execution.

## 35. Muse Role Profiles

Scope: the same Muse model is given different execution envelopes for driver and worker behavior.

### 35.1 `modelctl-worker-read`

For search, extraction, inspection, classification, and summaries.

```text
read/glob/grep/LSP: allowed
edit: denied
bash: denied unless an exact validation command is predeclared
recursive task delegation: denied
```

Expected output is structured JSON, not an essay.

### 35.2 `modelctl-worker-edit`

For narrow mechanical changes.

```text
workspace: isolated
write paths: explicit allowlist
patch-size cap: small
test commands: predefined
recursive task delegation: denied
```

### 35.3 `modelctl-driver`

For one coherent implementation package.

```text
workspace: isolated Git worktree
repository context: larger
write paths: bounded by brain contract
build/test commands: allowed by exact policy
patch-size cap: medium
recursive task delegation: denied
```

### 35.4 Effective policy

Every managed OpenCode run records the role profile, Muse model ID, base commit, task-contract hash, and validation-contract hash.

## 36. Workspace Isolation

Scope: write-capable delegates must never directly modify the caller's active working tree.

Default flow:

```text
main working tree
      │
      └── temporary Git worktree at known base commit
                  │
                  ├── opencode run
                  ├── capture diff
                  ├── enforce changed-path scope
                  ├── run validation
                  └── return candidate patch metadata to Codex
```

For read-only repetitive tasks, prefer a staging directory containing only declared files. This reduces both token cost and accidental cloud disclosure.

After a write-capable delegate:

\[
\boxed{\text{changed paths} \subseteq \text{allowed write paths}}
\]

Otherwise the result is rejected as `E_DELEGATE_OUT_OF_SCOPE_CHANGE`.

Canonicalized paths must be used so symlinks and `..` traversal cannot escape the allowed workspace.

### 36.1 Delegate process containment

Every `opencode run` execution is an owned local process tree with bounded lifetime.

Recommended lifecycle:

```text
spawn OpenCode in a new process group / local cgroup
record PID + start time + run ID
enforce task deadline
SIGTERM owned group on timeout/cancel
wait cleanup grace period
SIGKILL owned group if required
verify no owned child remains
capture/delete OpenCode session according to policy
clean temporary worktree/staging directory
finalize run state
```

A timeout is successful cleanup only after the owned process tree and temporary workspace are verified gone. Unknown/unowned local processes are never killed.

Suggested configuration:

```yaml
execution:
  run_timeout_s: 1800
  graceful_cancel_timeout_s: 10
  cleanup_timeout_s: 30
```

---

## 37. Role Routing Algorithm

Scope: routing chooses brain vs driver vs worker; it does not normally choose between OpenCode models.

For task node \(t\), define:

- \(J(t)\): judgment/ambiguity requirement;
- \(V(t)\): ease of independent verification;
- \(R(t)\): repetitiveness/parallelizability;
- \(S(t)\): scope size and coupling.

Initial deterministic policy:

```text
high J                         → brain
low J + large coherent S      → driver
low J + high R + high V       → worker
low J + tiny bounded S        → worker
worker blocked by scope       → brain may reframe as driver
driver discovers judgment gap → brain
```

Canonical implementation:

```python
def choose_role(task):
    if task.requires_judgment or task.is_architectural:
        return "brain"
    if task.is_repetitive or task.is_parallelizable:
        return "worker"
    if task.has_clear_spec and task.is_bounded_implementation:
        return "driver"
    return "brain"
```

### 37.1 No automatic model shopping

The default OpenCode model is Muse Spark for both executable roles. The optimization problem is task shaping and parallelism, not model selection.

## 38. Reduction, Validation, and Escalation

Scope: the system sends delegation back to the brain when execution stops being economical or reliable.

### 38.1 Worker result reduction

For \(n\) workers:

\[
\text{raw outputs}_{1:n}
\rightarrow
\text{normalize/deduplicate}
\rightarrow
\text{compact evidence packet}
\rightarrow
\text{brain}
\]

The brain should not receive twelve full transcripts when a compact normalized result is sufficient.

### 38.2 Driver acceptance

A driver patch is a candidate until:

```text
allowed-path check passes
AND patch-size policy passes
AND required tests/checks pass
AND owned delegate process is cleaned up
AND brain accepts the semantic change
```

### 38.3 Escalation

```text
worker blocked by missing context → reissue or convert to driver
worker returns conflicting facts  → another worker check or brain review
driver fails deterministic tests  → one bounded repair attempt
driver discovers design ambiguity → brain
repeated delegate failure         → brain performs/replans task
```

The brain is the escalation target.

## 39. Delegation Boundary

Scope: `modelctl` controls exactly what repository/task context a Muse delegate receives.

The brain constructs an explicit context package:

```text
task specification
+ declared repository files
+ required dependency summaries
+ validation contract
```

It does not automatically forward unrelated conversation or repository context.

### 39.1 Context minimization

Worker context should be aggressively small. Driver context may be larger but remains bounded by the task contract.

### 39.2 Event journal

Store control-plane metadata:

```text
task-contract hash
role
model
base commit
declared paths
result hash
validation result
usage/cost metadata
```

### 39.3 Session cleanup

After the required result is captured, `modelctl` cleans the owned OpenCode session and temporary workspace according to configured retention policy.

## 40. Budget, Quota, and Concurrency Governor

Scope: cheap delegation remains locally bounded even when the provider subscription has generous limits.

OpenCode Go currently documents plan limits of **$12 per 5 hours, $30 per week, and $60 per month**; request capacity varies by model cost. These limits are mutable and are not a substitute for local controls.

OpenCode also documents an optional **Use balance** setting that can fall back to Zen balance after Go limits are exhausted. For a predictable-cost `modelctl` deployment, this should be treated as disabled-by-policy. If the provider does not expose a machine-readable way to verify the setting, `modelctl doctor` must report the account-side state as `UNVERIFIED` rather than claiming a hard cloud-spend guarantee.

Reference:

- https://opencode.ai/docs/go/

Example:

```yaml
budget:
  max_parallel_total: 10
  max_parallel_worker: 8
  max_parallel_driver: 4
  max_parallel_frontier: 2
  max_task_fanout: 8
  max_retry_per_candidate: 1
  max_escalations: 2

  # Local operator spend guard.
  soft_daily_usd: 5
  hard_daily_usd: 10

  # Mirror the currently documented Go allowance windows.
  go_usage_value:
    hard_5h_usd: 12
    hard_weekly_usd: 30
    hard_monthly_usd: 60
```

At the soft limit, prefer cheaper eligible models and warn. At the hard limit, return `E_DELEGATION_BUDGET_EXCEEDED`; no automatic override.

Retry only clearly transient transport, rate-limit, and selected provider 5xx failures. Do not retry invalid requests, context overflow, policy denials, or failed tests as if they were transport failures.

A circuit breaker temporarily removes repeatedly failing models from routing.

---

## 41. Delegation Efficiency Benchmarking

Scope: benchmarking determines whether delegation actually saves Codex effort and wall-clock time.

Measure by task class:

```text
brain tokens/effort spent specifying task
brain tokens/effort spent reviewing result
delegate latency
delegate usage/cost
validation pass rate
accepted-result rate
rework rate
wall-clock speedup from parallelism
brain-token savings
```

The principal efficiency metric is:

\[
\boxed{
\eta_{\mathrm{delegate}}
=
\frac{
C_{\mathrm{brain\ baseline}}
-
(C_{\mathrm{describe}}+C_{\mathrm{review}})
}{
C_{\mathrm{brain\ baseline}}
}
}
\]

Positive \(\eta_{\mathrm{delegate}}\) means offloading saved brain work.

Benchmarking should determine:

- which task types belong in worker;
- which belong in driver;
- optimal worker batch size;
- optimal parallelism;
- when context packaging costs exceed delegation benefits.

Commands:

```bash
modelctl bench delegation run
modelctl bench delegation report
modelctl bench delegation task-types
```

## 42. Delegation CLI Surface

Scope: the CLI exposes task orchestration and inspection rather than provider-model shopping.

```text
modelctl
├── delegate
│   ├── run
│   ├── batch
│   ├── graph
│   ├── status
│   ├── cancel
│   ├── history
│   └── show
├── delegates
│   ├── sync
│   ├── doctor
│   └── broker
├── queue
│   ├── status
│   ├── drain
│   └── retry
├── budget
│   ├── status
│   └── history
└── bench
    └── delegation
```

Examples:

```bash
modelctl delegate run --role worker --task-file W3.json
modelctl delegate batch --role worker --tasks-dir .modelctl/tasks/search/
modelctl delegate run --role driver --task-file D1.json
modelctl delegate graph --file .modelctl/taskgraph.json
```

Both executable roles resolve to the configured Muse backend.

## 43. Codex Skill Offload Policy

Scope: this is the central behavior the `modelctl` skill teaches Codex.

For a substantial task:

```text
1. Understand the task.
2. Form the high-level plan.
3. Build a task graph.
4. Mark each node BRAIN, DRIVER, or WORKER.
5. Dispatch independent WORKER nodes in parallel.
6. Reduce worker results.
7. Dispatch DRIVER nodes after prerequisites are available.
8. Validate candidate outputs.
9. Bring compact evidence/results back into Codex.
10. Codex integrates, reasons, replans, and answers.
```

### 43.1 Default offload heuristic

Offload aggressively when work is:

```text
repetitive
search-heavy
enumerative
mechanical
easy to test
easy to diff
parallel
low-judgment
```

Keep work in Codex when it is:

```text
architectural
ambiguous
cross-cutting
judgment-heavy
hard to verify
final integration
```

### 43.2 Example

For:

```text
Refactor the authentication subsystem to support two token types.
```

Codex should behave like:

```text
BRAIN:
  understand current design
  choose new interface
  define invariants

WORKERS in parallel:
  enumerate token-validation call sites
  inspect tests
  enumerate configuration dependencies
  inspect error handling
  identify documentation references

BRAIN:
  synthesize evidence
  define exact implementation plan

DRIVER:
  produce candidate implementation in isolated worktree

WORKERS in parallel:
  add edge-case tests
  check old symbol references
  update docs
  inspect generated diff for unrelated changes

BRAIN:
  review tests/diff
  resolve hard issues
  integrate
  answer user
```

This is the target operating pattern.

### 43.3 Brain return budget

Delegates return compact artifacts:

```yaml
orchestration:
  worker_return_tokens: 800
  driver_return_tokens: 2000
  aggregate_return_tokens: 4000
```

Large logs/diffs remain in artifacts; Codex receives summaries and references unless expansion is needed.

## 44. Stable Delegation Result Envelope

Scope: Codex receives a stable `modelctl` result rather than depending directly on OpenCode's internal event schema.

```json
{
  "ok": true,
  "run_id": "dlg_01J...",
  "caller": "codex",
  "role": "worker",
  "selected_model": "opencode-go/<model-id>",
  "task_class": "generate_tests",
  "workspace": {
    "mode": "isolated_worktree",
    "base_commit": "4f0c...",
    "changed_paths": ["tests/test_duration.py"]
  },
  "validation": {
    "scope": "passed",
    "commands": [
      {
        "argv": ["pytest", "-q", "tests/test_duration.py"],
        "exit_code": 0
      }
    ],
    "status": "passed"
  },
  "usage": {
    "cost_usd": 0.0031,
    "latency_ms": 8120
  },
  "provenance": {
    "backend": "opencode-run",
    "model_ref": "opencode-go/<model-id>"
  }
}
```

The OpenCode JSON event format is adapter-specific. Only the `modelctl` envelope is part of the stable skill contract.

---

## 45. Delegation Error Taxonomy

Scope: failures from the OpenCode path receive stable, actionable codes.

```text
E_OPENCODE_NOT_FOUND
E_OPENCODE_VERSION_UNSUPPORTED
E_OPENCODE_AUTH_REQUIRED
E_DELEGATE_MODEL_UNAVAILABLE
E_DELEGATE_MODEL_UNCLASSIFIED
E_DELEGATE_PROVIDER_FAILURE
E_DELEGATE_RATE_LIMITED
E_DELEGATE_CONTEXT_TOO_LARGE
E_DELEGATION_POLICY_DENIED
E_DELEGATION_PRIVACY_DENIED
E_DELEGATION_PRIVACY_STALE
E_DELEGATION_BUDGET_EXCEEDED
E_OPENCODE_POLICY_DRIFT
E_DELEGATION_POLICY_LOCK_STALE
E_DELEGATION_DEPTH_EXCEEDED
E_DELEGATION_SCOPE_REQUIRED
E_DELEGATE_OUTPUT_MALFORMED
E_DELEGATE_OUT_OF_SCOPE_CHANGE
E_DELEGATE_VALIDATION_FAILED
E_DELEGATE_TIMEOUT
E_DELEGATE_CANCEL_FAILED
E_WORKTREE_CREATE_FAILED
E_WORKTREE_DIRTY_BASE
E_WORKTREE_CLEANUP_FAILED
```

A delegate returning text is not sufficient for `ok: true`; postconditions and validation determine success.

---

## 46. Delegation Release Tests

Scope: these are mandatory tests before automatic Codex offloading is enabled.

### 46.1 Routing

- requested bin is respected;
- unavailable/disabled/unclassified models are skipped;
- privacy-ineligible models are skipped;
- hard budget blocks execution;
- deterministic policy gives deterministic selection;
- `routing explain` matches actual routing.

### 46.2 Shell/workspace safety

- quotes/metacharacters in prompts cannot create additional commands;
- malicious filenames cannot escape staging;
- model IDs are validated against the observed catalog;
- agent profile comes from an allowlist;
- every managed invocation uses `--pure`;
- `--auto` is never emitted;
- effective permission policy cannot broaden beyond the selected profile;
- project/global plugin configuration cannot execute in a managed run;
- a stale policy lock blocks execution;
- out-of-scope patches are rejected;
- failed delegates do not modify the main worktree;
- cancel/timeout terminates only the owned OpenCode process tree;
- terminal cleanup verifies no owned child process remains;
- temporary worktree/staging cleanup is verified or a cleanup error is surfaced.

### 46.3 Privacy

- `SECRET` never routes to cloud;
- `CONFIDENTIAL` is local-only by default;
- unknown privacy metadata blocks cloud routing;
- expired privacy metadata blocks cloud routing;
- training-enabled models are excluded when policy forbids them;
- `.env` and key files are never staged implicitly;
- full prompt bodies are absent from the default event journal.

### 46.4 Escalation

- worker validation failure can escalate once to driver;
- driver can escalate to frontier when allowed;
- escalation maximum is enforced;
- workers cannot recursively spawn uncontrolled delegation;
- provider outage cannot trigger unbounded retries.

The delegation feature is production-ready only when Codex can request a bin without naming a concrete model, `modelctl` resolves an explicit observed model, the invocation uses the argv-safe `opencode run` adapter, write jobs are isolated, scope/tests are independently validated, cloud privacy policy is enforced, and every run records model/bin/validation/provenance.

---

## 47. Project Structure Additions

Scope: OpenCode delegation is a separate subsystem from vLLM lifecycle management.

```text
src/modelctl/
├── delegation/
│   ├── domain.py
│   ├── catalog.py
│   ├── router.py
│   ├── policy.py
│   ├── budget.py
│   ├── task_contract.py
│   ├── result_contract.py
│   ├── escalation.py
│   ├── validation.py
│   └── adapters/
│       └── opencode.py
├── workspace/
│   ├── staging.py
│   ├── worktree.py
│   ├── scope.py
│   └── cleanup.py
├── privacy/
│   ├── classification.py
│   ├── redaction.py
│   └── provider_policy.py
└── benchmark/
    ├── suite.py
    ├── runner.py
    └── report.py
```

The two backends intentionally have different lifecycle semantics:

```text
local vLLM:    deploy / start / stop / VRAM ownership
OpenCode Go:   select / invoke / validate / account
```

---


## 48. Brain-Aware Delegation Scheduler

Scope: the scheduler maximizes useful parallel work without flooding Codex with results or creating conflicting edits.

Rules:

1. Read-only workers may run broadly in parallel.
2. Workers writing disjoint files may run concurrently.
3. Workers touching overlapping paths are serialized or isolated and later merged by the brain.
4. Only a small number of drivers run concurrently.
5. Driver nodes wait for required worker evidence.
6. The scheduler applies backpressure when the brain has not consumed prior result batches.
7. Failed/blocked nodes return to the brain instead of recursively spawning more agents.

Recommended initial limits:

```yaml
scheduler:
  max_workers: 12
  max_drivers: 3
  max_pending_result_batches: 3
  overlap_policy: serialize
```

## 49. Delegation Doctor and Policy Lock

Scope: this adds a single preflight command that catches most integration failures before Codex begins a real task.

```bash
modelctl delegates doctor
```

It verifies:

```text
OpenCode executable/version
OpenCode Go authentication
Muse Spark delegate model is currently available
catalog freshness
provider allowlist
--pure capability
generated agent profiles
effective permission hashes
session-delete support
budget ledger
worktree/staging support
local secret scanner availability
provider/account spend-state observability
```

A successful doctor emits a hashed **delegation policy lock**:

```text
.modelctl/delegation.lock
```

containing:

```text
opencode_version
catalog_digest
brain_authority
driver_model_ref
worker_model_ref
agent_profile_digests
generated_config_digest
checked_at
```

Normal delegated runs compare against this lock. Drift is surfaced before execution rather than discovered halfway through a job.

---

## 50. Shadow Mode and Safe Calibration

Scope: shadow mode lets the system learn whether a cheap model is reliable without giving it production authority.

```bash
modelctl delegate run --bin worker --shadow ...
```

In shadow mode:

```text
Codex performs/owns the real task
delegate receives the same bounded contract independently
modelctl validates the delegate result
delegate output is not auto-integrated
metrics are recorded
```

This is the preferred way to calibrate a new model or promote a catalog-only model into an active bin.

---

## 51. Deterministic Task Cache

Scope: exact repetitive tasks can be reused without paying for an identical cloud delegation twice.

A cache key is:

\[
\boxed{
K =
H(
\text{task-contract version},
\text{base commit/tree hash},
\text{declared input-file hashes},
\text{model ref},
\text{agent-policy hash},
\text{validation spec}
)
}
\]

Cache entries are eligible only when all declared inputs and validation contracts match. Mutable tasks, external-web tasks, and time-sensitive tasks are not cached by default.

Commands:

```bash
modelctl delegate cache status
modelctl delegate cache prune
modelctl delegate run --no-cache ...
```

---

## 52. Optional Warm OpenCode Broker

Scope: high-volume worker jobs may reuse a local headless OpenCode server to reduce repeated startup/cold-boot overhead.

OpenCode currently supports:

```bash
opencode serve
opencode run --attach http://127.0.0.1:<port> ...
```

`modelctl` may expose:

```bash
modelctl delegates broker start
modelctl delegates broker status
modelctl delegates broker stop
```

Safety requirements:

- bind to `127.0.0.1`;
- use a generated password;
- run with the same pure/policy configuration;
- one policy lock per broker;
- do not reuse conversational sessions across independent tasks;
- restart broker when config/catalog/policy digest changes.

The broker is an optimization only. Direct `opencode run` remains the correctness baseline.

Authoritative OpenCode references:

- https://opencode.ai/docs/cli/
- https://opencode.ai/docs/server/

---

## 53. Delivery Plan Additions

Scope: OpenCode delegation is added only after the core ownership/state primitives exist.

### Phase 9 — OpenCode adapter and catalog

Implement OpenCode version/auth checks, `opencode models` sync, `opencode --pure run --format json` adapter, Muse Spark as the single production delegate model, and catalog-only handling for all other models.

### Phase 10 — task and workspace contracts

Implement task/result schemas, isolated worktrees, staged read-only contexts, path enforcement, owned process-group containment, verified cleanup, and explicit OpenCode agent profiles.

### Phase 11 — deterministic router

Implement deterministic brain/driver/worker role selection, Muse-backed driver/worker profiles, queue scheduling, backpressure, and the delegation policy lock.

### Phase 12 — validation and escalation

Implement diff capture, path-scope validation, tests/static checks, bounded retry, and `worker → driver → frontier` escalation.

### Phase 13 — Codex skill integration

Teach the skill to classify delegation eligibility, construct a bounded task contract, invoke `modelctl delegate`, inspect validation, and accept/reject/escalate.

### Phase 14 — calibration

Run shadow-mode delegation benchmarks on representative repositories. Measure brain-token savings, validation success, useful parallelism, and rework rate by task class; tune role thresholds and batch sizes rather than shopping for models.

### Phase 15 — performance optimization

Add deterministic task caching and the optional loopback-only warm OpenCode broker only after direct-run correctness, privacy, and cleanup tests pass.


## 54. North-Star Behavior

Scope: this section states the skill's intended behavior in one invariant.

\[
\boxed{
\text{Do not use an expensive brain token for work that a cheap Muse delegate can do and the system can cheaply verify.}
}
\]

The inverse is:

\[
\boxed{
\text{Do not delegate judgment merely because delegation is cheap.}
}
\]

`modelctl` is an execution amplifier for Codex, not an alternative autonomous coding brain.

## Limitations

Scope: these are the assumptions and idealizations on which the specification depends.

- The design assumes remote Linux machines reachable over SSH and a usable remote process supervisor; the preferred `systemd --user` path is not portable to every OS or container environment.
- It assumes model weights already exist on remote machines. Artifact downloading, replication, synchronization, and provenance distribution are outside v1.
- It assumes a trusted single-user or small-team fleet. Multi-tenant RBAC, quotas, scheduler fairness, and hostile-user isolation require a stronger control plane.
- It assumes one serving machine per deployment in v1. Multi-node vLLM requires additional rendezvous, topology, port, worker, and failure semantics.
- GPU process inspection can prove that modelctl-owned CUDA processes disappeared, but total GPU memory may remain nonzero because unrelated system/graphics/compute processes can legitimately use VRAM.
- No software wrapper can guarantee the GPU driver itself will always release resources correctly after catastrophic driver/kernel faults. The specification therefore uses `LEAK_SUSPECTED` and fail-closed reservations instead of claiming impossible certainty.
- vLLM CLI and runtime behavior can change between releases. The design depends on version detection and a tested capability matrix rather than permanently assuming current flags.
- A static preflight cannot prove that every model will fit in memory under every runtime condition. The final fit test remains actual vLLM initialization; `modelctl` can make the failure bounded, classified, and cleanly recovered.
- Session/TTL leases prevent forgotten managed processes only when the remote lease mechanism itself is running correctly. They do not replace explicit operational monitoring on critical infrastructure.
- The no-leakage claims depend on loopback binding, SSH transport, private file permissions, explicit request/output logging controls, secret redaction, and the absence of separately configured external proxies or logging layers that capture inference content.
- OpenCode Go is an external/cloud execution path. Any claim that prompts or source remain entirely local applies only to the local vLLM backend; cloud delegation necessarily transmits the selected task context to the configured provider.
- The `frontier`/`driver`/`worker` assignments are bootstrap policy, not independently established rankings; they depend on current provider availability and must be calibrated on representative work.
- Provider model availability, cost, quota, retention, and training policies can change. Safe routing depends on refreshed metadata and fail-closed handling of unknown privacy state.
- Workspace isolation prevents direct modification of the main checkout but does not itself prevent disclosure of files visible inside the delegated workspace; privacy depends on data classification, scope/staging, and secret filtering.
- Delegated outputs remain untrusted candidates. `modelctl` can constrain context/tools and validate observable postconditions, but cannot guarantee that a remote model follows instructions internally.
- Cost accounting depends on the usage metadata exposed by OpenCode/provider APIs; concurrency and request limits remain necessary when exact monetary accounting is unavailable.
- OpenCode repository instructions such as `AGENTS.md` can intentionally influence delegated behavior. Strict staging can exclude undeclared instruction/config files, but doing so may also remove legitimate repository guidance; this is a policy trade-off.
- OpenCode's optional account-side Zen balance fallback can consume paid balance after Go limits. Unless its state becomes machine-verifiable, `modelctl` cannot provide an absolute provider-side spend cap; it can enforce its own request/budget ledger and report the account setting as unverified.
- A warm OpenCode broker improves startup overhead but introduces a longer-lived local process and configuration state. It is therefore optional and must restart on policy/config digest changes.
