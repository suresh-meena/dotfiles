# Operations

## Daily use

```bash
bin/rkb capabilities --json
bin/rkb --root /path/to/repo status --json
bin/rkb --root /path/to/repo context --mode question --query "..." --json
bin/rkb --root /path/to/repo propose --file ops.json --dry-run --json
bin/rkb --root /path/to/repo propose --file ops.json --json          # store
bin/rkb --root /path/to/repo approve --proposal ID --json            # reviewer
bin/rkb --root /path/to/repo apply --proposal ID --approval-token T --json
bin/rkb --root /path/to/repo changes --after CURSOR --json
```

Low-risk captures may auto-apply under the conservative policy. High-risk operations (evidence
assessment, issue resolution, supersession, tombstone, selection manifests, policy changes, execution)
require a stored proposal plus a bounded approval.

## Maintenance

| Task | Command | Notes |
|---|---|---|
| Apply pending migrations | `rkb migrate` | Explicit; opening a newer schema is refused |
| Rebuild FTS | `rkb fts-rebuild` | From canonical revisions; reports before/after consistency |
| Quarantine expired leases | `rkb lease-quarantine` | Expiry is not proof a process stopped |
| Verify integrity/readiness | `rkb verify --checks integrity,references,citations,index` | Findings only; no silent fixes |
| Backup | `rkb backup --tag manual` | SQLite online backup API + blobs + manifest |
| Verify backup | `rkb backup-verify --dir BACKUP` | Integrity, keys, sampled blob hashes |
| Restore | `rkb restore --source BACKUP --destination DIR` | Read-only reconciliation mode |
| Export | `rkb export --format markdown\|jsonl\|rocrate\|draft --output PATH` | Marked `GENERATED — NOT CANONICAL` |
| Embeddings | `rkb embeddings status`, `rkb embeddings drain` | Optional; disabled by default, degraded status reported |
| Search page | `rkb search QUERY --page CURSOR` | Snapshot-bound keyset pagination (offset mode when fusion is on) |
| Legacy inventory | `rkb legacy-inventory --db V2.db` | Read-only; no automatic migration |
| Blob GC | `rkb gc [--apply] [--grace SECONDS]` | Dry-run by default; apply requires `administer` |
| Import receipts | `rkb receipts [--connector NS]` | Per-item outcomes, failures, and cursor |
| Outbox | `rkb outbox` | Pending execution intents (reconcile, do not relaunch) |

## Backup and recovery objectives

- Target at most one hour of ordinary metadata loss; back up immediately after critical scientific
  approvals and before migrations. `maybe_backup_after_critical` does this automatically when a
  high-risk proposal applies.
- Backups include the database, source/extraction blobs, and the accepted policy profile. Keep an
  off-device encrypted copy and verify backup hashes periodically. An external path list is not a
  backup of the artifacts at those paths.
- Restore creates a new controller epoch; old cursors and launch tokens cannot be mistaken for current
  ones. Pending/dispatched outbox rows become dead letters, and active leases become quarantined. Run
  `rkb verify` and an operator review before enabling writes and dispatch.

## Editable draft workflow

Markdown/JSONL exports are read-only views. To edit and import back:

1. `rkb export --format draft --output draft.json` writes records with their exact `ref.revision`
   expected versions and a snapshot cursor.
2. Edit only `title`, `body_md`, `record_state`, and `state_json`; keep each `ref.revision` unchanged.
3. `rkb propose --file draft.json --draft --dry-run` returns per-record `draft_diffs` (changed fields)
   against the live head; no writes occur.
4. `rkb propose --file draft.json --draft` stores the corrections as ordinary proposals, which then go
   through the normal approval/apply path. A concurrent change makes the import fail with
   `REVISION_CONFLICT`; reconcile rather than forcing it.

## Ownership leases

`claim_work` and `release_work` coordinate active agents on ordinary work. A lease is a coordination
safeguard: expiry means ownership is stale, not that work failed or a process stopped, and it does not
grant permission to change claims or run expensive jobs.

## Degraded modes

| Failure | Behavior |
|---|---|
| Embeddings unavailable | Exact + FTS retrieval with explicit semantic degradation |
| FTS unhealthy | Exact reads and controlled source-range lookup; rebuild requested |
| Controller unavailable | Read a dated export; prepare uncommitted proposals; never claim current state |
| Artifact/source unavailable | Preserve the reference and report missing evidence |
| Version mismatch | Read-only compatible operations or a clear upgrade error |
| Context budget exceeded | Indispensable warnings plus continuation and incomplete status |
| Disk full | Abort the mutation; never report a saved note |

## Configuration

Routing lives at `.research/project.toml` (see `skills/research-kb/assets/project.example.toml`).
Runtime policy is stored per project under the state directory (`policy.json`) and versioned in
`policy_revisions`; updating it never retroactively changes what governed old runs or approvals.
Unknown routing or policy keys fail validation.
