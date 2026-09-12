# Architecture

This runtime follows `research_kb_plan_v3.md` §1–§5 and the skill references
(`skills/research-kb/references/01-design-and-audit.md`, `02-data-model.md`).

## Layers

| Layer | Owns | Code |
|---|---|---|
| Agent skill | When to retrieve/capture, which procedure, how to interpret responses | `skills/research-kb/` |
| Knowledge-base runtime | Persistence, schemas, authorization, transactions, revision history, queries, validation, receipts | `src/research_kb/` |
| Project content | Research records, evidence, sources, work state, conventions | SQLite + blobs under `.research/state/<project-id>/` |

## Request path

```text
CLI / Python / MCP
  -> clients/cli.py | clients/python_api.py | transports/mcp_server.py
  -> service/api.py (envelope, errors)
  -> service/{operations,objects,work,assessment,sources,retrieval,proposals,...}.py
  -> domain/{vocab,schemas,relation_registry,transition_registry,...}.py
  -> storage/{db,migrations,repo,search,blobs}.py -> research.db + content-addressed blobs
```

All transports invoke the same domain-service functions; none bypass validation, expected-revision
checks, authorization, or the audit event.

## Storage

- One SQLite database per project, on a local filesystem, with foreign keys enabled on every
  connection and `BEGIN IMMEDIATE` for contested writes. When the SQLite build lacks the WAL-reset
  fix, the runtime falls back to `journal_mode=DELETE` instead of an unpatched WAL deployment.
- `objects` + `revisions` store full immutable snapshots; `objects.current_revision` is only a
  rebuildable head pointer. Link structure lives in `link_revisions`; citations are immutable children
  of a citing revision. Triggers reject updates/deletes on immutable tables.
- Large payloads (captured bytes, frozen extraction text) live in content-addressed blob trees;
  SQLite stores identities, manifests, and availability.
- `search_documents` + FTS5 external-content index are a rebuildable derived projection updated in the
  same transaction as mutations. Optional embedding vectors live in a cache keyed by content/model
  identity, with an outbox and watermark; lexical and semantic lists are combined by reciprocal rank
  fusion. Blocking applicability uses a restricted declarative rule language, never stored code.

## Historical state

Every commit appends a monotonic `commit_events` row. Revisions carry `recorded_seq`; historical
queries select the highest revision with `recorded_seq <= as_of_seq`. `occurred_at`/`effective_from`
describe when a reported event happened or a statement applied and are never used as ingestion time.

## Derived state

Review flags, FTS rows, embeddings (optional), Markdown exports, and cached summaries are derived and
rebuildable. Canonical refetch after search is mandatory; FTS rank is never evidence quality.

## Execution boundary

Execution is an optional module (`src/research_kb/execution/`). Launch intent is committed to
`dispatch_outbox` before any external call; leases coordinate resources; receipts and reconciliation
rules keep physical uncertainty explicit. Disabled unless the accepted policy enables it.
