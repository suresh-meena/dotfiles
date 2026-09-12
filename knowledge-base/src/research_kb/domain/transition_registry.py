from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from research_kb.domain.schemas import Bool, NonEmptyText, NullableText, REF, REF_LIST, StringList, validate_value
from research_kb.errors import schema_validation_failed

_ObjectRefList = REF_LIST


@dataclass(frozen=True)
class OperationSpec:
    name: str
    capability: str
    risk: str
    description: str
    payload_schema: dict[str, Any] = field(default_factory=dict)
    requires_review: bool = False
    enabled_by_default: bool = True

    @property
    def high_risk(self) -> bool:
        return self.risk == "high"


_CAPTURE_SCHEMA = {
    "type": "object",
    "required": ["kind", "subkind", "title", "state_json"],
    "properties": {
        "kind": NonEmptyText,
        "subkind": NonEmptyText,
        "title": {"type": "string", "maxLength": 300},
        "body_md": {"type": "string", "maxLength": 2_000_000},
        "state_json": {"type": "object"},
        "record_state": {"enum": ["draft", "active"]},
        "occurred_at": NullableText,
        "occurred_at_unknown": Bool,
        "effective_from": NullableText,
        "effective_to": NullableText,
        "citations": {"type": "array", "items": {"type": "object"}},
        "links": {"type": "array", "items": {"type": "object"}},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "attribution": {"type": "object"},
        "source_anchor_ids": StringList,
        "proposed_follow_up": StringList,
        "unresolved_references": {"type": "array", "items": {"type": "object"}},
    },
    "additionalProperties": False,
}

_REVISE_SCHEMA = {
    "type": "object",
    "required": ["ref"],
    "properties": {
        "ref": REF,
        "title": NullableText,
        "body_md": NullableText,
        "state_json": {"type": ["object", "null"]},
        "record_state": {"enum": ["draft", "active", "retired", "tombstoned", None]},
        "reason": NullableText,
        "citations": {"type": ["array", "null"], "items": {"type": "object"}},
        "reaffirm_citation_ids": StringList,
    },
    "additionalProperties": False,
}

_LINK_SCHEMA = {
    "type": "object",
    "required": ["predicate", "src_ref", "dst_ref"],
    "properties": {
        "predicate": NonEmptyText,
        "src_ref": REF,
        "dst_ref": REF,
        "pin_mode": {"enum": ["pinned", "tracking", None]},
        "qualifiers": {"type": "object"},
        "rationale": NullableText,
        "review_state": {"enum": ["unreviewed", "reviewed", "rejected", None]},
        "applicability": {"type": ["object", "string", "null"]},
    },
    "additionalProperties": False,
}

_ASSESS_SCHEMA = {
    "type": "object",
    "required": ["ref", "evidence_state", "review_state", "rationale", "assessed_revision"],
    "properties": {
        "ref": REF,
        "evidence_state": {"enum": ["untested", "provisional", "supported", "contested", "refuted"]},
        "review_state": {"enum": ["unreviewed", "reviewed", "rejected"]},
        "rationale": NonEmptyText,
        "assessed_revision": {"type": "integer", "minimum": 1},
        "criterion_ref": {"oneOf": [REF, {"type": "null"}]},
        "support_refs": _ObjectRefList,
        "contradiction_refs": _ObjectRefList,
        "exclusions": StringList,
        "missing_checks": StringList,
        "assessor_note": NullableText,
    },
    "additionalProperties": False,
}

_WORK_STATE_SCHEMA = {
    "type": "object",
    "required": ["ref", "to_state"],
    "properties": {
        "ref": REF,
        "to_state": {"enum": ["open", "in_progress", "in_review", "done", "cancelled"]},
        "reason": NullableText,
        "evidence_refs": REF_LIST,
        "completion_report": {"type": ["object", "string", "null"]},
        "blocked_reason": NullableText,
    },
    "additionalProperties": False,
}

_RESOLVE_ISSUE_SCHEMA = {
    "type": "object",
    "required": ["ref", "resolution_ref", "criterion_met", "rationale"],
    "properties": {
        "ref": REF,
        "resolution_ref": REF,
        "criterion_met": NonEmptyText,
        "rationale": NonEmptyText,
        "evidence_refs": REF_LIST,
    },
    "additionalProperties": False,
}

