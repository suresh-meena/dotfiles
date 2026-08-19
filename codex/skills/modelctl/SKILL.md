---
name: modelctl
description: Deterministic model control for remote vLLM (SSH + systemd) and Codex→Muse delegated execution via opencode run. Use when starting/inspecting/connecting to a local model, checking which machine has a model, or dispatching bounded driver/worker tasks to Muse Spark instead of spending brain tokens.
---

# modelctl — Model Control & Delegation Skill

Use `modelctl` as the deterministic control plane for two backends: local/private vLLM on remote GPU machines (SSH + systemd --user) and delegated OpenCode/Muse execution for Codex.

## When to use

- Run, stop, or inspect a configured local model (`qwen-72b@gpu-a`).
- Discover which machine has a model artifact (`inventory list`).
- Get a loopback endpoint via SSH tunnel (`connect`/`endpoint`).
- Delegate repetitive or bounded coding work to `muse-spark-1.2-contributor` via `modelctl delegate` instead of spending brain tokens.
- Inspect delegation routing, budget, or validation (`delegates`, `budget`, `queue`).

## Never do

- Invent a machine alias or model alias — always resolve via `inventory list --json`, `targets list --json`, or `machines list --json`.
- Invent SSH hostnames, model paths, GPU indices, ports, or vLLM flags. Only schema-approved overrides are allowed; free-form vLLM flags are forbidden.
- Construct `ssh` or `vllm serve` commands yourself — invoke only `modelctl --json --non-interactive`.
- Kill foreign GPU processes, re-route failed `exec`/`submit`, or automatically `ssh` around an `E_GPU_BUSY_FOREIGN`/`E_LEAK_SUSPECTED` refusal.
- Log or echo prompts, completions, or secrets. Request/output bodies are never logged by `modelctl`; pass secrets only via env/credential refs.
- Modify config unless explicitly asked. Prefer `config validate`/`config resolve` for inspection.

## Decision flow

```
User intent
  ├── exact model + machine known?
  │     ├── yes → resolve target (config resolve --target M@MACHINE --json)
  │     └── no  → query inventory/targets (inventory list --json / targets list --json)
  ├── mutation requested? (start/stop/restart/connect/delegate run)
  │     ├── yes → require ONE exact target (model@machine) and call --json --non-interactive
  │     └── no  → safe discovery/status (status/ps/logs/events/doctor)
  └── delegation? → classify brain/driver/worker, then `modelctl delegate run`
```

If two machines have `qwen-72b` AVAILABLE, do not randomly choose. Ask or require ` --machine`.

## vLLM lifecycle (fail-closed)

```bash
# discover
modelctl inventory list --json
modelctl inventory sync --machine gpu-a --json
modelctl targets list --json

# validate before mutating
modelctl config validate --json
modelctl config resolve --target qwen-72b@gpu-a --json

# start is idempotent for same digest; --replace needed for new digest
modelctl start qwen-72b --machine gpu-a --json --non-interactive
modelctl status qwen-72b --machine gpu-a --json
modelctl connect qwen-72b --machine gpu-a --json --non-interactive
# → { local: "http://127.0.0.1:<port>/v1" }

# stop succeeds only after: supervisor inactive + cgroup empty + no owned GPU process + reservation released
modelctl stop qwen-72b --machine gpu-a --json --non-interactive
modelctl reconcile --machine gpu-a --json
modelctl doctor --target qwen-72b@gpu-a --json
```

Postconditions: `READY` requires supervisor active + /health 200 + /v1/models identity + digest + fingerprint. `LEAK_SUSPECTED` retains GPU reservation and blocks replacement — run `doctor`.

Network: remote vLLM binds `127.0.0.1` by default; `allow_remote_exposure: true` requires `network_policy`. `enable-log-requests/outputs: false` by default.

## Delegation (Codex → Muse)

Both `driver` and `worker` resolve to `opencode-go/muse-spark-1.2-contributor`; they differ by prompt/constraints/tool-profile/write-scope/validation. The brain (Codex) plans; `modelctl` dispatches; Muse executes; `modelctl` validates; Codex decides.

```
Brain: design, judgment, integration
Driver: one coherent implementation unit (isolated worktree, bounded writes, tests)
Worker: small parallel tasks (search/inspect/enumerate, read-only or narrow writes)
```

Invocation is always argv-safe:

```bash
opencode --pure run --model opencode-go/muse-spark-1.2-contributor --agent <profile> --format json --dir <isolated-workspace> "<prompt>"
```

No shell interpolation of agent text. `--pure` required, `--auto` never. `changed_paths ⊆ allowed_write_paths` (canonicalized, symlink-safe) or `E_DELEGATE_OUT_OF_SCOPE_CHANGE`.

