# Validation and evaluation

The plan's core invariant matrix (§22.1) maps to concrete tests as follows.

| Invariant | Test |
|---|---|
| Project isolation | `tests/test_capture.py::test_project_isolation`, `tests/test_proposals_auth.py::test_cross_project_reference_rejected` |
| Actual version references | `tests/test_storage.py::test_pinned_link_requires_revisions`, schema tests in `tests/test_domain.py` |
| Immutable history | `tests/test_storage.py::test_revisions_are_immutable`, `tests/test_capture.py::test_history_is_insert_only` |
| Concurrent-write safety | `tests/test_storage.py::test_two_connections_serialize_writes`, revision-conflict tests |
| Logical idempotency | `tests/test_capture.py::test_idempotent_replay_and_conflict` |
| Atomic mutation/audit | `tests/test_storage.py::test_commit_event_is_written_with_mutation` |
| Source fidelity | `tests/test_sources_import.py::test_anchor_hash_is_retained`, extraction and citation tests |
| Historical correctness | `tests/test_retrieval.py::test_history_as_of_does_not_leak_later_corrections` |
| Evidence review | `tests/test_work_evidence.py::test_evidence_assessment_requires_reviewer_and_blocks` |
| Counterevidence preservation | `tests/test_work_evidence.py::test_counterevidence_preserved_in_claim_evidence` |
| Work completion | `tests/test_work_evidence.py::test_work_state_transitions_and_completion_evidence` |
| Dependency consistency | dependency and cycle tests in `tests/test_work_evidence.py` |
| Search freshness | FTS tests in `tests/test_retrieval.py`, index checks in `rkb verify` |
| No false absence | `tests/test_retrieval.py::test_search_empty_does_not_claim_absence` |
| Secret/source boundary | validation tests plus the skill bundle's injection scenarios |
| Recoverability | `tests/test_exports_backup.py::test_backup_and_restore_read_only_reconciliation` |
| Execution failure injection | `tests/test_execution.py` (leases, quarantine, prepare/launch/reconcile, disabled default, duplicate receipts, ambiguous and late receipts) |
| Blocking-rule scope | `tests/test_completeness.py::test_blocking_rule_language`, `test_blocker_rule_scopes_assessment` |
| Crash atomicity | `tests/test_completeness.py::test_crash_before_commit_rolls_back`, `test_crash_after_commit_persists` |
| Fencing and reconciliation rules | `tests/test_completeness.py::test_stale_lease_generation_is_fenced`, `test_reconciliation_*` |
| Pagination honesty | `tests/test_completeness.py::test_search_pagination_covers_all_candidates` |
| Optional embeddings | `tests/test_completeness.py::test_embeddings_*`, `test_rrf_fusion_math` |
| Editable draft corrections | `tests/test_completeness.py::test_draft_*` |
| Ownership leases | `tests/test_completeness.py::test_ownership_leases` |
| Conflict diffs and critical backups | `tests/test_completeness.py::test_conflict_reports_small_diff`, `test_critical_backup_after_high_risk_apply` |
| Projection rebuild from canonical | `tests/test_completeness.py::test_fts_rebuild_reprojects_canonical_revisions` |
| Citation non-transfer and reaffirmation | `tests/test_plan_coverage.py::test_citations_do_not_transfer_silently_and_can_be_reaffirmed` |
| Effective-time applicability | `tests/test_plan_coverage.py::test_effective_at_filters_applicability` |
| Paper readiness | `tests/test_plan_coverage.py::test_paper_readiness_requires_current_review` |
| Import-root enforcement | `tests/test_plan_coverage.py::test_import_roots_are_enforced` |
| Policy key validation | `tests/test_plan_coverage.py::test_policy_unknown_keys_are_rejected` |
| Strict JSON parsing | `tests/test_plan_coverage.py::test_cli_rejects_duplicate_json_keys` |
| Coverage requires eligible evidence | `tests/test_completeness.py::test_coverage_counts_only_eligible_evidence` |
| Comparison consistency | `tests/test_completeness.py::test_comparison_assessment_conflict_rejected` |
| Selection manifest default rule | `tests/test_completeness.py::test_selection_manifest_records_default_selection` |
| Tokenizer literals | `tests/test_plan_coverage.py::test_tokenizer_handles_literals_and_notation` |
| RFC 8785 vectors | `tests/test_domain.py::test_rfc8785_published_number_vector`, `test_rfc8785_literals_and_string_escaping` |
| Artifact results contract | `tests/test_plan_coverage.py::test_artifact_results_contract_is_validated` |
| Blob GC safety | `tests/test_plan_coverage.py::test_gc_dry_run_and_permission` |

## Running

```bash
PYTHONPATH=src python3 -m pytest tests skills/research-kb/tests -q
```

The skill bundle tests live in `skills/research-kb/tests/test_bundle.py` and validate only the package,
its scripts, schemas, examples, and evals. They do not prove host compatibility or agent behavior.

## Retrieval evaluation

`fixtures/retrieval_gold.json` contains 45 questions over the synthetic corpus in
`fixtures/synthetic_project.json`. `tests/test_fixtures.py::test_retrieval_gold_set_recall` imports the
fixture explicitly, runs FTS-only search, and requires at least 90% of questions to have their expected
records in the top 10. This is a proposed initial gate, not a measured claim about a real project.

## Skill evaluation

`skills/research-kb/evals/scenarios.json` specifies positive-activation, negative-activation, and
failure-injection cases; `skills/research-kb/evals/rubric.md` defines scoring. Run fresh-agent sessions
with and without the skill and record activation accuracy, retrieval-before-assertion, attribution,
error recovery, and write verification. These entries are behavior specifications, not recorded
scores.

## Performance

No achieved performance numbers are claimed. Exact lookups and context assembly should be benchmarked
locally on a declared machine and corpus before advertising any latency targets.
