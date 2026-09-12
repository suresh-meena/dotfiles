# Implementation sequence

The plan's phases (§21) map to the code and tests as follows. Phases 1–4 form a complete, usable
knowledge base; execution and scale modules are optional.

| Phase | Delivered | Where |
|---|---|---|
| 0 — Contracts and fixtures | Vocabularies, schemas, relation/transition registries, skill entry point, synthetic fixtures and gold set | `src/research_kb/domain/`, `skills/research-kb/`, `fixtures/` |
| 1 — Durable knowledge | Project identities, immutable full revisions, typed links/citations, sources/anchors, capture/get, auth, idempotency, expected revisions, audit, consistent backups | `service/{objects,sources,proposals,backup,policy}.py`, `storage/`, `tests/test_capture.py`, `tests/test_storage.py`, `tests/test_exports_backup.py` |
| 2 — Useful retrieval | FTS projections, alias resolution, source ranges, exact status, historical retrieval, mandatory caveat/evidence expansion, context packaging | `storage/search.py`, `service/retrieval.py`, `tests/test_retrieval.py` |
| 3 — Progress and evidence | Work/milestones, dependency criteria, claim assessments, impact review, analysis/selection manifests, session handoffs | `service/{work,assessment}.py` and `service/retrieval.py`, `tests/test_work_evidence.py`, `tests/test_manuscript.py` |
| 4 — Agent release | CLI/MCP contracts, approvals, limits, exports, operational health, bundle tests | `clients/cli.py`, `transports/mcp_server.py`, `service/{exports,verify,api}.py`, `skills/research-kb/tests/` |
| 5 — Optional execution | Run import, immutable manifests, slots, outbox, adapters, leases, reconciliation | `execution/`, `tests/test_execution.py` |
| 6 — Optional scale/integrations | Embeddings cache, outbox with dead-letter, pluggable providers (disabled by default; local deterministic provider for offline tests), reciprocal rank fusion, interchange exports | `retrieval/embeddings.py`, `migrations/0006_embeddings.sql`, `service/exports.py` |

Session cursors and handoffs are implemented in `service/retrieval.py` (`changes` with explicit
acknowledgment) and the `create_handoff` operation.

## Gate status

- Every core acceptance question has an executable path: exact reads (`get`), evidence expansion
  (`context --mode claim_evidence`), history (`context --mode history`), progress (`status`,
  `context --mode next_work`), and sources (`context --mode source_read`).
- The invariant matrix is tested (see `docs/validation.md`).
- Execution launch is disabled by default and gated behind policy plus approvals.
- Embeddings remain disabled by default. When enabled, the cache is keyed by
  `(project, object, revision, projection hash, model identity, dimensions, normalization, chunker)`;
  external providers require explicit policy authorization and unembedded recent revisions stay visible
  to lexical retrieval with a degraded watermark.

## Extending

- New record subkind: add vocabulary, schema, transition operations, tests, and skill references
  together.
- New predicate: extend `domain/relation_registry.py`, endpoint rules, and tests.
- Database migration: add an ordered checksummed SQL file under `src/research_kb/migrations/` and a
  compatibility test; run `rkb migrate` explicitly.
