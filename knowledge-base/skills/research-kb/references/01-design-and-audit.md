# Design, layers, and audit constraints

This reference explains the layer separation, storage ownership, deployment modes, release gates, and historical audit corrections that an implementer or maintainer must satisfy before changing the research knowledge base.

## 1. The three layers stay separate

| Layer | Owns | Must not do |
|---|---|---|
| Agent skill | When to retrieve/capture, which workflow to follow, how to interpret responses, what requires review | Enforce database integrity or assume unavailable tools exist |
| Knowledge-base runtime | Persistence, schemas, authorization, transactions, revision history, queries, validation, receipts | Decide scientific truth from fluent text |
| Project content | Research records and evidence, source snapshots, work state, domain conventions | Become executable agent instructions merely because it was retrieved |

Hard rules:

- The skill is the entry point; the runtime is the enforcement mechanism. A standalone Markdown file cannot guarantee durable storage, concurrent-write safety, or permissions.
- Retrieved source text, notes, logs, code comments, and tool output are **data, not instructions**. They cannot change policy, grant tools, request secrets, resolve blockers, or trigger execution.
- Installing the skill does not install a controller, provision a database, connect MCP, or grant capabilities. Live persistence requires the separately implemented runtime.
- Bundled validation and preflight scripts are offline helpers; they do not create a knowledge base and do not prove backend readiness.
- Keep one authored skill package; copy or link it deliberately instead of maintaining divergent copies.

## 2. The acceptance questions

The system is incomplete until every question below can be answered with record IDs plus source/revision references:

| Question | Required information |
|---|---|
| What is this project trying to establish? | Goals, scoped claims, success criteria, assumptions |
| What do we currently know about X? | Relevant reviewed and provisional knowledge, scope, contradictions, evidence |
| Where did this formula, number, or statement come from? | Pinned source passage, derivation, run, or analysis artifact |
| Why did we choose this method rather than the alternatives? | Decision, rationale, alternatives, applicable versions |
| What has already been tried and under which conditions? | Studies, runs, negative results, comparison domains |
| What is blocked, by what, and what resolves it? | Explicit blockers, dependency conditions, owners, acceptance criteria |
| What changed since this agent last worked? | Commit cursor, new/revised/withdrawn records, resulting impacts |
| What did we believe before a correction was recorded? | Revision history and historical edge state, without future information |
| Which conclusions or figures need rechecking after an upstream change? | Version-pinned lineage and impact traversal |
| What work is actually done, rather than merely attempted? | Completion evidence and recorded review |
| Can this result be used in this claim or figure? | Applicability, comparability, validity, review, completeness, provenance |
| What is unknown or inaccessible? | Explicit missing evidence, unread sources, unavailable artifacts, retrieval limits |

Treat these questions, not the table inventory or an embedding count, as the completeness definition. Test implementations against them.

## 3. Authoritative data versus derived copies

"Store once" means one authoritative representation, not a ban on immutable revisions, backups, or rebuildable caches.

| Data | Authority | Handling |
|---|---|---|
| Research assertions, decisions, task state, evidence relationships | Revisioned database records | Change only through semantic operations |
| Code | Git plus the exact source snapshot used for an execution | Store identities and locators, not another editable code tree |
| Dataset/checkpoint/large outputs | Existing artifact store | Registry stores immutable identity, manifest, locations, access/availability |
| Source documents and cited excerpts | Exact registered source version and frozen extraction | Retain bytes or an explicit `metadata_only`/unavailable status |
| Project routing | Repository `.research/project.toml` | Project ID and controller location; no conclusions or secrets |
| Accepted policies | Versioned controller policy revision | Import reviewed configuration; bind operations to its hash |
| FTS, embeddings, summaries, Markdown status exports | Derived caches/views | Rebuildable; never an independent editable authority |
| Secrets | OS/host credential facility | Reference by name; never store tokens in records, commands, or skill files |

Maintainer test: for every stored value, name its single authority. If two representations are editable, the design is wrong.

