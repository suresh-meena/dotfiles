# Sources

Purpose: record what the v3 design rests on, distinguish externally verified facts from proposals, and specify how to treat the v2 draft and migrate legacy data.

Related references: [design and audit](01-design-and-audit.md), [data model](02-data-model.md), [capture and sources](03-capture-and-sources.md), [safety and operations](07-safety-and-operations.md), [skill and delivery](09-skill-and-delivery.md).

## The v2 file is a design, not project evidence

- Do not seed the live project with v2's illustrative E017/K041/R083 examples, sample machines, metrics, or timestamps. They are examples in a design document, not established facts about the user's project.
- Register the v2 document as a design source only when explicitly importing it. It is not evidence for any scientific claim.
- The source supplied for the v3 revision is `research_experiment_os_v2.md`, 1,702 logical lines in the supplied reader, SHA-256 `5200a79f01c90eb5ef89dba5a4054c16c15b478ba06aeb70f59cf14ff22025d5`. This fingerprint records exactly which draft was reviewed.
- Findings labeled D0 below are design analysis of that uploaded draft, not claims made by external sources.

## Legacy migration mapping

If a v2 database actually exists, inventory it read-only, preserve a consistent backup, and create an explicit mapping of old IDs/scopes/kinds to new project-scoped identities. Keep original IDs as aliases and import provenance.

| Legacy field/concept | Migration handling |
|---|---|
| Mutable experiment with `spec_version` | Recover real historical specs from preserved snapshots when available; otherwise record an explicitly unreconstructed legacy reference and block unsupported critical use |
| `knowledge_items` | Map subkind and preserve text/attribution; separate todo/work records and link them where needed |
| `scope_type/scope_id` | Resolve actual objects and create typed links; leave unresolved references in a draft/import report |
| `source_refs_json` | Convert verified references to pinned links/citations; do not invent versions or page anchors |
| `incomparable` validity | Preserve the legacy judgment; create a target-specific comparison assessment only when its target is known |
| Status fields | Map through an explicit vocabulary table and report unmappable states |
| Timestamps | Preserve original reported timestamps separately from new import-recording time |
| Audit diffs | Import as historical evidence; do not pretend they reconstruct missing full states |
| Artifact URIs | Register locations with unverified identity until manifests/checksums are established |

Additional migration rules:

- State the earliest point for which full historical reconstruction is supported.
- Never synthesize a false old specification from the latest experiment row.
- Rebuild indexes only after reference, count, and integrity checks pass.
- A migration is an import with receipts, not a rewrite of the source database.

## First-use workflow

1. Create the project namespace and a reviewed minimal policy.
2. Import a small useful set: current goals, important definitions and assumptions, active claims, the decisions preventing repeated work, major sources, open blockers, and the next few tasks.
3. Resolve citations for the conclusions that currently matter most.
4. Run orientation, evidence, history, and progress queries with a fresh agent.
5. Fix observed failures before expanding the ontology or adding embeddings.
6. Establish checkpoint/handoff and backup routines.
7. Add execution control only when imported provenance and the actual compute environment demonstrate that it is needed.

A successful deployment lets the researcher give a brief scientific update while the system supplies identity, provenance, retrieval, history, and verification. It must not replace missing evidence with confident prose or turn every thought into a bookkeeping task.

## D0 and external references

External documentation was consulted on 10 September 2026. Product discovery paths and dependency/security requirements should be rechecked when implementing or installing later.

| ID | Primary source | Used for |
|---|---|---|
| D0 | User-supplied `research_experiment_os_v2.md`, 1,702 logical lines in the supplied reader; SHA-256 `5200a79f01c90eb5ef89dba5a4054c16c15b478ba06aeb70f59cf14ff22025d5` | Original requirements, terminology, audit, and migration analysis |
| S1 | [Agent Skills — Specification](https://agentskills.io/specification) | Skill directory/frontmatter, references/scripts/assets, progressive disclosure |
| S2 | [OpenAI — Build skills](https://learn.chatgpt.com/docs/build-skills) (the consulted Codex skills URL redirects here) | Current local skill discovery and optional OpenAI host metadata |
| S3 | [Anthropic — Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) | Evaluation-driven authoring, concise procedures, reference organization |
| S4 | [SQLite — Write-Ahead Logging](https://www.sqlite.org/wal.html) | Same-host/single-writer limitations and published WAL-reset fix versions |
| S5 | [SQLite — FTS5 Extension](https://www.sqlite.org/fts5.html) | External-content index maintenance, trigger/backfill/rebuild behavior |
| S6 | [SQLite — Online Backup API](https://www.sqlite.org/backup.html) | Consistent live-database backup mechanism |
| S7 | [SQLite — Foreign Key Support](https://www.sqlite.org/foreignkeys.html) | Per-connection foreign-key enforcement |
| S8 | [SQLite — STRICT Tables](https://www.sqlite.org/stricttables.html) | Table typing, distinct from application JSON/domain validation |
| S9 | [W3C — PROV-DM](https://www.w3.org/TR/prov-dm/) | Entities, activities, agents, derivation and provenance vocabulary |
| S10 | [AiiDA — Provenance concepts](https://aiida.readthedocs.io/projects/aiida-core/en/stable/topics/provenance/concepts.html) | Typed provenance links; distinction between data and logical provenance |
| S11 | [MLflow — Architecture overview](https://mlflow.org/docs/latest/self-hosting/architecture/overview/) | Separation of metadata and large artifact storage |
| S12 | [MCP — Tools, specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) | Structured outputs, schemas, annotation trust, access-control requirements; an explicitly versioned reference |
| S13 | [AWS — Transactional outbox pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) | Database/external-side-effect boundary and duplicate-consumer handling |
| S14 | [Elastic — Reciprocal rank fusion](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion) | Combining independently ranked retrieval lists |
| S15 | [RFC 8785 — JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785) | Explicit canonical serialization and numerical representation constraints |
| S16 | [RO-Crate 1.2 specification](https://www.researchobject.org/ro-crate/specification/1.2/) | Optional research-object/package metadata interchange |
| S17 | [Claude Code — Extend Claude with skills](https://code.claude.com/docs/en/skills) | Current Claude Code skill installation/discovery locations |

## Proposal versus verified fact

- **D0** is analysis of the uploaded draft by the design author. It is not independently verified.
- **S1–S17** descriptions are externally verified facts from the cited documentation as consulted on 10 September 2026.
- **All other material** — normative requirements, architecture choices, example contracts, thresholds, evaluation gates, implementation phases, and the `rkb` interface — is a proposal in this revised design. It is not a claim that the cited systems already implement this exact architecture.
- All demonstrations use synthetic data.
- Where a reference informs a pattern, the design deliberately borrows only the useful pattern and does not import an entire external framework or claim that adopting one eliminates the need for project-specific evidence and workflow design.
- Citation of S1–S17 does not make the surrounding recommendation externally verified; only the specific external fact attributed to that source is.

The schema and record contracts that operationalize these decisions are in [data model](02-data-model.md); operational safety requirements in [safety and operations](07-safety-and-operations.md); packaging and evaluation in [skill and delivery](09-skill-and-delivery.md).
