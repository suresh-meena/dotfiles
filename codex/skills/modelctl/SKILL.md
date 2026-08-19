---
name: modelctl
description: Deterministic model control for remote vLLM (SSH + systemd) and delegated opencode execution via opencode run (driver → deepseek-v4-flash, worker → hy3, max reasoning by default). Use when starting/inspecting/connecting to a local model, checking which machine has a model, or dispatching bounded driver/worker tasks as subagents instead of spending brain tokens. If a delegated run fails, do the work yourself rather than stopping.
---

# modelctl — Model Control & Delegation Skill

`modelctl` is the deterministic control plane for two backends: local/private vLLM on remote GPU machines (SSH + systemd --user) and delegated OpenCode execution. It is **fail-closed**: success is only reported after verified postconditions, and refusals carry stable error codes.

Always call `modelctl --json --non-interactive` so results are machine-readable and no prompt is spawned.

## When to use

- Run, stop, or inspect a configured local model (`qwen-72b@gpu-a`).
- Discover which machine holds a model artifact (`inventory list`).
- Get a loopback endpoint via SSH tunnel (`connect`/`endpoint`).
- Delegate a bounded unit of coding work (`delegate run`) instead of spending your own context/tokens.
- Inspect routing, budget, validation, or failure history (`delegates`, `budget`, `queue`, `delegate status`).

## Never do (safety invariants)

- Invent a machine/model alias — always resolve via `inventory list --json`, `targets list --json`, or `machines list --json`.
- Invent SSH hostnames, model paths, GPU indices, ports, or free-form vLLM flags. Only schema-approved config is valid.
- Construct `ssh` or `vllm serve` yourself — invoke only `modelctl --json --non-interactive`.
- Kill foreign GPU processes, force-past a `E_GPU_BUSY_FOREIGN`/`E_LEAK_SUSPECTED` refusal, or tunnel around a safety error.
- Log or echo prompts, completions, or secrets. Pass secrets only via env/credential refs.
- Modify config unless explicitly asked. Prefer `config validate` / `config resolve` for inspection.
- Treat raw delegate text as success — only the validation envelope counts.

## Fallback policy: if modelctl fails, do it yourself

`modelctl` is a guardrail, **not a gate**. When a call fails, classify the error:

**Do the work yourself (in your own session) instead of stopping:**
`E_OPENCODE_NOT_FOUND`, `E_DELEGATE_MODEL_UNAVAILABLE`, `E_DELEGATION_BUDGET_EXCEEDED` (do it locally, spend nothing), `E_DELEGATION_POLICY_DENIED`, `E_DELEGATE_TIMEOUT`, `E_DELEGATE_PROVIDER_FAILURE`, `E_DELEGATE_OUT_OF_SCOPE_CHANGE`, `E_DELEGATE_VALIDATION_FAILED`, `E_WORKTREE_*`.
These are *execution* failures — the delegated path couldn't run, so complete the task directly with your own tools, keeping the same scope/validation constraints.

**Do NOT work around — surface and follow `suggested_action`:**
`E_GPU_BUSY_FOREIGN`, `E_LEAK_SUSPECTED`, `E_PORT_BUSY`, `E_SSH_UNREACHABLE`, `E_ARTIFACT_MISSING`/`E_ARTIFACT_STALE`, `E_DELEGATION_PRIVACY_DENIED`, `E_DELEGATION_BUDGET_EXCEEDED` (hard daily limit).
These are *environmental/safety* refusals. Resolving them means real state changes (reconcile, sync, doctor) or a user decision — never improvise around them.

**Retry discipline before falling back:**
1. Transient failures (`E_DELEGATE_TIMEOUT`, `E_DELEGATE_PROVIDER_FAILURE`): retry once via `modelctl queue retry <run_id> --json`.
2. Still failing → complete the task yourself.
3. Report in your summary: what modelctl refused, why, and what you did instead.

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

If two machines have `qwen-72b` AVAILABLE, do not randomly choose. Ask or require `--machine`.

