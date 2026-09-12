# Fixtures

Synthetic data used by tests and evaluations. **Never silently import these records into a user
project.** A fixture import is an explicit operation, and `tests/test_fixtures.py` asserts a fresh
project contains no fixture records until one occurs.

- `synthetic_project.json` — 33 records (definitions, hypotheses, observations, claims, issues, tasks,
  decisions, a source note, an artifact with typed results, a study, and a resource) plus 15 typed
  links. Link references use `{"alias": ...}` placeholders that the test resolves to object IDs.
- `retrieval_gold.json` — 45 FTS questions with expected record aliases, used as the proposed 90%
  required-evidence recall gate.
- `generate_synthetic.py` — deterministic regenerator for both files (`python3
  fixtures/generate_synthetic.py`).

The synthetic corpus uses invented vocabulary on purpose so that retrieval expectations are meaningful
without reflecting any real project.
