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
- `model_ref` — optional explicit model override (`"provider/id"`). Precedence: task `model_ref` > CLI `--model` > config `delegation.roles.<role>.model` > bin default. Config defaults: driver → `zai-coding-plan/glm-5.3`, worker → `zai-coding-plan/glm-5.3-flash` (z.ai API plan). Flexible admission: any model under an allowlisted provider (`delegation.execution.provider_allowlist`, default `[zai-coding-plan]`) is usable without prior sync/assign; unknown models are auto-admitted enabled + AVAILABLE. Fail-closed only for non-allowlisted providers, operator-disabled models, and UNAVAILABLE models.
- `inputs.paths` — files/dirs the worker may read. Stage only what is declared.
- `allowed_write_paths` — `[]` for read-only workers. Drivers declare explicit paths; canonicalized, symlink-safe, relative to the isolated workspace.
- `validation` — exact argv lists (e.g. `[["pytest", "-q", "tests/config"]]`). Run in the run's workdir after the run; all must exit 0.
- `timeout_s` — optional override of the role default (worker 600s, driver 900s).
- `workdir` — optional directory the run executes in. Default: the directory `modelctl` was invoked from (the real project). `"isolated": true` instead spawns a throwaway temp worktree (auto-cleaned).
- `variant` — optional reasoning-effort override. Default: none (the model's own default; `--variant` flag is omitted). Pass e.g. `"low"` for trivial enumeration to cut latency.
- `data_class` / `max_data_class` — `SECRET` never goes to cloud; `CONFIDENTIAL` local-only by default.
- `agent_profile` — one of `modelctl-worker-read`, `modelctl-worker-edit`, `modelctl-driver`.

Driver tasks add: concrete `allowed_write_paths`, `validation`, and a patch-size budget (default 1500 lines).

## Invocation contract

```
opencode --pure run --model <ref> --agent <profile> --format json \
         --dir <workdir> "<prompt>"
```

- `--pure` required; `--auto` never.
- `--variant` omitted by default (model's own default reasoning); only attached when the task/role config sets one.
- Environment: auth is read from `$XDG_DATA_HOME/opencode/auth.json`. A fresh/empty XDG data dir → `Unexpected server error` for every model (missing credentials, NOT an outage) — seed `auth.json` first or run without XDG overrides. Concurrent runs sharing one data dir can race on the shared log; treat log-open errors as transient. On provider failure, health-check with `opencode run --pure -m <model> 'Reply with exactly: OK'` before blaming the model.
- Process-group containment: owned process tree with deadline → SIGTERM → SIGKILL → verified cleanup.

## Result envelope (stable)

```json
{
  "ok": true,
  "run_id": "dlg_...",
  "role": "driver",
  "selected_model": "zai-coding-plan/glm-5.3",
  "workspace": {"mode": "project_dir", "changed_paths": ["..."]},
  "validation": {"scope": "passed", "status": "passed"},
  "usage": {"cost_usd": 0.0, "cost_estimated": true, "latency_ms": 8120},
  "provenance": {"backend": "opencode-run", "model_ref": "zai-coding-plan/glm-5.3"},
  "task_hash": "<sha256>"
}
```

Interpretation rules:

- Only the envelope counts as success. Raw delegate text is never proof.
- `usage.cost_estimated: true` means no real token cost was reported; the ledger records 0.0 until adapters report usage.
- `validation.scope` reflects `changed_paths ⊆ allowed_write_paths` as reported by the backend — re-verify in the real repo before merging.

## Workspace & containment

- Default: runs execute in the **real project directory** (invocation cwd or task `workdir`); changed paths are derived from `git status`, not the model's self-report; path scope enforced post-run; tests executed; patch size capped.
- `"isolated": true` opts into a throwaway temp workspace; it is verified deleted after the run, success or failure (cleanup failure → `E_WORKTREE_CLEANUP_FAILED`).

## Escalation ladder

Enforced by the brain (you), not by code:

```
worker validation fail → one bounded driver repair
driver ambiguity       → brain (you)
max_escalations: 2, max_retry_per_candidate: 1
workers cannot recursively delegate
```

## Orchestration policy

`delegate batch` is a dependency-free graph. `delegate graph` accepts a JSON
object with a non-empty `nodes` list. Each node has a unique `task_id`, a
`role` (`worker` or `driver`), optional `depends_on` task IDs, and either
inline task fields or a nested `task` object.

The scheduler validates the full DAG before execution, then runs ready nodes in
deterministic `task_id` order subject to these effective limits:

```yaml
delegation:
  orchestration:
    max_total_parallel: 12
    max_task_depth: 1
    max_task_fanout: 8
    worker_batch_size: 8
    fail_fast: true
    overlap_policy: serialize
budget:
  max_parallel_total: 12
  max_parallel_worker: 12
  max_parallel_driver: 3
  max_retry_per_candidate: 1
```

Budget caps can only reduce delegation concurrency. `serialize` prevents
shared-project reads and writes from overlapping with a write-capable node;
`allow_disjoint` allows only declared, non-overlapping write paths to run
concurrently. Isolated tasks do not participate in shared-workspace conflicts.
Failed prerequisites produce `skipped` nodes, `fail_fast` stops new scheduling,
and retries are limited to transient provider/timeout/rate-limit failures.
The graph result is a compact per-node summary sorted by `task_id`, not raw
model output.

## Model catalog & overrides

- `modelctl delegates sync` — refresh catalog from `opencode models`; discovered models land as `unclassified` + disabled.
- Explicit requests (`task model_ref` / CLI `--model`) are flexible: any allowlisted-provider model works immediately — unknown models are auto-admitted (audited), unclassified-disabled ones are enabled by the request. No sync/assign dance needed for one-off use.
- `modelctl delegates admit <provider/id>` — pre-admit a model without bin assignment (same allowlist policy).
- `modelctl delegates assign <ref> --bin worker|driver [--enable|--disable]` — route a model to a bin for implicit/default routing, or deliberately disable it (explicit requests to an operator-disabled model then fail closed with `E_DELEGATION_POLICY_DENIED`).
- Non-allowlisted providers fail closed (`E_DELEGATION_POLICY_DENIED`); extend via `delegation.execution.provider_allowlist`.
- `queue retry` re-runs with the original run's selected model.

## Budget & privacy

- Local ledger with `soft_daily_usd` / `hard_daily_usd`. Hard limit → `E_DELEGATION_BUDGET_EXCEEDED`; do the work locally instead.
- `.env` / `*.pem` are never staged implicitly; prompts/completions are absent from the event DB.