_ACK_FLAG_SCHEMA = {
    "type": "object",
    "required": ["flag_id", "rationale"],
    "properties": {
        "flag_id": {"type": "integer", "minimum": 1},
        "rationale": NonEmptyText,
        "review_ref": {"oneOf": [REF, {"type": "null"}]},
    },
    "additionalProperties": False,
}

_HANDOFF_SCHEMA = {
    "type": "object",
    "required": ["summary", "next_step", "snapshot_cursor"],
    "properties": {
        "summary": NonEmptyText,
        "completed_refs": REF_LIST,
        "open_refs": REF_LIST,
        "pending_proposals": StringList,
        "in_flight_executions": StringList,
        "next_step": NonEmptyText,
        "snapshot_cursor": NonEmptyText,
        "unresolved_issues": StringList,
        "session_id": NullableText,
    },
    "additionalProperties": False,
}

_SOURCE_SCHEMA = {
    "type": "object",
    "required": ["subkind", "title", "version", "identity_assurance"],
    "properties": {
        "subkind": NonEmptyText,
        "title": NonEmptyText,
        "author": NullableText,
        "external_id": NullableText,
        "version": {"type": ["string", "object"]},
        "locator": NullableText,
        "mirror_locator": NullableText,
        "access_restrictions": NullableText,
        "publication_time": NullableText,
        "retrieval_time": NullableText,
        "blob_hash": NullableText,
        "byte_size": {"type": ["integer", "null"], "minimum": 0},
        "identity_assurance": {"enum": ["content_sha256", "manifest", "metadata_only", "unverified"]},
        "preservation": {"enum": ["local_copy_allowed", "metadata_only", "external_embedding_allowed", "restricted"]},
        "supplied_by": NullableText,
        "extracted_by": NullableText,
        "author_attribution_notes": NullableText,
        "known_omissions": StringList,
        "captured_path": NullableText,
        "anchors": {"type": "array", "items": {"type": "object"}},
        "aliases": StringList,
    },
    "additionalProperties": False,
}

_ARTIFACT_SCHEMA = {
    "type": "object",
    "required": ["subkind", "role", "content_identity", "assurance", "availability"],
    "properties": {
        "subkind": NonEmptyText,
        "role": NonEmptyText,
        "media_type": NullableText,
        "byte_size": {"type": ["integer", "null"], "minimum": 0},
        "content_identity": {"type": "object"},
        "assurance": {"enum": ["content_sha256", "manifest", "metadata_only", "unverified"]},
        "locations": {"type": "array", "items": {"type": "object"}},
        "producer_ref": {"oneOf": [REF, {"type": "null"}]},
        "input_lineage": REF_LIST,
        "availability": {"enum": ["available", "missing", "unverified", "restricted"]},
        "preservation_policy": NullableText,
        "results": {"type": "object"},
        "local_path": NullableText,
        "role_note": NullableText,
    },
    "additionalProperties": False,
}

_COMPARISON_SCHEMA = {
    "type": "object",
    "required": ["run_ref", "target_ref", "assessment", "dimensions", "rationale"],
    "properties": {
        "run_ref": REF,
        "target_ref": REF,
        "assessment": {"enum": ["eligible", "ineligible", "needs_review", "not_assessed"]},
        "dimensions": {"type": "array", "items": {"type": "object"}},
        "permitted_differences": StringList,
        "rationale": NonEmptyText,
        "checker": NullableText,
    },
    "additionalProperties": False,
}

_SELECTION_SCHEMA = {
    "type": "object",
    "required": ["target_ref", "input_refs", "inclusion_rule", "generated_artifact_hashes"],
    "properties": {
        "target_ref": REF,
        "claim_refs": REF_LIST,
        "input_refs": REF_LIST,
        "inclusion_rule": NonEmptyText,
        "selection_mode": {"enum": ["earliest_valid_attempt", "best_metric", "aggregate", None]},
        "analysis_plan_ref": {"oneOf": [REF, {"type": "null"}]},
        "exclusions": {"type": "array", "items": {"type": "object"}},
        "exclusion_reasons": StringList,
        "code_identity": {"type": ["object", "string", "null"]},
        "environment_identity": {"type": ["object", "string", "null"]},
        "generated_artifact_hashes": StringList,
        "review_refs": REF_LIST,
    },
    "additionalProperties": False,
}

