# Data model: identities, revisions, and typed links

This reference defines the identity, revision, time, review, link, applicability, and impact rules that every record and mutation must follow, and it states explicitly what the database does and does not establish scientifically.

## 1. Identity rules

- Use server-generated UUIDs as canonical project, object, relation, and source-anchor identities.
- Human labels such as `K041`, `C012`, `E017`, and `R083` are project-scoped aliases, never global primary keys. Never reuse an alias after withdrawal.
- Resolve an ambiguous alias to candidates rather than guessing. Store the alias-to-object mapping with its namespace and normalization.
- Every API reference contains `project_id` and `object_id`. References used as evidence also contain an exact positive `revision`.
- A display string such as `K041@3` abbreviates that tuple; it is not an independent identifier and must not be parsed as one.
- Entity identity and version identity are different. Editing creates a new revision of the same object only when it remains the same research question/assertion lineage.
- A distinct competing claim is a new object linked by `contradicts` or `related_to`. Never collapse competing interpretations through deduplication.
- All cross-object references are project-scoped. A reference must not resolve to another project's object merely because the ID exists; foreign keys and domain checks enforce this.

## 2. Common record contract

Every durable research object is stored through an identity table plus an immutable revision table. Type-specific fields are schema-validated; important references are normalized into relational link/citation tables, not hidden in arbitrary JSON.

| Field | Contract |
|---|---|
| `project_id`, `object_id` | Existing namespace and immutable object identity |
| `kind` | Immutable family: `project`, `knowledge`, `claim`, `source`, `artifact`, `work`, `study`, `run`, `resource`, `link`, or `handoff` |
| `revision` | Positive integer, increased by one on each accepted change to this object |
| `schema_version` | Version of the kind/subkind payload contract |
| `title` | Short, human-readable title; never used as identity |
| `body_md` | Canonical prose/equations; empty only for types whose content is wholly structured |
| `record_state` | `draft`, `active`, `retired`, or `tombstoned`; not a scientific truth label |
| `state_json` | Validated type-specific fields, without unvalidated foreign references |
| `recorded_seq` | Commit event sequence assigned by the controller |
| `recorded_at` | Controller UTC timestamp of that commit |
| `occurred_at` | Optional time of the observation/action being reported; may be unknown |
| `effective_from`, `effective_to` | Optional applicability interval, half-open `[from,to)`; not ingestion time |
| `content_hash` | Hash of this revision's owned content and citation bindings; a link revision also hashes its own endpoints/qualifiers |
| Attribution | Authenticated writer in the commit event; original speaker/author and extraction agent recorded separately |

Meaning rules:

- `record_state=active` means part of the current registered state, not "true," "reviewed," or "complete." A hypothesis can be active and untested; a failed study can be actively documented.
- Missing information is `null` or explicitly `unknown` as defined by the schema. Zero, an empty array, and "not applicable" are not interchangeable.
- Record why a normally required field is not applicable. Do not invent timestamps, units, seeds, confidence scores, or authors to satisfy a schema.

## 3. Record kinds and knowledge subkinds

Top-level kinds: `project`, `knowledge`, `claim`, `source`, `artifact`, `work`, `study`, `run`, `resource`, `link`, `handoff`. Type-specific subkinds where applicable: source (`paper`, `book`, `note`, `meeting_excerpt`, `message`, `web_page`, `code_document`, `dataset_documentation`, `result_report`), work (`task`, `goal`, `milestone`), study (`analytical`, `numerical`, `experimental`, `literature`), artifact (`dataset`, `figure`, `table`, `analysis_result`, `notebook_export`, `source_snapshot`, `environment_manifest`, `checkpoint`, `manuscript_target`).

The `knowledge` family uses validated subkinds: `idea`, `hypothesis`, `observation`, `interpretation`, `conclusion`, `negative_result`, `definition`, `assumption`, `derivation`, `method`, `decision`, `question`, `caveat`, `issue`.

| Subkind/group | Required content beyond the common record |
|---|---|
| Idea/hypothesis | Precise proposal; applicability; what would support or contradict it; evidence may be absent |
| Observation | What was observed; conditions; source/run/analysis evidence; no interpretation disguised as direct measurement |
| Interpretation/conclusion | Assertion, assumptions, applicability, supporting and opposing evidence, review state |
| Negative result | What failed or was not observed, parameter/protocol domain, detection limits or diagnostic evidence, exceptions |
| Definition | Meaning, symbol/name, notation namespace, units/domain, source or declaration of project convention |
| Assumption | Exact assumption, domain, consequences, status, and dependent claims/methods through links |
| Derivation | Statement, assumptions, coherent steps, intermediate results, conventions, gaps/checks, source/code provenance |
| Method | Procedure/version, prerequisites, inputs/outputs, applicability, validation checks, reproducibility references |
| Decision | Choice, alternatives, reasons, decision maker, applicability, evidence, reconsideration conditions |
| Question | Precise unresolved question, why it matters, dependencies, what constitutes an answer |
| Caveat/issue | Affected scope, severity, specific effect, blocking operations, resolution criterion, evidence |

