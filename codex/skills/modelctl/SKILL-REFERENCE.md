# modelctl — Delegation Reference

Deep reference for dispatching delegate runs. Read when writing task files or interpreting envelopes; the main SKILL.md holds the invariants.

## Task contract (`task_file` JSON)

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

Field semantics:

- `objective` — sent to the model verbatim as the prompt. Make it self-contained; the rest of the contract is enforced by modelctl, not shown to the model.
- `model_ref` — optional explicit model override (`"provider/id"`). Precedence: task `model_ref` > CLI `--model` > config `delegation.roles.<role>.model` > bin default. Must be enabled + AVAILABLE in the catalog or the run fails closed.
- `inputs.paths` — files/dirs the worker may read. Stage only what is declared.
- `allowed_write_paths` — `[]` for read-only workers. Drivers declare explicit paths; canonicalized, symlink-safe, relative to the isolated workspace.
- `validation` — exact argv lists (e.g. `[["pytest", "-q", "tests/config"]]`). Run inside the isolated workspace after the run; all must exit 0.
- `timeout_s` — optional override of the role default (worker 600s, driver 1800s).
- `variant` — optional reasoning-effort override (`"max"` default from role config; e.g. `"low"` for trivial enumeration saves minutes of latency).
- `data_class` / `max_data_class` — `SECRET` never goes to cloud; `CONFIDENTIAL` local-only by default.
- `agent_profile` — one of `modelctl-worker-read`, `modelctl-worker-edit`, `modelctl-driver`.

Driver tasks add: concrete `allowed_write_paths`, `validation`, and a patch-size budget (default 1500 lines).

## Invocation contract

```
opencode --pure run --model <ref> --agent <profile> --format json \
         --dir <isolated-workspace> --variant max "<prompt>"
```

- `--pure` required; `--auto` never.
- `--variant max` (maximum reasoning) is the current default for every role.
- Process-group containment: owned process tree with deadline → SIGTERM → SIGKILL → verified cleanup.

## Result envelope (stable)

```json
{
  "ok": true,
  "run_id": "dlg_...",
  "role": "driver",
  "selected_model": "opencode-go/deepseek-v4-flash",
  "workspace": {"mode": "isolated_worktree", "changed_paths": ["..."]},
  "validation": {"scope": "passed", "status": "passed"},
  "usage": {"cost_usd": 0.0, "cost_estimated": true, "latency_ms": 8120},
  "provenance": {"backend": "opencode-run", "model_ref": "opencode-go/deepseek-v4-flash"},
  "task_hash": "<sha256>"
}
```

Interpretation rules:

- Only the envelope counts as success. Raw delegate text is never proof.
- `usage.cost_estimated: true` means no real token cost was reported; the ledger records 0.0 until adapters report usage.
- `validation.scope` reflects `changed_paths ⊆ allowed_write_paths` as reported by the backend — re-verify in the real repo before merging.

## Workspace & containment

- Read-only workers: staged directory with declared input files only.
- Write-capable runs: isolated workspace; path scope enforced post-run; tests executed; patch size capped.
- Temporary workspaces are verified deleted after every run, success or failure; cleanup failure surfaces as `E_WORKTREE_CLEANUP_FAILED`.

## Escalation ladder

Enforced by the brain (you), not by code:

```
worker validation fail → one bounded driver repair
driver ambiguity       → brain (you)
max_escalations: 2, max_retry_per_candidate: 1
workers cannot recursively delegate
```

## Model catalog & overrides

- `modelctl delegates sync` — refresh catalog from `opencode models`; discovered models land as `unclassified` + disabled.
- `modelctl delegates assign <ref> --bin worker|driver [--enable|--disable]` — route a model to a bin so it becomes eligible for that bin's default routing.
- Explicit requests (task `model_ref` / CLI `--model`) bypass bin routing but still require the model to be enabled + AVAILABLE; failures are fail-closed, never silent fallbacks.
- `queue retry` re-runs with the original run's selected model.

## Budget & privacy

- Local ledger with `soft_daily_usd` / `hard_daily_usd`. Hard limit → `E_DELEGATION_BUDGET_EXCEEDED`; do the work locally instead.
- `.env` / `*.pem` are never staged implicitly; prompts/completions are absent from the event DB.