_STUDY_STATE_SCHEMA = {
    "type": "object",
    "required": ["ref", "to_state"],
    "properties": {
        "ref": REF,
        "to_state": {"enum": ["planned", "active", "concluded", "abandoned"]},
        "evidence_refs": REF_LIST,
        "conclusion": {"type": ["object", "string", "null"]},
        "reason": NullableText,
    },
    "additionalProperties": False,
}

_RETIRE_SCHEMA = {
    "type": "object",
    "required": ["ref", "superseded_by_ref", "reason"],
    "properties": {
        "ref": REF,
        "superseded_by_ref": REF,
        "reason": NonEmptyText,
        "scope": {"type": ["object", "string", "null"]},
    },
    "additionalProperties": False,
}

_TOMBSTONE_SCHEMA = {
    "type": "object",
    "required": ["ref", "reason"],
    "properties": {
        "ref": REF,
        "reason": NonEmptyText,
        "impact_acknowledgment": NullableText,
    },
    "additionalProperties": False,
}

_EXECUTION_PREPARE_SCHEMA = {
    "type": "object",
    "required": ["study_ref", "trial"],
    "properties": {
        "study_ref": REF,
        "trial": {"type": "object"},
        "repo_root": NullableText,
        "limits": {"type": "object"},
        "budget": {"type": ["object", "number", "null"]},
        "walltime_seconds": {"type": ["integer", "null"], "minimum": 1},
    },
    "additionalProperties": False,
}

_EXECUTION_LAUNCH_SCHEMA = {
    "type": "object",
    "required": ["execution_id"],
    "properties": {
        "execution_id": NonEmptyText,
        "approval_id": NullableText,
        "adapter": {"enum": ["local_process", "slurm", "import_only", None]},
    },
    "additionalProperties": False,
}

_EXECUTION_CANCEL_SCHEMA = {
    "type": "object",
    "required": ["execution_id"],
    "properties": {
        "execution_id": NonEmptyText,
        "reason": NullableText,
    },
    "additionalProperties": False,
}

_EXECUTION_RECONCILE_SCHEMA = {
    "type": "object",
    "required": ["execution_id", "phase", "payload"],
    "properties": {
        "execution_id": NonEmptyText,
        "phase": {"enum": ["accepted", "rejected", "running", "terminal", "ambiguous"]},
        "payload": {"type": "object"},
        "external_ref": NullableText,
        "generation": {"type": ["integer", "null"]},
    },
    "additionalProperties": False,
}


_CLAIM_WORK_SCHEMA = {
    "type": "object",
    "required": ["ref"],
    "properties": {
        "ref": REF,
        "expected_revision": {"type": ["integer", "null"], "minimum": 1},
        "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
    },
    "additionalProperties": False,
}

_RELEASE_WORK_SCHEMA = {
    "type": "object",
    "required": ["lease_id"],
    "properties": {
        "lease_id": NonEmptyText,
        "reason": NullableText,
    },
    "additionalProperties": False,
}

_POLICY_CHANGE_SCHEMA = {
    "type": "object",
    "required": ["profile"],
    "properties": {
        "profile": {"type": "object"},
        "reason": NullableText,
    },
    "additionalProperties": False,
}