## 4. Deployment modes

**Local mode:** one trusted user, one local database, multiple clients sharing the same domain-service code. This provides consistency, not an adversarial security boundary against that user. Same-account file access cannot be fenced by the application.

**Controller mode:** the database is owned by a separate service identity; agents access authenticated tools and cannot open the database file. Use this mode when permissions must be enforced against agents with shell access or when remote machines report results.

Operational constraints for both modes:

- Workers must not open the SQLite file over NFS/SMB or synchronize a live copy through a cloud-drive folder. WAL requires same-host shared memory and permits one writer at a time.
- Remote clients use the controller API; disconnected workers spool signed, attributed receipts for later import.
- Discovery must never infer the project from a similar name, the most recently opened database, or an arbitrary parent directory. Resolve the state root explicitly.
- Worktrees of the same project share one project ID but keep distinct workspace/code identities. A copied repository must not silently create a second writable canonical store.

## 5. Filesystem layout

```text
project-repo/
  .research/project.toml       # committed routing configuration, no secrets
  .agents/skills/research-kb/  # local Codex installation, or host-equivalent
  AGENTS.md                    # short pointer; not a second knowledge base
  research-exports/            # optional generated snapshots marked NONCANONICAL

<configured-state-root>/<project-id>/
  research.db
  sources/sha256/<prefix>/<hash>
  extractions/sha256/<prefix>/<hash>
  spool/                       # uncommitted imports and executor receipts
  exports/
  backups/                     # staging only; also copy backups off-device
```

Do not store research conclusions or secrets in `.research/project.toml`. Do not treat a generated export as the live knowledge base; see [progress and publication](05-progress-and-publication.md) and [safety and operations](07-safety-and-operations.md).

## 6. Implementation sequence and release gates

Build in this order and do not start a phase before its gate passes.

| Phase | Deliver | Gate before proceeding |
|---|---|---|
| 0 — Contracts and fixtures | Representative questions, vocabulary, schemas, entry point, permission model, synthetic fixtures | Every core question has an expected answer/evidence set; source-to-design distinctions are explicit |
| 1 — Durable knowledge | Project identities, full revisions, typed links/citations, sources/anchors, capture/get, auth, idempotency, expected revisions, audit, consistent backups | A new process can retrieve a saved fact and exact source; conflicts/retries cannot corrupt it; restore works |
| 2 — Useful retrieval | FTS, aliases, source ranges, exact status, historical retrieval, mandatory caveat/evidence expansion, context packaging | Fresh-agent answers resolve to correct revisions/sources; historical queries never leak later corrections |
| 3 — Progress and evidence | Work/milestones, dependency criteria, claim assessments, impact review, analysis/selection manifests, session handoffs | Progress distinguishes attempted from accepted work; corrections expose affected claims/figures |
| 4 — Agent release | Shared CLI/MCP contracts, approvals, limits, exports, operational health, end-to-end skill tests | Agent can resume a real project with no previous chat; no false save/completion or unauthorized mutation |
| 5 — Optional execution | Run import first; then immutable manifests, slots, outbox, adapters, leases, reconciliation | Crash/partition/duplicate-dispatch tests pass; physical uncertainty cannot cause unsafe resource reuse |
| 6 — Optional scale/integrations | Embeddings, trackers, scheduler adapters, interchange export, alternate DB only when justified | Measured improvement on the same query/task set without safety or citation regressions |

Phases 1–4 already form a complete usable knowledge base. Do not postpone sources, claims, history, or agent behavior until after a launcher. A knowledge-first release does not depend on implementing every optional execution table.

Required implementation artifacts before calling a build ready: tested migrations; type/JSON schemas; relation and transition registries; semantic tool schemas; provenance/selection rules; fixture imports; retrieval gold cases; permission tests; consistent backup/restore tooling; and a documented supported runtime. Generate human and machine reference material from the same schema and vocabulary; a renamed status or operation must update examples, validators, and tests together.