Additional rules:

- `review_state=unreviewed|reviewed|rejected` is used where review is meaningful. For assertions needing an evidence assessment, use `evidence_state=untested|provisional|supported|contested|refuted` with rationale and reviewer attribution.
- These labels are project assessments, not universal scientific facts. Never assign an unexplained numeric confidence score.
- A derivation is not split into one object per algebraic line. Preserve a coherent derivation document, then give independently reused lemmas or assumptions stable identities and anchors. Preserve LaTeX exactly in the canonical body; search normalization is separate.
- Claims are first-class because papers and goals depend on their evidence. A claim contains its precise statement, domain/applicability, quantifiers, assumptions, evidence criteria, and present assessment. Do not hide a claim only inside an experiment's free-text field.
- Evidence criteria are explicit, for example an analytical derivation under named assumptions, a specified comparison with defined uncertainty, independent reproduction, or a combination. The criterion's revision is part of every assessment.
- An evidence assessment records the reviewed claim revision, support and contradiction links, exclusions, missing checks, assessor, and rationale. A paper-ready assessment must not depend on unresolved critical blockers, unreviewed result selection, or inaccessible indispensable evidence. Support scores and vote counts are not substitutes for an assessment.

## 4. Revision semantics

- A mutation must supply `expected_revision`; creation uses `0`.
- The service opens a short write transaction, checks the current revision, writes the next full snapshot, updates the head pointer, records links/citations, appends the audit event, updates synchronous search projections, and commits the receipt together.
- A conflicting revision returns `REVISION_CONFLICT` with the actual version and a small authorized diff. Reread and decide whether the change still applies; never blindly retry with the new revision number.
- Immutable revision rows are not updated or deleted through normal application operations. Protect them with database triggers and permissions, with an explicit maintenance path for approved migrations or redaction.
- A hash chain alone is not tamper-proof against the database owner. Do not present it as such.
- Meaningful revisions are full snapshots rather than only text diffs. Prior body text, metadata, citation anchors, and relation state remain recoverable. Retain diffs as convenience output, not as the only record of history.
- Machine heartbeats and other high-frequency observations are separate and do not create scientific revisions.
- A link is itself a revisioned object: structural endpoints are stored once; its ordinary revision stores scope, rationale, review, and lifecycle. Citations are immutable children of the citing revision; rebuilding an object's current view must include them.

## 5. Two meanings of time

- `recorded_at`/`recorded_seq` answer **what the system knew then**. `occurred_at`/`effective_from` answer **when the reported event happened or a statement applied**.
- A correction imported today about last month's work must not appear in an answer about what was known last month.
- Use `as_of_seq` as the precise snapshot boundary. Controller wall time is a recorded logical commit timestamp, not a guarantee of an externally measured subsecond instant; use the cursor for exact ordering.
- `as_of_time` first resolves to the greatest committed sequence whose controller timestamp is at or before that time. Sequence numbers are monotonic but need not be contiguous.
- Keep controller timestamps nondecreasing for this mapping. Log a clock anomaly instead of allowing a backwards clock jump to reorder history.
- For each object at a historical cursor, select its highest revision with `recorded_seq <= as_of_seq`. Apply the same rule to link objects, reviews, blocker resolutions, and selection manifests.
- Use one read transaction for the whole context response. A current head pointer is a rebuildable convenience, not the historical query mechanism.
- An optional `effective_at` further filters applicability within the selected historical state. Unknown effective dates remain unknown.
- Current access permissions still apply to historical retrieval; an old cursor must not restore revoked access.

## 6. Review, evidence, and supersession

- Changing wording without changing meaning still creates a revision. Evidence links pin the old revision and do not silently transfer to new wording. A reviewer can record that a set of links remains applicable to the new revision, producing new reviewed links.
- Superseding one knowledge object with another requires a reason, an explicit `supersedes` relationship, and a revision of the older object's current state to `retired`. Retain both. Being newer is not sufficient reason to supersede a conflicting claim.
- A source author's reported result, a user's report, an agent inference, and an independently checked result are distinct provenance categories. Authenticated authorship establishes who said something, not that the statement is scientifically correct.
- Current retrieval uses actual resolution/supersession records and their scope, never a "last text wins" rule.

## 7. Typed relationship registry

Implement a small controlled registry rather than accepting arbitrary predicate strings. The registry specifies direction, permitted endpoint kinds, whether version pins are required, and how changes propagate.

