---
name: modelctl
description: Deterministic model control for remote vLLM (SSH + systemd) and delegated opencode execution via opencode run (brain = you, the primary agent; driver → zai-coding-plan/glm-5.3, worker → zai-coding-plan/glm-5.3-flash by default via the user's z.ai API plan; override per task/CLI/config with any model from an allowlisted provider — auto-admitted, no sync/assign needed). Use when starting/inspecting/connecting to a local model, checking which machine has a model, or dispatching bounded driver/worker tasks as subagents instead of spending brain tokens. If a delegated run fails, do the work yourself rather than stopping.
---

# modelctl — Model Control & Delegation Skill

`modelctl` is the deterministic control plane for two backends: local/private vLLM on remote GPU machines (SSH + systemd --user) and delegated OpenCode execution. It is **fail-closed**: success is only reported after verified postconditions, and refusals carry stable error codes.

Always call `modelctl --json --non-interactive ...` (flags before the subcommand) so results are machine-readable and no prompt is spawned.

## When to use

- Run, stop, or inspect a configured local model (`qwen-72b@gpu-a`).
- Discover which machine holds a model artifact (`inventory list`).
- Get a loopback endpoint via SSH tunnel (`connect`/`endpoint`).
- Delegate a bounded unit of coding work (`delegate run`) instead of spending your own context/tokens.
- Inspect routing, budget, validation, or failure history (`delegates`, `budget`, `queue`, `delegate status`).

## Never do (safety invariants)

- Invent a machine/model alias — resolve via `inventory list`, `targets list`, or `machines list`.
- Invent SSH hostnames, model paths, GPU indices, ports, or free-form vLLM flags.
- Construct `ssh` or `vllm serve` yourself — invoke only `modelctl`.
- Kill foreign GPU processes, force-past `E_GPU_BUSY_FOREIGN`/`E_LEAK_SUSPECTED`, or tunnel around a safety error.
- Log or echo prompts, completions, or secrets.
- Modify config unless explicitly asked; prefer `config validate` / `config resolve` for inspection.
- Treat raw delegate text as success — only the validation envelope counts.

## Error → action (single table)

**Do the work yourself** (execution failures — delegated path couldn't run; keep the same scope/validation constraints):
`E_OPENCODE_NOT_FOUND`, `E_DELEGATE_MODEL_UNAVAILABLE`, `E_DELEGATION_POLICY_DENIED`, `E_DELEGATE_TIMEOUT`, `E_DELEGATE_PROVIDER_FAILURE`, `E_DELEGATE_OUT_OF_SCOPE_CHANGE`, `E_DELEGATE_VALIDATION_FAILED`, `E_WORKTREE_*`.

**Surface and follow `suggested_action`** (environmental/safety refusals — never improvise around them):
`E_GPU_BUSY_FOREIGN` → `gpu status`; `E_LEAK_SUSPECTED` → `doctor --target`; `E_PORT_BUSY` → `ps`; `E_SSH_UNREACHABLE` → `machines probe`; `E_ARTIFACT_MISSING/STALE` → `inventory sync --machine`; `E_DELEGATION_PRIVACY_DENIED`.

**Budget:** soft limit warns; hard limit → `E_DELEGATION_BUDGET_EXCEEDED` → do the work locally, spend nothing.

**Retry discipline before falling back:** transient failures (`E_DELEGATE_TIMEOUT`, `E_DELEGATE_PROVIDER_FAILURE`) → retry once via `modelctl queue retry <run_id>`. Still failing → do it yourself. Report in your summary what was refused, why, and what you did instead.

## Decision flow

```
User intent
  ├── exact model + machine known?
  │     ├── yes → config resolve --target M@MACHINE
  │     └── no  → inventory list / targets list / machines list
  ├── mutation? (start/stop/restart/connect/delegate run)
  │     ├── yes → ONE exact target (model@machine), --json --non-interactive
  │     └── no  → safe discovery/status (status/ps/logs/events/doctor)
  └── delegation? → classify brain/driver/worker → delegate run
```

If two machines have `qwen-72b` AVAILABLE, do not randomly choose — ask or require `--machine`.

## Command surface

```bash
# discovery & validation
modelctl inventory list|sync --json          modelctl targets list --json
modelctl machines list|probe --json          modelctl config validate|resolve --target T --json

# vLLM lifecycle (fail-closed)
modelctl start qwen-72b --machine gpu-a [--ttl 2h] [--replace] --json --non-interactive
modelctl status|logs qwen-72b --machine gpu-a --json
modelctl connect|endpoint qwen-72b --machine gpu-a --json   # prefer tunnel over exposure
modelctl stop qwen-72b --machine gpu-a --json --non-interactive
modelctl ps|events|reconcile|doctor|gc --json

# delegation
modelctl delegates sync|list|doctor --json   # doctor emits .modelctl/delegation.lock
modelctl delegates assign <ref> --bin worker|driver [--enable|--disable]  # route a catalog model to a bin
modelctl delegates admit <provider/id> --json  # admit any allowlisted-provider model, no bin needed
modelctl delegate run --role worker|driver --task-file .modelctl/tasks/T1.json [--model provider/id] --json
modelctl delegate batch --role worker --tasks-dir .modelctl/tasks/search/ [--model provider/id] --json
modelctl delegate graph --file .modelctl/taskgraph.json [--model provider/id] --json
modelctl delegate status|history|cancel --json
modelctl queue status|retry <run_id> --json  modelctl budget status|history --json
```

Postconditions: `READY` requires live process + `/health` 200 + `/v1/models` identity + digest. `stop` reports STOPPED only after the process group is gone AND the port is free. Local-simulation payloads carry `"simulation": true`. `gc` never deletes model weights.

## Delegation in five rules

`delegate run` is a **subagent spawn**: you (brain) dispatch bounded work, then review the returned envelope.

1. **The `objective` field IS the prompt.** Write it self-contained: goal, input paths, constraints, exact deliverable format. A vague objective gets a bad result.
2. **One task = one unit.** Split into driver (one coherent implementation) + workers (parallel search/inspect/enumerate).
3. **Respect the boundary.** Writes must stay inside `allowed_write_paths` or you get `E_DELEGATE_OUT_OF_SCOPE_CHANGE`.
4. **Supervise, don't trust.** `ok: true` requires scope + patch-size + validation passed. Re-run declared `validation` commands yourself in the real repo before merging.
5. **Escalate bounded:** worker fail → one driver repair; driver ambiguity → brain. Workers cannot recursively delegate.

## Configurable orchestration

For independent work, use `delegate batch`; for dependent work, use a JSON DAG
with `task_id`, `role`, `depends_on`, and either the task fields inline or under
`task`. `delegate graph` validates the entire graph before starting a model:
duplicate IDs, missing dependencies, cycles, excessive depth/fan-out, and
invalid roles are refused.

The scheduler is deterministic and bounded by configuration:

```yaml
delegation:
  orchestration:
    max_total_parallel: 12
    max_task_depth: 1
    max_task_fanout: 8
    worker_batch_size: 8
    fail_fast: true
    overlap_policy: serialize   # or allow_disjoint for known disjoint writes
budget:
  max_parallel_total: 12
  max_parallel_worker: 12
  max_parallel_driver: 3
  max_retry_per_candidate: 1
```

Independent read-only workers may run in parallel. Shared-project writes are
serialized by default; failed prerequisites skip their dependents, and
`fail_fast` prevents new work after a failure. Results are compact summaries
sorted by `task_id`; raw prompts and completions are never returned by the
orchestrator. Keep final synthesis, architecture, and acceptance decisions in
the brain.

Routing heuristic:

```
high judgment/ambiguity            → brain (you)
low judgment + repetitive/parallel → worker (zai-coding-plan/glm-5.3-flash)
low judgment + large bounded spec  → driver (zai-coding-plan/glm-5.3)
```

Model selection precedence: task file `model_ref` > `delegate run --model` > config `delegation.roles.<role>.model` > bin default. Config defaults are set in `~/.config/modelctl/config.yaml`: driver → `zai-coding-plan/glm-5.3`, worker → `zai-coding-plan/glm-5.3-flash` (z.ai API plan), allowlist `[zai-coding-plan]`. An **explicit** model request is flexible: any `provider/id` under an allowlisted provider (`delegation.execution.provider_allowlist`, default `[zai-coding-plan]`) is usable without prior sync or assignment — unknown models are auto-admitted (enabled + AVAILABLE, audited) and unclassified-disabled models are enabled by the request itself. Still fail-closed, never silent fallback: non-allowlisted provider → `E_DELEGATION_POLICY_DENIED`; operator-disabled (`assign --disable`) or UNAVAILABLE model → refused. Implicit bin routing stays strictly classified. To pre-admit or route for implicit use: `delegates admit <ref>` / `delegates assign <ref> --bin worker`.

Delegated runs execute in the **real project directory** (the directory `modelctl` was invoked from, or the task's `workdir` field) — edits land in the actual repo, and changed paths are verified via `git status`, not the model's self-report. Set `"isolated": true` in the task file to opt into a throwaway temp worktree (auto-cleaned after the run).

## opencode invocation gotchas (delegation backend)

`delegate run` shells out to `opencode --pure run`. These environment facts cause
recurring false "model is broken" diagnoses — never rediscover them:

- **Auth lives in `$XDG_DATA_HOME/opencode/auth.json`** (default
  `~/.local/share/opencode/auth.json`). If `XDG_DATA_HOME` is set to a fresh dir,
  z.ai/zai-coding-plan credentials are missing and **every** model fails with
  `Unexpected server error` — this is NOT a model/provider outage. To run isolated,
  seed auth first:
  ```bash
  SANDBOX=$(mktemp -d) && mkdir -p "$SANDBOX/opencode"
  cp ~/.local/share/opencode/auth.json "$SANDBOX/opencode/auth.json" && chmod 600 "$SANDBOX/opencode/auth.json"
  XDG_DATA_HOME="$SANDBOX" opencode run --pure -m zai-coding-plan/glm-5.3-flash 'ping'
  ```
- **Concurrent `opencode run` processes share the log file**
  (`~/.local/share/opencode/log/opencode.log`) and can race on it — a
  "could not open log" error is transient, not fatal. Run one session per data dir.
- **Diagnose before blaming the model:** on `E_DELEGATE_PROVIDER_FAILURE` /
  `Unexpected server error`, first verify provider health with a minimal
  `opencode run --pure -m <model> 'Reply with exactly: OK'` (no XDG overrides).
  If that succeeds, the failure was environmental (auth/log), not the model.
- **On `Cannot connect to API`:** test egress in the same shell
  (`curl -s -o /dev/null -w "%{http_code}" --max-time 8 https://api.z.ai` —
  any HTTP code means reachable; a timeout means no network/proxy there).

Before writing a task file, read **SKILL-REFERENCE.md** (next to this file) for the full task contract schema, result envelope spec, and workspace containment rules. For delegation, construct a minimal context package (task spec + declared files + validation contract), not the whole conversation.
