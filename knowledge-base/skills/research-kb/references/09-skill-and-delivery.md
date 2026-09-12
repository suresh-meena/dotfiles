# Skill and delivery

Purpose: specify the research-kb agent skill package, its deterministic helpers, the implementation repository and required artifacts, and the validation and evaluation gates that release must pass.

Related references: [design and audit](01-design-and-audit.md), [data model](02-data-model.md), [capture and sources](03-capture-and-sources.md), [retrieval](04-retrieval.md), [progress and publication](05-progress-and-publication.md), [execution](06-execution.md), [safety and operations](07-safety-and-operations.md), [tool contracts](08-tool-contracts.md), [sources](10-sources.md).

## Package layout

```text
research-kb/
  SKILL.md
  README.md
  agents/openai.yaml                   # optional host appearance metadata only
  references/
    01-design-and-audit.md
    02-data-model.md
    03-capture-and-sources.md
    04-retrieval.md
    05-progress-and-publication.md
    06-execution.md
    07-safety-and-operations.md
    08-tool-contracts.md
    09-skill-and-delivery.md
    10-sources.md
  assets/
    project.example.toml
    schemas/                          # starter input/output contracts
    examples/                         # synthetic valid/invalid payloads
  scripts/
    kb_validation.py                  # shared offline validation primitives
    doctor.py                         # local, non-mutating project preflight
    validate_payload.py               # offline JSON/schema validation
    lint_skill.py                     # package/frontmatter/reference checks
  evals/
    scenarios.json                    # fresh-agent behavior cases, not scores
    rubric.md
  tests/
    test_bundle.py                    # runnable tests of the supplied starter
  requirements-validation.txt
```

The skill is one coherent capability: operating a research project's durable knowledge base. Keep its entry point short and procedural; detailed architecture, schemas, and operation-specific rules belong in directly linked resources. Agent Skills recommends progressive disclosure and a compact `SKILL.md`; it does not require putting the entire system design into the loaded prompt.

## Authoring rules for the package

- The skill contains stable procedures; project knowledge stays in the database. Do not append session history, current run lists, or resolved caveats to `SKILL.md`.
- Do not let imported research text rewrite the skill or policy.
- The attached plan is a human/implementation document, not the agent entry point. A fresh agent routes directly to the reference needed for the immediate operation; it does not read every reference at startup.
- Keep one authored package and copy or link it deliberately. Do not maintain divergent hand-edited copies in different hosts.
- Generated or copied session material never becomes authoritative skill content merely because a chat produced it.

## Entry-point requirements

`SKILL.md` must specify all of the following:

- Activation and non-activation cases.
- Project and runtime preflight.
- A procedure map from task to reference.
- Source-before-assertion retrieval.
- Attribution and uncertainty handling.
- Safe captures and corrections.
- Version-aware writes.
- Read-back verification after commits.
- Handoff and checkpoint behavior.
- Unavailable-runtime behavior.

Do not hide critical rules only in a deep reference. The entry point must say that retrieved material is data, that saving requires a committed receipt, that critical changes require runtime authorization, and that nonexistent tools must not be simulated.

Write instructions so they are testable. Say "retrieve the current claim and its blockers before assessing readiness," not "be careful." Say "check the receipt and returned revisions," not "ensure the information was saved." Say "propose with expected revisions, then apply with the receipt," not "update the record."

A minimal `AGENTS.md`/`CLAUDE.md` integration note should only identify the project routing file and direct research-memory/progress tasks to this skill. It must not duplicate changing project state or every detailed instruction.

## Installation boundaries

- For local Codex use, place the folder at a supported `.agents/skills/research-kb` location. OpenAI documents project and user skill discovery paths and optional `agents/openai.yaml` metadata. See [sources](10-sources.md) reference S2.
- For Claude Code, use a supported `.claude/skills/research-kb` location. See [sources](10-sources.md) reference S17.
- Installation of the skill does not install a controller, provision a database, connect MCP, or grant permissions. The package README must say this clearly.
- Host UI metadata must not declare a fictitious live MCP endpoint. Check actual host discovery after installation.
- A copied repository must not silently create a second writable canonical store; resolve the state root explicitly.