| Predicate and direction | Version rule | Behavior |
|---|---|---|
| `about`: record -> topic/object | Usually tracking identities | Organizes scope without claiming evidence |
| `supports`: evidence -> claim/knowledge | Both endpoints pinned | Adds scoped support; does not by itself approve a claim |
| `contradicts`: evidence/claim -> claim/knowledge | Both endpoints pinned | Preserves explicit opposing evidence; no automatic winner |
| `derived_from`: output/interpretation -> input | Both endpoints pinned | Propagates review needs after invalidation or relevant replacement |
| `assumes`: claim/method/derivation -> assumption | Both endpoints pinned | Makes assumptions inspectable and impact-traversable |
| `supersedes`: replacement -> replaced record | Both endpoints pinned; reviewed | Retires the replaced interpretation in the stated domain |
| `depends_on`: work/study -> prerequisite | Tracking or pinned, explicitly declared | Readiness depends on a specified criterion, not mere object existence |
| `blocks`: issue/caveat -> target | Target identity/revision plus applicability | Blocks named operations under explicit policy |
| `resolves`: evidence/decision -> issue/question | Both endpoints pinned | Requires satisfaction of the resolution criterion |
| `produced_by`: artifact -> run/study/analysis | Both endpoints pinned | Tracks production identity |
| `uses`: run/analysis/method -> code/data/protocol | Both endpoints pinned | Records exact inputs, not a directory name |
| `included_in`: evidence/artifact/claim -> manuscript target | Both endpoints pinned | Enables figure/table/claim audit |
| `related_to`: record -> record | Tracking permitted | Discovery only; no validity propagation |

Rules:

- A tracking link resolves its endpoint's revision at the query cursor. A pinned link always points to its recorded revision. Never implement those two semantics implicitly with the same nullable field and no declared mode.
- Evidence links include rationale, applicable domain, review status, and source of the assessment.
- A task dependency includes its satisfaction condition: for example `done_with_review`, `accepted_artifact_available`, or `claim_assessed_under_criteria`. Do not infer conditions from prose during every status query.
- Supporting a claim is not transitive by default. A citation to a review quoting another paper is indirect evidence unless the original has actually been checked.
- Deduplicate evidence by underlying source/run provenance, not by how many notes repeat it.
- Enforce acyclicity only where required: task prerequisites, supersession, and immutable data-production lineage. Knowledge relationships such as `related_to` may legitimately contain cycles.

## 8. Applicability and blockers

- Represent applicability with structured dimensions where possible: protocol revision, dataset/split, parameter domain, units, model/system size, numerical regime, hardware role, or manuscript target. Preserve a plain-language explanation alongside them.
- Blocking rules are a restricted declarative language: known fields, equality/set membership, and typed ranges with defined units. Never execute a stored Python/SQL expression or let a language model silently decide whether a critical blocker applies.
- Unknown applicability is visible and prevents a critical operation until resolved under policy.
- A project-level blocker applies project-wide only when that scope is explicit. A caveat about one old tokenizer/protocol must not block unrelated work.
- A diagnostic study intended to resolve a blocker may receive a narrow authorized exception naming the blocker, operation, study revision, reason, and expiry. It does not waive the blocker for paper conclusions.

## 9. Impact propagation

- When an input, assumption, result validity, source interpretation, protocol, or selected evidence changes, traverse only the relevant dependency/lineage predicates. Produce `needs_review` flags with the causing revision and affected path.
- Do not automatically declare all descendants scientifically false.
- A new upstream version does not invalidate an old result produced correctly under the old version; it can make that result unsuitable for the current target. Distinguish **historical validity**, **current applicability**, and **freshness of review**.
- Cache entries and generated summaries record their dependencies. Relevant changes invalidate those caches immediately or mark them stale before critical decisions can use them.
- Resolving a root issue does not automatically close downstream reviews; each affected conclusion/figure needs its own justified acknowledgment.
- Track lineage to a finite visited set with explicit traversal limits. A truncated impact analysis is incomplete, not "no more affected records."

## 10. What the database does and does not establish

- SQL is authoritative about **what the database records**. It cannot turn an unsupported conclusion into established science.
- Similarity scores and citation counts cannot override review, scope, or evidence quality.
- Registered state, review state, and evidence assessment are separate dimensions; report each explicitly.

Operational checklist before recording or revising any object:

1. Resolve the object by UUID or unambiguous alias; if the alias is ambiguous, return candidates instead of choosing.
2. Retrieve the current revision and relevant dependents before proposing a change.
3. Supply `expected_revision` and handle `REVISION_CONFLICT` by rereading, not by overwriting.
4. Choose revision vs new object correctly: same lineage -> revision; competing claim -> new object plus link.
5. Pin every evidence/derivation link to exact revisions; declare tracking links explicitly.
6. Record structured applicability and blockers; never encode blocking logic in prose.
7. For historical questions, select revisions at the requested `as_of_seq`; do not use current heads or current vectors.
8. Verify whether a change makes dependent reviews stale, and flag each affected record.

Related procedures: [capture and sources](03-capture-and-sources.md), [retrieval](04-retrieval.md), [progress and publication](05-progress-and-publication.md), [execution](06-execution.md), and the operation schemas in [tool contracts](08-tool-contracts.md).
