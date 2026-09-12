from __future__ import annotations

from typing import Final

RECORD_KINDS: Final = (
    "project",
    "knowledge",
    "claim",
    "source",
    "artifact",
    "work",
    "study",
    "run",
    "resource",
    "link",
    "handoff",
)

KNOWLEDGE_SUBKINDS: Final = (
    "idea",
    "hypothesis",
    "observation",
    "interpretation",
    "conclusion",
    "negative_result",
    "definition",
    "assumption",
    "derivation",
    "method",
    "decision",
    "question",
    "caveat",
    "issue",
)

WORK_SUBKINDS: Final = ("task", "goal", "milestone")
STUDY_SUBKINDS: Final = ("analytical", "numerical", "experimental", "literature")
RUN_SUBKINDS: Final = ("attempt",)
SOURCE_SUBKINDS: Final = (
    "paper",
    "book",
    "note",
    "meeting_excerpt",
    "message",
    "web_page",
    "code_document",
    "dataset_documentation",
    "result_report",
)
ARTIFACT_SUBKINDS: Final = (
    "dataset",
    "figure",
    "table",
    "analysis_result",
    "notebook_export",
    "source_snapshot",
    "environment_manifest",
    "checkpoint",
    "manuscript_target",
)
HANDOFF_SUBKINDS: Final = ("session_handoff",)

SUBGENRES: Final = {
    "project": ("project",),
    "knowledge": KNOWLEDGE_SUBKINDS,
    "claim": ("claim",),
    "source": SOURCE_SUBKINDS,
    "artifact": ARTIFACT_SUBKINDS,
    "work": WORK_SUBKINDS,
    "study": STUDY_SUBKINDS,
    "run": RUN_SUBKINDS,
    "resource": ("machine", "gpu", "cpu", "scheduler"),
    "link": ("link",),
    "handoff": HANDOFF_SUBKINDS,
}

RECORD_STATES: Final = ("draft", "active", "retired", "tombstoned")
REVIEW_STATES: Final = ("unreviewed", "reviewed", "rejected")
EVIDENCE_STATES: Final = ("untested", "provisional", "supported", "contested", "refuted")
WORK_STATES: Final = ("open", "in_progress", "in_review", "done", "cancelled")
STUDY_STATES: Final = ("planned", "active", "concluded", "abandoned")
RUN_STATUSES: Final = ("queued", "starting", "running", "completed", "crashed", "cancelled", "lost")
RUN_VALIDITY: Final = ("unknown", "valid", "suspicious", "invalid")
COMPARISON_ASSESSMENTS: Final = ("eligible", "ineligible", "needs_review", "not_assessed")
PROVENANCE_CATEGORIES: Final = (
    "source_author_statement",
    "user_report",
    "agent_inference",
    "independently_checked",
    "adapter_observation",
)
ASSURANCE_LEVELS: Final = ("content_sha256", "manifest", "metadata_only", "unverified")
BLOCKER_SEVERITIES: Final = ("low", "medium", "high", "critical")
PRIORITIES: Final = ("low", "medium", "high", "critical")
DEPENDENCY_CONDITIONS: Final = (
    "exists",
    "done",
    "done_with_review",
    "accepted_artifact_available",
    "claim_assessed_under_criteria",
    "diagnostic_check_passed",
    "source_registered",
    "custom",
)

PREDICATES: Final = (
    "about",
    "supports",
    "contradicts",
    "derived_from",
    "assumes",
    "supersedes",
    "depends_on",
    "blocks",
    "resolves",
    "produced_by",
    "uses",
    "included_in",
    "related_to",
)

PREDICATE_DIRECTION: Final = {
    "about": ("record", "topic"),
    "supports": ("evidence", "claim"),
    "contradicts": ("evidence", "claim"),
    "derived_from": ("output", "input"),
    "assumes": ("consumer", "assumption"),
    "supersedes": ("replacement", "replaced"),
    "depends_on": ("dependent", "prerequisite"),
    "blocks": ("blocker", "target"),
    "resolves": ("resolution", "issue"),
    "produced_by": ("artifact", "producer"),
    "uses": ("consumer", "input"),
    "included_in": ("item", "target"),
    "related_to": ("any", "any"),
}