## Discovery & validation

```bash
modelctl inventory list --json
modelctl inventory sync --machine gpu-a --json     # re-probe artifact availability
modelctl targets list --json
modelctl machines list --json
modelctl machines probe --machine gpu-a --json
modelctl config validate --json
modelctl config resolve --target qwen-72b@gpu-a --json
modelctl version --json
```

## vLLM lifecycle (fail-closed)

```bash
# start is idempotent for the same config digest; --replace needed for a new digest
modelctl start qwen-72b --machine gpu-a --json --non-interactive
modelctl start qwen-72b --machine gpu-a --ttl 2h --json --non-interactive   # leased deployment
modelctl status qwen-72b --machine gpu-a --json
modelctl ps --machine gpu-a --json
modelctl connect qwen-72b --machine gpu-a --json --non-interactive
modelctl endpoint qwen-72b --machine gpu-a --json
modelctl logs qwen-72b --machine gpu-a --json

# stop only reports STOPPED after the process group is gone AND the port is free
modelctl stop qwen-72b --machine gpu-a --json --non-interactive
modelctl reconcile --machine gpu-a --json
modelctl doctor --target qwen-72b@gpu-a --json
modelctl gpu reservations --json
modelctl gpu reconcile --json
```

Postconditions: `READY` requires a live server process + `/health` 200 + `/v1/models` identity + digest. `LEAK_SUSPECTED` retains the GPU reservation and blocks replacement — run `doctor` (never bypass). Local-simulation payloads carry `"simulation": true` / `"backend": "local-simulation"`.

Network: remote vLLM binds `127.0.0.1` by default; `allow_remote_exposure: true` requires `network_policy`. `enable-log-requests/outputs: false` by default.

## Delegation: treat delegate runs as subagents

`modelctl delegate run` is exactly a **subagent spawn**: you (brain) dispatch a bounded unit of work, then review the returned validation envelope like you would a subagent's report. Rules that follow:

- **Complete task file = complete subagent prompt.** Write a self-contained `task_file` with objective, inputs, constraints, exact deliverable, `allowed_write_paths`, and `validation` commands. A vague task gets a bad result — same as a vague subagent prompt.
- **Supervise, don't trust.** `ok: true` requires scope + patch-size + tests + cleanup. When uncertain, re-run the declared `validation` yourself in the repo. Never merge blind.
- **One task = one unit.** Don't pack multiple unrelated changes into one delegate run; split into driver (coherent implementation) + workers (parallel search/inspect/test).
- **Respect the boundary.** Workers/drivers may only write inside `allowed_write_paths`. If a run returns `E_DELEGATE_OUT_OF_SCOPE_CHANGE`, fix the scope and re-dispatch or do it yourself.

```
Brain (you): design, judgment, integration, review/merge
Driver:      one coherent implementation unit → opencode-go/deepseek-v4-flash
Worker:      small parallel tasks (search/inspect/enumerate) → opencode-go/hy3
```

Invocation is always argv-safe, and **maximum reasoning is the default** (`--variant max`):

```bash
opencode --pure run --model <ref> --agent <profile> --format json --dir <isolated-workspace> --variant max "<prompt>"
```

`--pure` required, `--auto` never. `changed_paths ⊆ allowed_write_paths` (canonicalized, symlink-safe) or `E_DELEGATE_OUT_OF_SCOPE_CHANGE`.

### Commands

```bash
# catalog & routing
modelctl delegates sync --json
modelctl delegates list --bin worker --json
modelctl delegates doctor --json                      # emits .modelctl/delegation.lock
modelctl delegate status --json                       # recent runs
modelctl delegate status --run-id dlg_... --json
modelctl delegate history --limit 20 --json

# dispatch
modelctl delegate run --role worker --task-file .modelctl/tasks/W3.json --json
modelctl delegate run --role driver --task-file .modelctl/tasks/D1.json --json
modelctl delegate batch --role worker --tasks-dir .modelctl/tasks/search/ --json
modelctl delegate graph --file .modelctl/taskgraph.json --json

# lifecycle of runs
modelctl delegate cancel dlg_... --json
modelctl queue retry dlg_... --json                    # re-runs a FAILED run (manual retry)
modelctl queue status --json

# budget
modelctl budget status --json
modelctl budget history --json
```