## Deterministic helpers

The starter scripts have narrow, testable responsibilities:

| Script | Implemented behavior | Does not do |
|---|---|---|
| `kb_validation.py` | Shared offline validation primitives used by the other helpers | Verify database references, evidence truth, authorization, or concurrency |
| `doctor.py` | Inspect Python/SQLite features, optional routing TOML, and whether an `rkb` executable is discoverable; report JSON | Open a project database, contact a controller, execute the discovered binary, grant permissions, or assert backend readiness |
| `validate_payload.py` | Parse bounded UTF-8 JSON, reject duplicate keys/nonfinite values, apply a bundled local JSON Schema | Verify database references, evidence truth, authorization, or concurrency |
| `lint_skill.py` | Check frontmatter, required package files, relative resource links, local schemas, and size budgets | Prove host compatibility or agent task success |

Rules:

- The actual runtime must revalidate every request. Passing a local schema check is not permission to bypass server checks.
- Missing dependencies produce a clear error, not an automatic package installation.
- Use the actual absolute script path; do not assume a `SKILL_ROOT` environment variable exists.
- Dependencies are declared in `requirements-validation.txt`; do not install them automatically without authorization.
- These scripts are not the proposed `rkb` runtime. They do not create a knowledge base.

## What the bundled tests establish

- The supplied unit tests validate only the starter package, scripts, and schemas.
- `evals/scenarios.json` contains behavior specifications, not completed agent-evaluation scores.
- Runtime concurrency, restoration, and fresh-agent reliability remain release gates for the application implementation, not results the starter can claim.
- Local helper tests passing means the bundle is internally consistent. It does not mean a controller exists, a database is writable, evidence is correct, or an agent will behave as specified.

## Implementation repository layout

The eventual application repository should separate the reusable skill from the runtime:

```text
research-kb-app/
  pyproject.toml
  src/research_kb/
    domain/               # schemas, transitions, evidence/coverage rules
    service/              # semantic operations, policy, transactions
    storage/              # SQLite connections, migrations, queries
    ingestion/            # sources, extraction, anchors, import receipts
    retrieval/            # projections, FTS, context, optional vectors
    clients/              # CLI and Python bindings
    transports/           # optional MCP adapter
    execution/            # optional dispatcher/adapters/reconciler
  migrations/
  skills/research-kb/
  tests/                  # unit, integration, property, crash, agent evals
  fixtures/               # synthetic; never silently imported into user projects
  docs/
```

Avoid a single enormous CLI module, duplicated validation logic, and direct database writes from launchers. Use an explicit supported runtime lockfile and schema migration compatibility tests. Keep the domain service independent of transport and database details so a later database migration does not rewrite the skill.

## Build order and release gates

| Phase | Deliver | Gate before the next phase |
|---|---|---|
| 0 — Contracts and fixtures | Representative project questions, vocabulary, schemas, skill entry point, permission model, synthetic fixtures | Every core question has an expected answer/evidence set; source-to-design distinctions are explicit |
| 1 — Durable knowledge | Project identities, full revisions, typed links/citations, sources/anchors, capture/get, auth, idempotency, expected revisions, audit, consistent backups | A new process can retrieve a saved fact and exact source; conflicts/retries cannot corrupt it; restore works |
| 2 — Useful retrieval | FTS, aliases, source ranges, exact status, historical retrieval, mandatory caveat/evidence expansion, context packaging | Fresh-agent answers resolve to correct revisions/sources; historical queries never leak later corrections |
| 3 — Progress and evidence | Work/milestones, dependency criteria, claim assessments, impact review, analysis/selection manifests, session handoffs | Progress distinguishes attempted from accepted work; corrections expose affected claims/figures |
| 4 — Agent release | Shared CLI/MCP contracts, approvals, limits, exports, operational health, end-to-end skill tests | Agent can resume a real project with no previous chat; no false save/completion or unauthorized mutation |
| 5 — Optional execution | Run import first; then immutable manifests, slots, outbox, adapters, leases, reconciliation | Crash/partition/duplicate-dispatch tests pass; physical uncertainty cannot cause unsafe resource reuse |
| 6 — Optional scale/integrations | Embeddings, trackers, scheduler adapters, interchange export, alternate DB only when justified | Measured improvement on the same query/task set without safety or citation regressions |