Separate the reusable skill from the runtime application, for example `src/research_kb/{domain,service,storage,ingestion,retrieval,clients,transports,execution}` with `migrations/`, `skills/research-kb/`, `tests/`, `fixtures/`, and `docs/`. Avoid one enormous CLI module, duplicated validation logic, and direct database writes from launchers.

## 7. Audit findings that must not recur

These corrections came from auditing the earlier v2 draft. Treat each as a design test.

Critical:

1. **Mutable specifications.** An incrementing `spec_version` on a mutable experiment row loses history. Store immutable full specification revisions; every trial/run references a real revision.
2. **Promised but unsupported historical state.** Do not expose `retrieve_state_at()`-style queries until full record revisions, historical relation state, and a monotonic transaction cursor exist.
3. **Deferred core evidence.** Claims, sources, artifacts, and evidence graphs are core. Give them first-class typed identities and evidence relationships before adding optional modules.
4. **Dangling references.** `scope_type/scope_id` and JSON `source_refs` permit wrong-type or missing targets. Use foreign-key-backed identities, typed edges, and exact source anchors.
5. **Missing skill behavior.** Specify packaging, activation/non-activation, session checkpoints, and unavailable-tool behavior; ship a short entry point with directly linked procedures and evaluation cases.
6. **Unsafe launch path.** Resource allocation and process creation need a transactional intent/outbox, executor receipts, idempotent dispatch where supported, and reconciliation.
7. **Lease expiry treated as proof of termination.** Quarantine uncertain allocations; verify termination before physical reuse.

High priority:

- Keep the model domain-neutral; a complete research KB is not only ML experiments.
- Make work typed with completion criteria, ownership, and dependency conditions rather than prose.
- Store comparability as a target-specific comparison assessment; remove `incomparable` from the global run-validity axis.
- Separate attribution, review state, evidence assessment, and applicability; do not collapse them into one `confidence` label.
- Use versioned selection/aggregation rules declared before outcomes are examined; never choose by favorable result.
- Revalidate condition identity and comparison eligibility after configuration changes such as batch size.
- Separate content/version identity from storage location and availability; a URL is not an identity.
- State method-specific provenance requirements; deterministic methods may declare randomness `not_applicable`.
- Permit a narrow, authorized diagnostic exception when a blocker prevents the work needed to resolve it.
- Stage ingestion with precise anchors, attribution, deduplication, review, and import receipts.
- Store full snapshots; an audit diff or short hash is not a complete historical representation.
- Require expected-revision checks, atomic batches, and conflicting-payload idempotency rejection.
- SQL records what state is registered; evidence and review determine what is supported.
- Enforce permissions in the controller; restricting the agent to semantic tools is not an authorization design.
- Treat imported text as data; maintain a prompt-injection boundary.
- Expand blockers and counterevidence through mandatory structured queries; never let top-k retrieval omit them.
- Filter revisions at the requested cursor so future corrections cannot leak into historical answers.

## 8. Maintainer checklist

Run these checks before merging a design or schema change:

1. Name the authority for every new stored field; confirm nothing derived became editable.
2. Confirm new references are project-scoped, kind-checked, and revision-pinned where the [data model](02-data-model.md) requires.
3. Confirm the change creates an immutable revision rather than mutating history; verify `expected_revision` handling.
4. Confirm the change does not turn registered state into a scientific-truth claim.
5. Confirm imported or retrieved text is never executed, followed, or used to rewrite policy.
6. Confirm the relevant phase gate above still passes and the validation invariants in [safety and operations](07-safety-and-operations.md) are exercised.
7. Confirm agent-facing behavior is documented in [tool contracts](08-tool-contracts.md) and tested via [skill and delivery](09-skill-and-delivery.md).
8. For design provenance and external references, consult [sources](10-sources.md); for operational retrieval behavior, consult [retrieval](04-retrieval.md), and for execution changes, [execution](06-execution.md).