### Task contract (example `W3.json`)

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
high judgment/ambiguity → brain (you)
low judgment + repetitive/parallel + high verify → worker
low judgment + large coherent bounded spec → driver
worker blocked → brain reframes as driver; driver ambiguity → brain
```

Do not shop models. Driver defaults to `opencode-go/deepseek-v4-flash`, worker to `opencode-go/hy3`; catalog-only models stay disabled.

### Workspace & containment

- Read-only workers: staged dir with declared files only.
- Write-capable: isolated Git worktree at base commit; diff captured; path scope enforced; tests run; patch-size capped.
- Process-group containment: `run` is owned local process tree with deadline → SIGTERM → SIGKILL → verified cleanup. Temporary worktree/staging verified deleted or error surfaced.

### Escalation

```
worker validation fail → one bounded driver repair
driver ambiguity → brain (you)
max_escalations: 2, max_retry_per_candidate: 1
workers cannot recursively delegate
```

### Validation envelope (stable)

```json
{
  "ok": true,
  "run_id": "dlg_...",
  "role": "driver",
  "selected_model": "opencode-go/deepseek-v4-flash",
  "workspace": {"mode": "isolated_worktree", "changed_paths": ["..."]},
  "validation": {"scope": "passed", "status": "passed"},
  "usage": {"cost_usd": 0.0, "cost_estimated": true, "latency_ms": 8120},
  "provenance": {"backend": "opencode-run", "model_ref": "opencode-go/deepseek-v4-flash"}
}
```

`usage.cost_estimated: true` means the adapter did not report real token cost. Do not treat raw delegate text as success.

### Budget & privacy

- Local ledger `soft_daily_usd`/`hard_daily_usd`. At hard limit → `E_DELEGATION_BUDGET_EXCEEDED` — do the work locally, spend nothing.
- `SECRET` never to cloud; `CONFIDENTIAL` local-only by default.
- `.env`/`*.pem` never staged implicitly; prompts/completions absent from event DB.

## Error handling

All failures are `{ok:false, code:"E_...", message, retryable, suggested_action}`. Never print raw traceback without `--debug`; secrets always redacted. Act per the Fallback policy above. Common codes:

- `E_TARGET_NOT_FOUND` → `targets list` (then fall back to doing it yourself)
- `E_ARTIFACT_MISSING/STALE` → `inventory sync --machine`
- `E_GPU_BUSY_FOREIGN` → `gpu status --machine` (never kill foreign)
- `E_LEAK_SUSPECTED` → `doctor --target` (never bypass)
- `E_PORT_BUSY` → `ps --machine` (never force)
- `E_DELEGATION_BUDGET_EXCEEDED` → do the work locally
- `E_OPENCODE_NOT_FOUND` → do the work locally
- `E_DELEGATE_OUT_OF_SCOPE_CHANGE` → fix `allowed_write_paths`, re-dispatch or do it yourself
- `E_DELEGATE_TIMEOUT` / `E_DELEGATE_PROVIDER_FAILURE` → `queue retry` once, then do it yourself
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
  BRAIN (you): design interface/invariants
  WORKERS (parallel): enumerate call sites, inspect tests, enumerate config users, error paths
  BRAIN: synthesize → implementation plan
  DRIVER: isolated worktree patch
  WORKERS (parallel): tests, docs, lint, diff inspection
  BRAIN: review/merge

User: Delegate W3 but modelctl returns E_DELEGATE_PROVIDER_FAILURE.
Skill: modelctl queue retry <run_id> --json
       # still failing → do the callsite enumeration yourself, same constraints
```

For delegation, construct a minimal context package (task spec + declared files + validation contract), not the whole conversation.