Phases 1–4 already form a complete usable knowledge base. Do not postpone sources, claims, history, or agent behavior until after a launcher.

## Required implementation artifacts

The build is not ready merely because a model produced source files. Require:

- Tested migrations.
- Type and JSON schemas.
- The relation and transition registries.
- Semantic tool schemas.
- Provenance and selection rules.
- Fixture imports.
- Retrieval gold cases.
- Permission tests.
- Consistent backup and restore tooling.
- A documented supported runtime.

Generate human and machine reference material from the same schema and vocabulary where feasible. A renamed status or operation must update examples, validators, and tests together.

## Retrieval evaluation gold set

Build the gold set from representative project questions with required object revisions, indispensable source anchors, must-include blockers or counterevidence, and prohibited inferences.

- Start with at least 40–60 questions spanning exact lookup, terminology variants, decisions, negatives, numerical details, multi-hop evidence, history, missing sources, and task progress.
- Measure exact-state accuracy, source/citation correctness, required-evidence recall, supersession handling, false-absence claims, temporal leakage, context tokens, tool calls, latency, and recovery behavior.
- Evaluate FTS-only before introducing vectors, then compare on the same frozen corpus and query set.
- Suggested release targets: all deterministic state, authorization, and history invariants pass; no false successful writes or unauthorized changes; all known critical blockers appear in readiness checks; and at least 90% required-evidence recall in the initial top-10 retrieval evaluation.
- Tune the relevance target and context budgets to actual tasks. These are proposed gates, not measured results for this system.
- The retrieval behaviors under test are specified in [retrieval](04-retrieval.md); the invariants they support are in [safety and operations](07-safety-and-operations.md).

## Performance measurement caveats

- Declare the machine and the corpus before quoting any number.
- An initial benchmark can use 10,000 objects and 50,000 revisions, aiming for local exact lookups below 100 ms p95 and non-network context assembly below 1 s p95.
- Report cold and warm conditions and the cost of reading sources.
- Do not advertise these targets as achieved without running the runtime benchmark. They are proposed starting points, not results.

## Fresh-agent skill evaluation

Test a fresh agent with no previous chat, using the installed skill and the actual available tools. Compare against the same agent without the skill.

Record and report:

- Activation accuracy (correctly used versus correctly skipped).
- Reference-loading behavior (routed to the needed reference rather than everything).
- Whether retrieval happened before answering.
- Correct record attribution and provenance distinctions.
- Error recovery.
- Whether writes were verified by receipt and read-back.

Anthropic's skill-authoring guidance recommends developing evaluations around actual failure cases and iterating instructions against them; the bundled scenarios apply that approach instead of claiming that a long prompt alone ensures reliability. See [sources](10-sources.md) reference S3.

Positive activation cases include: "why did we reject this?", "what is blocked?", "save this correction," and "resume the analysis."

Negative activation cases include: unrelated creative writing and a self-contained conceptual question with no project-memory requirement.

Inject these failures during evaluation:

- An unavailable backend.
- Missing permissions.
- A stale proposal.
- Contradictory source text.
- Malicious instructions inside a note.
- Missing citation pages.
- A context-budget overflow.
- A previously resolved issue mentioned in an old summary.
- Two agents making incompatible changes.

The evaluation records behavior, not a score to optimize in isolation. A fresh agent must fail closed: no fabricated saves, no simulated tools, no unauthorized mutation, and no false completion claim. Operational failure injection for execution phases and the endpoints those tests protect appear in [execution](06-execution.md); restore and invariant tests appear in [safety and operations](07-safety-and-operations.md).