PREDICATE_PIN_RULE: Final = {
    "about": "tracking",
    "supports": "pinned_required",
    "contradicts": "pinned_required",
    "derived_from": "pinned_required",
    "assumes": "pinned_required",
    "supersedes": "pinned_required_reviewed",
    "depends_on": "either_declared",
    "blocks": "either_declared",
    "resolves": "pinned_required",
    "produced_by": "pinned_required",
    "uses": "pinned_required",
    "included_in": "pinned_required",
    "related_to": "tracking",
}

ACYCLIC_PREDICATES: Final = ("depends_on", "supersedes", "derived_from")

SOURCE_ANCHOR_KINDS: Final = (
    "markdown_text",
    "pdf_page",
    "code",
    "notebook",
    "message_excerpt",
    "table_cell",
    "web_page",
)

EXTRACTION_STATUSES: Final = (
    "extracted",
    "metadata_only",
    "unsupported",
    "unavailable",
    "needs_visual_check",
    "ocr_uncertain",
)

CAPABILITY_GROUPS: Final = ("reader", "contributor", "reviewer", "operator", "administrator")
CAPABILITIES: Final = (
    "read",
    "capture",
    "propose",
    "update_owned_work",
    "assess_evidence",
    "accept_conclusions",
    "resolve_critical",
    "approve_selection",
    "launch",
    "cancel",
    "administer",
)

ROLE_CAPABILITIES: Final = {
    "reader": ("read",),
    "contributor": ("read", "capture", "propose", "update_owned_work"),
    "reviewer": (
        "read",
        "capture",
        "propose",
        "update_owned_work",
        "assess_evidence",
        "accept_conclusions",
        "resolve_critical",
        "approve_selection",
    ),
    "operator": (
        "read",
        "capture",
        "propose",
        "update_owned_work",
        "launch",
        "cancel",
    ),
    "administrator": CAPABILITIES,
}

HIGH_RISK_OPERATIONS: Final = frozenset(
    {
        "assess_evidence",
        "accept_conclusion",
        "resolve_critical",
        "invalidate_evidence",
        "tombstone",
        "execute_launch",
        "execute_cancel",
        "policy_change",
        "supersede",
        "approve_selection",
    }
)

WORK_TRANSITIONS: Final = {
    "open": ("in_progress", "cancelled", "in_review", "done"),
    "in_progress": ("in_review", "done", "cancelled", "open"),
    "in_review": ("done", "in_progress", "open", "cancelled"),
    "done": ("in_progress", "open"),
    "cancelled": ("open",),
}

STUDY_TRANSITIONS: Final = {
    "planned": ("active", "abandoned"),
    "active": ("concluded", "abandoned"),
    "concluded": (),
    "abandoned": (),
}

RUN_TRANSITIONS: Final = {
    "queued": ("starting", "cancelled", "lost"),
    "starting": ("running", "crashed", "cancelled", "lost"),
    "running": ("completed", "crashed", "cancelled", "lost"),
    "completed": (),
    "crashed": (),
    "cancelled": (),
    "lost": ("completed", "crashed", "cancelled"),
}


CITATION_ROLES: Final = ("quote", "paraphrase", "evidence", "formula", "table_value", "figure")

PROPOSAL_STATES: Final = ("stored", "applied", "rejected", "expired", "superseded")


CONTEXT_MODES: Final = ("lookup", "question", "claim_evidence", "history", "progress", "next_work", "source_read")

CHANGE_ACTIONS: Final = (
    "pending",
    "project_bootstrap",
    "capture",
    "proposal_validate",
    "revise",
    "retire",
    "tombstone",
    "link",
    "unlink",
    "citation",
    "assessment",
    "work_state",
    "review_flag",
    "review_resolution",
    "source_register",
    "import",
    "selection_manifest",
    "handoff",
    "proposal_store",
    "proposal_apply",
    "approval",
    "execution_prepare",
    "execution_dispatch",
    "execution_receipt",
    "resource_lease",
    "policy_accept",
    "migration",
    "maintenance",
)

GENERATED_NOTICE = "GENERATED — NOT CANONICAL"