```bash
# catalog & routing
modelctl delegates sync --json
modelctl delegates list --bin worker --json
modelctl delegates doctor --json  # emits .modelctl/delegation.lock

# single task
modelctl delegate run --role worker --task-file .modelctl/tasks/W3.json --json
modelctl delegate run --role driver --task-file .modelctl/tasks/D1.json --json

# batch & graph
modelctl delegate batch --role worker --tasks-dir .modelctl/tasks/search/ --json
modelctl delegate graph --file .modelctl/taskgraph.json --json
modelctl delegate status --run-id dlg_... --json
modelctl delegate cancel dlg_... --json
modelctl budget status --json
```

Task contract (example `W3.json`):

```json
{
  "task_id": "W3",
  "role": "worker",
  "objective": "Find every config reader that consumes timeout_ms.",
  "inputs": {"paths": ["src/", "tests/"]},
  "constraints": ["Read only.", "Return file, symbol, one-line use."],
  "deliverable": {"type": "json", "schema": "callsite-list-v1"},
  "allowed_write_paths": [],
  "agent_profile": "modelctl-worker-read",
  "validation": []
}
```

Driver adds `allowed_write_paths`, `validation` (exact argv, e.g., `["pytest","-q","tests/config"]`), `timeout_s`.

### Routing heuristic

```
high judgment/ambiguity → brain
low judgment + repetitive/parallel + high verify → worker
low judgment + large coherent bounded spec → driver
worker blocked → brain reframes as driver; driver ambiguity → brain
```

Do not shop models. Default is Muse for both bins; catalog-only models stay disabled.

### Workspace & containment

- Read-only workers: staged dir with declared files only.
- Write-capable: isolated Git worktree at base commit; diff captured; path scope enforced; tests run; patch-size capped.
- Process-group containment: `run` is owned local process tree with deadline → SIGTERM → SIGKILL → verified cleanup. Temporary worktree/staging verified deleted or error surfaced.

### Budget & privacy

- Local ledger `soft_daily_usd`/`hard_daily_usd` (Go limits $12/5h, $30/wk, $60/mo are mutable). At hard limit → `E_DELEGATION_BUDGET_EXCEEDED`.
- `SECRET` never to cloud; `CONFIDENTIAL` local-only by default; unknown/expired privacy metadata → `E_DELEGATION_PRIVACY_DENIED`/`STALE`.
- `.env`/`*.pem` never staged implicitly; prompts/completions absent from event DB.

### Escalation

```
worker validation fail → one bounded driver repair
driver ambiguity → frontier (brain)
max_escalations: 2, max_retry_per_candidate: 1
workers cannot recursively delegate
```

### Validation envelope (stable)

`modelctl delegate run --json` returns:

```json
{
  "ok": true,
  "run_id": "dlg_...",
  "role": "worker",
  "selected_model": "opencode-go/muse-spark-1.2-contributor",
  "workspace": {"mode": "isolated_worktree", "changed_paths": ["..."]},
  "validation": {"scope": "passed", "status": "passed"},
  "usage": {"cost_usd": 0.003, "latency_ms": 8120},
  "provenance": {"backend": "opencode-run", "model_ref": "opencode-go/muse-spark-1.2-contributor"}
}
```

`ok: true` requires scope + patch-size + tests + cleanup + brain acceptance. Do not treat raw delegate text as success.

## Error handling

All failures are `{ok:false, code:"E_...", message, retryable, suggested_action}`. Never print raw traceback without `--debug`; secrets always redacted. Common codes:

- `E_TARGET_NOT_FOUND` → `targets list`
- `E_ARTIFACT_MISSING/STALE` → `inventory sync --machine`
- `E_GPU_BUSY_FOREIGN` → `gpu status --machine` (never kill foreign)
- `E_LEAK_SUSPECTED` → `doctor --target`
- `E_DELEGATION_BUDGET_EXCEEDED` → `budget status`
- `E_DELEGATE_OUT_OF_SCOPE_CHANGE` → fix `allowed_write_paths`
- `E_DELEGATION_POLICY_LOCK_STALE` → `delegates doctor`

Prefer `connect`/`endpoint` over remote exposure. `gc` never deletes model weights.

## Examples

```
User: Use qwen-72b.
Skill: modelctl inventory list --json  # finds qwen-72b@gpu-a and @gpu-b → ask which machine

User: Use qwen-72b on gpu-a.
Skill: modelctl start qwen-72b --machine gpu-a --json --non-interactive
       modelctl connect qwen-72b --machine gpu-a --json --non-interactive

User: Refactor auth to support two token types.
Skill:
  BRAIN: design interface/invariants
  WORKERS (parallel): enumerate call sites, inspect tests, enumerate config users, error paths
  BRAIN: synthesize → implementation plan
  DRIVER: isolated worktree patch
  WORKERS (parallel): tests, docs, lint, diff inspection
  BRAIN: review/merge
```

For delegation, construct a minimal context package (task spec + declared files + validation contract), not the whole conversation.
