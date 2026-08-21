---
name: modelctl
description: Deterministic model control for remote vLLM (SSH + systemd) and delegated opencode execution via opencode run (driver → deepseek-v4-flash, worker → hy3 by default; override per task/CLI/config with any catalog-enabled model). Use when starting/inspecting/connecting to a local model, checking which machine has a model, or dispatching bounded driver/worker tasks as subagents instead of spending brain tokens. If a delegated run fails, do the work yourself rather than stopping.
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
modelctl delegate run --role worker|driver --task-file .modelctl/tasks/T1.json [--model provider/id] --json
modelctl delegate batch --role worker --tasks-dir .modelctl/tasks/search/ [--model provider/id] --json
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

Routing heuristic:

```
high judgment/ambiguity            → brain (you)
low judgment + repetitive/parallel → worker (opencode-go/hy3)
low judgment + large bounded spec  → driver (opencode-go/deepseek-v4-flash)
```

Model selection precedence: task file `model_ref` > `delegate run --model` > config `delegation.roles.<role>.model` > bin default. An explicit model must exist in the catalog, be enabled and AVAILABLE — otherwise the run fails closed (`E_DELEGATE_MODEL_UNAVAILABLE` / `E_DELEGATION_POLICY_DENIED`), never silently falls back. To use a catalog-discovered model, first route it: `delegates assign <ref> --bin worker`.

Before writing a task file, read **SKILL-REFERENCE.md** (next to this file) for the full task contract schema, result envelope spec, and workspace containment rules. For delegation, construct a minimal context package (task spec + declared files + validation contract), not the whole conversation.
