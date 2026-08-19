# modelctl — Model Control & Delegation Plane

Deterministic control plane for:

- **Local/private inference** — lifecycle for machine-specific vLLM deployments over SSH + `systemd --user`.
- **Delegated coding** — bounded `opencode run` jobs to `opencode-go/deepseek-v4-flash` (driver) and `opencode-go/hy3` (worker) via role bins (`brain`/`driver`/`worker`), with max-thinking reasoning by default, plus privacy, budget, and validation gates.

```
target = (model, machine, artifact, runtime profile)
deployment_id = target_id + ":" + config_digest + ":" + launch_nonce
```

Primary invariant: **fail-closed with explicit postconditions**. Success is reported only after verified healthy state; ambiguous state never becomes silent success.

## Local simulation mode

When a target's machine has no real vLLM runtime, `start` launches the bundled
fake runtime (`python -m modelctl.runtime.fake`) as a **real subprocess** that
serves actual `/health` and `/v1/models` HTTP endpoints. All lifecycle
verification is real code, not a sleep:

- `start` spawns the process (session leader, so `pid == pgid`), binds the GPU
  reservation to the spawned pid, and only reports `READY` after the health and
  model-identity checks pass within `startup_timeout_s`.
- `stop` sends `SIGTERM` to the process group, escalates to `SIGKILL`, and only
  reports `STOPPED` after the group is proven gone **and** the port is free.
  Failure to verify leaves the deployment `LEAK_SUSPECTED` with its reservation
  retained.
- `--no-wait` leaves the deployment `STARTING` with its pid recorded; a later
  `start --wait` (same digest) finishes the pending start instead of erroring.
- `--replace` really terminates the previous deployment before starting the new
  one; a same-digest re-`start` is idempotent and extends the lease when `--ttl`
  is given.

Every lifecycle payload carries `"simulation": true` / `"backend":
"local-simulation"` so consumers can distinguish simulated from production
results. `modelctl logs` reads the runtime's real log file.

## Install

```bash
# editable install
pip install -e ./modelctl

# verify
modelctl --help
modelctl doctor
modelctl config validate
```

The agent skill lives at `codex/skills/modelctl/SKILL.md` (canonical; `modelctl/SKILL.md` is a symlink to it). Symlink it into your agent's skill directory:

```bash
# for Codex
mkdir -p ~/.codex/skills
ln -sf "$(pwd)/codex/skills/modelctl" ~/.codex/skills/modelctl
# also for OpenCode
mkdir -p ~/.config/opencode/skills
ln -sf "$(pwd)/codex/skills/modelctl" ~/.config/opencode/skills/modelctl
```

## Quick start

```bash
# inspect inventory
modelctl inventory list --json
modelctl inventory sync --machine gpu-a

# start / status / connect / stop (fail-closed)
modelctl start qwen-72b --machine gpu-a --json
modelctl start qwen-72b --machine gpu-a --ttl 2h   # leased deployment
modelctl status qwen-72b --machine gpu-a
modelctl connect qwen-72b --machine gpu-a
modelctl stop qwen-72b --machine gpu-a

# delegation (Codex → driver/worker models) — honest: fails E_OPENCODE_NOT_FOUND if opencode is absent
modelctl delegates sync
modelctl delegates list --bin worker
modelctl delegate run --role worker --task-file .modelctl/tasks/W3.json --json
```

## Config

`~/.config/modelctl/config.yaml` (user) and `./modelctl.yaml` (project override, optional):

```yaml
version: 1
defaults:
  startup_timeout_s: 1200
  bind_host: 127.0.0.1
  security:
    allow_remote_exposure: false
machines:
  gpu-a:
    ssh: { host: gpu-a.example.internal, user: ink }
    supervisor: systemd-user
    inventory: { roots: [/models] }
    runtime: { type: venv, activate: /opt/vllm/bin/activate }
    gpu: { sharing: exclusive-managed }
models:
  qwen-72b: { served_model_name: qwen-72b }
targets:
  qwen-72b@gpu-a:
    model: qwen-72b
    machine: gpu-a
    artifact: { path: /models/Qwen2.5-72B-Instruct, require_observed: true }
    gpus: [0,1,2,3]
    vllm: { tensor_parallel_size: 4, max_model_len: 32768, gpu_memory_utilization: 0.92 }
delegation:
  enabled: true
  backend: { type: opencode, executable: opencode, provider: opencode-go }
  roles:
    driver: { model: opencode-go/deepseek-v4-flash, max_parallel: 3, workspace: isolated_worktree }
    worker: { model: opencode-go/hy3, max_parallel: 12, workspace: staged_or_worktree }
```

