# Migration from v2

The uploaded `research_experiment_os_v2.md` is a design document, not project evidence. Do not seed a
live project with its illustrative records, sample machines, metrics, or timestamps. Register it as a
design source only when explicitly importing it. Its reviewed fingerprint is recorded in
`skills/research-kb/references/10-sources.md`.

## If a v2 database exists

1. Inventory read-only: `rkb legacy-inventory --db V2.db --json`.
2. Preserve a consistent backup (`rkb backup` on the new store; copy the old file read-only).
3. Create an explicit mapping of old IDs/scopes/kinds to new project-scoped identities, keeping
   original IDs as aliases and import provenance. The mapping table lives in
   `src/research_kb/service/legacy.py::LEGACY_MAPPING`.
4. Import through typed operations so schemas, references, audit, and receipts apply. Do not write
   directly to canonical tables.
5. State the earliest point for which full historical reconstruction is supported. Never synthesize a
   false old specification from the latest mutable row.
6. Rebuild indexes only after reference, count, and integrity checks pass.

## Mapping summary

| Legacy field/concept | Handling |
|---|---|
| Mutable experiment with `spec_version` | Recover real historical specs from preserved snapshots when available; otherwise record an unreconstructed legacy reference and block unsupported critical use |
| `knowledge_items` | Map subkind and preserve text/attribution; split todo/work records and link them |
| `scope_type/scope_id` | Resolve actual objects and create typed links; leave unresolved references in a draft/import report |
| `source_refs_json` | Convert verified references to pinned links/citations; never invent versions or page anchors |
| `incomparable` validity | Preserve the legacy judgment; create a target-specific comparison assessment only when the target is known |
| Status fields | Map through an explicit vocabulary table and report unmappable states |
| Timestamps | Preserve reported timestamps separately from import-recording time |
| Audit diffs | Import as historical evidence; never pretend they reconstruct missing full states |
| Artifact URIs | Register locations with unverified identity until manifests/checksums exist |

## First-use workflow for a new project

1. `rkb init` creates the namespace, a draft project object, and the conservative policy.
2. Import a small useful set: current goals, important definitions/assumptions, active claims, the
   decisions that prevent repeated work, major sources, open blockers, and the next few tasks.
3. Resolve citations for the conclusions that currently matter most.
4. Run orientation, evidence, history, and progress queries with a fresh agent; fix observed failures
   before expanding the ontology or adding embeddings.
5. Establish checkpoint/handoff and backup routines. Enable execution only when imported provenance and
   the actual compute environment demonstrate that it is needed.
