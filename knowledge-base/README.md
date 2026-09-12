# Research Knowledge Base runtime

A local-first, revisioned research knowledge base with an agent skill: sources, definitions,
assumptions, derivations, claims, evidence, decisions, tasks, and progress, plus an optional
execution-provenance module. The implementation follows `research_kb_plan_v3.md`.

**The knowledge base state is stored in the working directory by default** at
`.research/state/<project-id>/`. Routing is committed at `.research/project.toml`. No centralized
state root is used unless an operator explicitly configures one.

## Layout

```text
.
├── migrations -> src/research_kb/migrations   # ordered, checksummed schema migrations
├── src/research_kb/                           # runtime: domain, storage, service, execution, clients
├── skills/research-kb/                        # the agent skill package (SKILL.md, references, assets)
├── fixtures/                                  # synthetic project + retrieval gold set (never auto-imported)
├── tests/                                     # runtime tests: unit, integration, invariants, fixtures
├── docs/                                      # architecture, operations, validation, migration notes
├── bin/                                       # rkb and rkb-mcp wrappers (work without pip install)
└── research_kb_plan_v3.md                     # the implementation specification
```

## Quickstart

```bash
# Initialize a project in the current directory (state goes under ./.research/state/)
bin/rkb init "My Project"

# Runtime health, versions, capabilities
bin/rkb capabilities --json

# Capture through the typed operation registry (auto-applies low-risk captures only)
cat > /tmp/ops.json <<'JSON'
{"operations": [
  {"op": "capture", "payload": {
    "kind": "knowledge", "subkind": "idea",
    "title": "Example idea",
    "state_json": {"subkind": "idea", "proposal": "Describe the idea precisely."}
  }}
]}
JSON
bin/rkb propose --file /tmp/ops.json --auto-apply --json

bin/rkb search "example idea" --json
bin/rkb context --mode question --query "example idea" --json
bin/rkb status --json
bin/rkb changes --after "<cursor from capabilities>" --json
```

Install the console scripts with `pip install -e .` for `rkb` and `rkb-mcp` on `PATH`; the `bin/`
wrappers work without installation.

## Optional execution

The execution module is off by default. Enabling it requires an explicit policy profile change; its
adapters (`import_only`, `local_process`, `slurm`) and leases are documented in
`skills/research-kb/references/06-execution.md`. The runtime will not launch anything while
`execution.enabled` is false.

## Agent skill

The skill is authored once at `skills/research-kb/` and linked into host discovery paths
(`.agents/skills/research-kb`, `.claude/skills/research-kb`). See the skill's `README.md` for
installation boundaries: installing the skill does not install the runtime, create a database,
connect MCP, or grant permissions.

## Tests

```bash
PYTHONPATH=src python3 -m pytest tests skills/research-kb/tests -q
```

The suite covers the plan's invariant matrix: project isolation, immutable history, expected-revision
conflicts, idempotency, transactional audit, source fidelity, historical-correctness (no late
leakage), evidence review rules, counterevidence preservation, work completion criteria, dependency
cycles, transactional FTS freshness, absence-claim honesty, prompt-injection boundaries, consistent
backup/restore, and execution failure injection. See `docs/validation.md`.

## State, backups, recovery

- `rkb backup` uses the SQLite online backup API, then copies source/extraction blobs and writes a
  manifest. `rkb backup-verify --dir DIR` checks integrity, foreign keys, and sampled blob hashes.
- `rkb restore --source DIR --destination DIR` restores into read-only reconciliation mode: a new
  controller epoch, dispatch disabled, auto-approval disabled, leases quarantined, and old outbox
  intents dead-lettered. Operator review is required before enabling writes and dispatch.
- `rkb migrate` applies pending migrations explicitly; opening a newer unsupported schema is refused.