OPERATIONS: dict[str, OperationSpec] = {
    "capture": OperationSpec(
        "capture", "capture", "low", "Capture a new attributed record.", _CAPTURE_SCHEMA
    ),
    "revise": OperationSpec(
        "revise", "propose", "low", "Create a new revision of an existing object.", _REVISE_SCHEMA
    ),
    "link": OperationSpec(
        "link", "propose", "low", "Create a typed relationship object.", _LINK_SCHEMA
    ),
    "assess_evidence": OperationSpec(
        "assess_evidence",
        "assess_evidence",
        "high",
        "Record a reviewed evidence assessment against a pinned claim/knowledge revision.",
        _ASSESS_SCHEMA,
        requires_review=True,
    ),
    "set_work_state": OperationSpec(
        "set_work_state", "update_owned_work", "low", "Transition a work record.", _WORK_STATE_SCHEMA
    ),
    "resolve_issue": OperationSpec(
        "resolve_issue",
        "resolve_critical",
        "high",
        "Resolve an issue when its resolution criterion is met.",
        _RESOLVE_ISSUE_SCHEMA,
        requires_review=True,
    ),
    "acknowledge_flag": OperationSpec(
        "acknowledge_flag",
        "update_owned_work",
        "low",
        "Acknowledge a derived review flag with a justified record.",
        _ACK_FLAG_SCHEMA,
    ),
    "create_handoff": OperationSpec(
        "create_handoff", "capture", "low", "Record a session handoff.", _HANDOFF_SCHEMA
    ),
    "register_source": OperationSpec(
        "register_source", "capture", "low", "Register a source version with anchors.", _SOURCE_SCHEMA
    ),
    "register_artifact": OperationSpec(
        "register_artifact", "capture", "low", "Register an artifact with content identity.", _ARTIFACT_SCHEMA
    ),
    "assess_comparison": OperationSpec(
        "assess_comparison",
        "assess_evidence",
        "high",
        "Record comparability of a run relative to a target protocol.",
        _COMPARISON_SCHEMA,
        requires_review=True,
    ),
    "create_selection_manifest": OperationSpec(
        "create_selection_manifest",
        "approve_selection",
        "high",
        "Freeze a paper table/figure selection manifest.",
        _SELECTION_SCHEMA,
        requires_review=True,
    ),
    "set_study_state": OperationSpec(
        "set_study_state", "update_owned_work", "low", "Transition a study lifecycle.", _STUDY_STATE_SCHEMA
    ),
    "retire": OperationSpec(
        "retire",
        "resolve_critical",
        "high",
        "Retire a record via explicit supersession.",
        _RETIRE_SCHEMA,
        requires_review=True,
    ),
    "tombstone": OperationSpec(
        "tombstone", "administer", "high", "Tombstone a record without destroying history.", _TOMBSTONE_SCHEMA
    ),
    "execute_prepare": OperationSpec(
        "execute_prepare",
        "launch",
        "high",
        "Prepare a launch: claim the trial attempt and write the outbox intent.",
        _EXECUTION_PREPARE_SCHEMA,
        requires_review=True,
        enabled_by_default=False,
    ),
    "execute_launch": OperationSpec(
        "execute_launch",
        "launch",
        "high",
        "Dispatch a prepared execution intent.",
        _EXECUTION_LAUNCH_SCHEMA,
        requires_review=True,
        enabled_by_default=False,
    ),
    "execute_cancel": OperationSpec(
        "execute_cancel",
        "cancel",
        "high",
        "Request cancellation of an execution.",
        _EXECUTION_CANCEL_SCHEMA,
        requires_review=True,
        enabled_by_default=False,
    ),
    "execute_reconcile": OperationSpec(
        "execute_reconcile",
        "launch",
        "low",
        "Reconcile an execution outcome from a receipt.",
        _EXECUTION_RECONCILE_SCHEMA,
        enabled_by_default=False,
    ),
    "claim_work": OperationSpec(
        "claim_work",
        "update_owned_work",
        "low",
        "Acquire a short ownership lease for active work coordination.",
        _CLAIM_WORK_SCHEMA,
    ),
    "release_work": OperationSpec(
        "release_work",
        "update_owned_work",
        "low",
        "Release an ownership lease explicitly.",
        _RELEASE_WORK_SCHEMA,
    ),
    "policy_change": OperationSpec(
        "policy_change",
        "administer",
        "high",
        "Accept a new reviewed policy profile revision.",
        _POLICY_CHANGE_SCHEMA,
        requires_review=True,
    ),
}


def get_operation(name: str) -> OperationSpec:
    spec = OPERATIONS.get(name)
    if spec is None:
        raise schema_validation_failed(
            f"Unsupported operation: {name}",
            allowed=sorted(OPERATIONS),
        )
    return spec


def validate_operation_payload(name: str, payload: dict[str, Any]) -> None:
    spec = get_operation(name)
    validate_value(spec.payload_schema, payload, f"operations[{name}]")


def capability_for(name: str) -> str:
    return get_operation(name).capability


def operation_document() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "capability": spec.capability,
            "risk": spec.risk,
            "requires_review": spec.requires_review,
            "enabled_by_default": spec.enabled_by_default,
            "description": spec.description,
            "payload_schema": spec.payload_schema,
        }
        for spec in OPERATIONS.values()
    ]