Strict schema: unknown keys, duplicate aliases, non-absolute artifact paths, TP > GPU count, non-loopback bind without `allow_remote_exposure`, negative timeouts, and literal secrets are rejected.

`modelctl config resolve --target qwen-72b@gpu-a --json` prints canonical resolved target and `config_digest = SHA256(canonical_json)`. The serving port comes from `defaults.port` (or `machines.<alias>.defaults.port`), not the `vllm` block.

## Safety invariants

- **Ownership** — only the recorded `server_pid` (a session leader spawned by
  `start`) may be killed; termination kills the whole process group and the
  port must be observed free before `STOPPED` is reported.
- **GPU reservation** — atomic `flock` per GPU UUID (sorted lexicographically)
  with ownership metadata `{deployment_id, pid, reserved_at}`; a reservation is
  live while its owner pid is alive, stale when the pid is dead, and
  `release_for_owner` never touches another deployment's reservation.
- **Stop postcondition** — success requires process group gone + port free +
  reservation released + metadata finalized; otherwise `LEAK_SUSPECTED`
  (reservation retained, `--force` re-attempts termination but never skips
  verification).
- **Leases** — `start --ttl 2h` records `lease_expires_at`; `status` reports
  `LEASE: expired` and `reconcile --fix-safe` auto-stops expired leases.
- **Inventory** — local simulation hosts are scanned on the filesystem; real
  hosts are scanned through SSH with roots sent over stdin (never on the
  command line) and the scan snippet carries no shell metacharacters.
- **Network** — remote vLLM binds `127.0.0.1:` by default; SSH tunnel is the only published endpoint. `allow_remote_exposure: true` requires explicit network policy.
- **Privacy** — `enable-log-requests: false`, `enable-log-outputs: false` by default; no prompt/completion/secret in event DB or JSON output.

## Delegation

Both `driver` and `worker` are dispatched via `opencode --pure run`; the driver role uses `opencode-go/deepseek-v4-flash` and the worker role uses `opencode-go/hy3`. They differ by prompt, context, tool profile, write scope, and validation depth. Invocation is `opencode --pure run --model <ref> --agent <profile> --format json --dir <isolated-workspace> "<prompt>"` — argv-safe, never shell-interpolated.

Maximum reasoning (`--variant max`) is the default for every delegated run; override by passing `variant=None` in the adapter or a `--variant` extra arg. Delegation is honest: the executable availability check runs **before** any run
record is created, so a missing `opencode` yields `E_OPENCODE_NOT_FOUND` with no
fabricated `RUNNING`/`SUCCEEDED` row. Costs are recorded as `0.0` with
`cost_estimated: true` when the adapter does not report tokens. The warm broker
(`delegates broker start`) is not implemented and refuses with
`E_DELEGATION_POLICY_DENIED` instead of faking a running broker.

`modelctl delegates doctor` emits `.modelctl/delegation.lock` (opencode version, catalog digest, profile digests). Stale lock blocks execution.

Isolated Git worktree is default for write-capable delegates; `changed_paths ⊆ allowed_write_paths` is enforced (canonicalized, symlink-safe). Process-group containment + verified cleanup on timeout/cancel.

## CLI

All agent-facing commands support `--json --non-interactive --trace-id`.

```
modelctl start|stop|restart|status|ps|connect|disconnect|endpoint
modelctl inventory sync|list|show|history|diff|stale
modelctl machines list|show|probe
modelctl models list|show
modelctl targets list|show
modelctl gpu status|reservations|reconcile
modelctl logs|events|reconcile|gc|doctor|config validate|resolve|explain|version
modelctl delegate run|batch|graph|status|cancel|history|show
modelctl delegates sync|list|show|history|doctor|broker
modelctl queue status|drain|retry
modelctl budget status|history
modelctl bench delegation run|report
```

Errors are stable: `{"ok":false,"code":"E_GPU_BUSY_FOREIGN","retryable":false,"suggested_action":"..."}`; no traceback without `--debug`.

## Layout

See `modelctl_specification_v1.5.md` for the full spec. Implementation follows §22 and §47 project structure.

## License

GPL-3.0
