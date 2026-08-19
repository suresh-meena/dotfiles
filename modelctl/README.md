# modelctl — Model Control & Delegation Plane

Deterministic control plane for:

- **Local/private inference** — lifecycle for machine-specific vLLM deployments over SSH + `systemd --user`.
- **Delegated coding** — bounded `opencode run` jobs to `opencode-go/muse-spark-1.2-contributor` via role bins (`brain`/`driver`/`worker`), with privacy, budget, and validation gates.

```
target = (model, machine, artifact, runtime profile)
deployment_id = target_id + ":" + config_digest + ":" + launch_nonce
```

Primary invariant: **fail-closed with explicit postconditions**. Success is reported only after verified healthy state; ambiguous state never becomes silent success.

## Install

```bash
# editable install
pip install -e ./modelctl

# verify
modelctl --help
modelctl doctor
modelctl config validate
```

Codex skill is at `codex/skills/modelctl/SKILL.md` (and `modelctl/SKILL.md`). Symlink for Codex:

```bash
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
modelctl status qwen-72b --machine gpu-a
modelctl connect qwen-72b --machine gpu-a
modelctl stop qwen-72b --machine gpu-a

# delegation (Codex → Muse)
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
  backend: { type: opencode, executable: opencode, provider: opencode-go, model: opencode-go/muse-spark-1.2-contributor }
  roles:
    driver: { max_parallel: 3, workspace: isolated_worktree }
    worker: { max_parallel: 12, workspace: staged_or_worktree }
```

Strict schema: unknown keys, duplicate aliases, non-absolute artifact paths, TP > GPU count, non-loopback bind without `allow_remote_exposure`, negative timeouts, and literal secrets are rejected.

`modelctl config resolve --target qwen-72b@gpu-a --json` prints canonical resolved target and `config_digest = SHA256(canonical_json)`.

## Safety invariants

- **Ownership** — only `systemd --user` + cgroup + deployment metadata + PID start-time may prove kill eligibility; bare PID is insufficient.
- **GPU reservation** — atomic file locks per GPU UUID (sorted lexicographically) + `E_GPU_BUSY_FOREIGN` refusal (never kill foreign).
- **Stop postcondition** — success requires: supervisor inactive + cgroup empty + no owned GPU compute process + reservation released + metadata finalized. Otherwise `LEAK_SUSPECTED`.
- **Network** — remote vLLM binds `127.0.0.1:` by default; SSH tunnel is the only published endpoint. `allow_remote_exposure: true` requires explicit network policy.
- **Privacy** — `enable-log-requests: false`, `enable-log-outputs: false` by default; no prompt/completion/secret in event DB or JSON output.

## Delegation

Both `driver` and `worker` resolve to `opencode-go/muse-spark-1.2-contributor`; they differ by prompt, context, tool profile, write scope, and validation depth. Invocation is `opencode --pure run --model <ref> --agent <profile> --format json --dir <isolated-workspace> "<prompt>"` — argv-safe, never shell-interpolated.

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
