from __future__ import annotations

import re
from typing import Any, Callable

from research_kb.domain.vocab import (
    ARTIFACT_SUBKINDS,
    ASSURANCE_LEVELS,
    BLOCKER_SEVERITIES,
    EVIDENCE_STATES,
    EXTRACTION_STATUSES,
    HANDOFF_SUBKINDS,
    PRIORITIES,
    PROVENANCE_CATEGORIES,
    REVIEW_STATES,
    RUN_STATUSES,
    RUN_SUBKINDS,
    RUN_VALIDITY,
    SOURCE_SUBKINDS,
    STUDY_STATES,
    STUDY_SUBKINDS,
    WORK_STATES,
    WORK_SUBKINDS,
)
from research_kb.errors import schema_validation_failed

Text = {"type": "string"}
NonEmptyText = {"type": "string", "minLength": 1}
NullableText = {"type": ["string", "null"]}
Bool = {"type": "boolean"}
Int = {"type": "integer"}
Number = {"type": "number"}
StringList = {"type": "array", "items": {"type": "string"}}
ObjectField = {"type": "object"}

REF = {
    "type": "object",
    "required": ["object_id"],
    "properties": {
        "project_id": NullableText,
        "object_id": NonEmptyText,
        "revision": {"type": ["integer", "null"], "minimum": 1},
    },
    "additionalProperties": False,
}

REF_LIST = {"type": "array", "items": REF}

RESULT_ENTRY = {
    "type": "object",
    "required": ["name", "value", "units", "condition"],
    "properties": {
        "name": NonEmptyText,
        "value": {"type": ["number", "string", "null"]},
        "value_ref": {"oneOf": [REF, {"type": "null"}]},
        "units": {"type": "string"},
        "dimensionless": Bool,
        "condition": Text,
        "uncertainty": {
            "type": ["object", "null"],
            "properties": {
                "type": {"enum": ["std_dev", "std_error", "confidence_interval", "bounds", "none"]},
                "level": {"type": ["number", "null"]},
                "method": NullableText,
            },
            "additionalProperties": False,
        },
        "sample_count": {"type": ["integer", "null"], "minimum": 0},
        "estimator": NullableText,
        "selection_rule": NullableText,
        "fit_window": NullableText,
        "weighting": NullableText,
        "model": NullableText,
        "diagnostics_ref": {"oneOf": [REF, {"type": "null"}]},
        "numerical_precision": NullableText,
        "domain_limits": NullableText,
        "analysis_ref": {"oneOf": [REF, {"type": "null"}]},
    },
    "additionalProperties": False,
}

_RESULT_SCHEMA = {
    "type": "object",
    "required": ["results"],
    "properties": {"results": {"type": "array", "items": RESULT_ENTRY}},
    "additionalProperties": True,
}

_PROJECT = {
    "type": "object",
    "required": ["scope", "objectives"],
    "properties": {
        "subkind": {"const": "project"},
        "scope": NonEmptyText,
        "objectives": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "research_area": NullableText,
        "domain_conventions": {"type": "object"},
        "current_priorities": StringList,
        "collaboration_roles": {"type": "array", "items": {"type": "object"}},
        "policy_refs": StringList,
        "terminology_aliases": {"type": "object"},
        "comparison_rules": {"type": "object"},
        "completion_evidence_conventions": StringList,
        "workspace_identity": NullableText,
    },
    "additionalProperties": False,
}

_IDEA = {
    "type": "object",
    "required": ["subkind", "proposal"],
    "properties": {
        "subkind": {"enum": ["idea", "hypothesis"]},
        "proposal": NonEmptyText,
        "applicability": {"type": ["object", "string", "null"]},
        "support_criteria": StringList,
        "contradiction_criteria": StringList,
        "review_state": {"enum": list(REVIEW_STATES)},
        "evidence_state": {"enum": list(EVIDENCE_STATES)},
        "assessment_rationale": NullableText,
        "assumption_refs": REF_LIST,
    },
    "additionalProperties": False,
}

_OBSERVATION = {
    "type": "object",
    "required": ["subkind", "observation", "conditions"],
    "properties": {
        "subkind": {"const": "observation"},
        "observation": NonEmptyText,
        "conditions": {"type": ["object", "string"]},
        "evidence_refs": REF_LIST,
        "provenance_category": {"enum": list(PROVENANCE_CATEGORIES)},
        "review_state": {"enum": list(REVIEW_STATES)},
        "evidence_state": {"enum": list(EVIDENCE_STATES)},
        "assessment_rationale": NullableText,
    },
    "additionalProperties": False,
}

_INTERPRETATION = {
    "type": "object",
    "required": ["subkind", "assertion"],
    "properties": {
        "subkind": {"enum": ["interpretation", "conclusion"]},
        "assertion": NonEmptyText,
        "assumptions": StringList,
        "assumption_refs": REF_LIST,
        "applicability": {"type": ["object", "string", "null"]},
        "support_refs": REF_LIST,
        "opposing_refs": REF_LIST,
        "review_state": {"enum": list(REVIEW_STATES)},
        "evidence_state": {"enum": list(EVIDENCE_STATES)},
        "assessment_rationale": NullableText,
        "assessor_actor_id": NullableText,
    },
    "additionalProperties": False,
}

_NEGATIVE_RESULT = {
    "type": "object",
    "required": ["subkind", "investigated_domain", "result", "detection_limits"],
    "properties": {
        "subkind": {"const": "negative_result"},
        "investigated_domain": {"type": ["object", "string"]},
        "result": NonEmptyText,
        "detection_limits": NonEmptyText,
        "diagnostic_evidence_refs": REF_LIST,
        "exceptions": StringList,
        "alternatives_open": StringList,
        "protocol_revision": NullableText,
        "review_state": {"enum": list(REVIEW_STATES)},
        "evidence_state": {"enum": list(EVIDENCE_STATES)},
        "assessment_rationale": NullableText,
    },
    "additionalProperties": False,
}

_DEFINITION = {
    "type": "object",
    "required": ["subkind", "meaning", "symbol", "namespace"],
    "properties": {
        "subkind": {"const": "definition"},
        "meaning": NonEmptyText,
        "symbol": NonEmptyText,
        "namespace": NonEmptyText,
        "units_or_domain": NullableText,
        "source_or_convention": NullableText,
        "aliases": StringList,
    },
    "additionalProperties": False,
}

_ASSUMPTION = {
    "type": "object",
    "required": ["subkind", "assumption", "domain"],
    "properties": {
        "subkind": {"const": "assumption"},
        "assumption": NonEmptyText,
        "domain": NonEmptyText,
        "consequences": StringList,
        "status": {"enum": ["holding", "questioned", "violated", "unknown"]},
        "review_state": {"enum": list(REVIEW_STATES)},
    },
    "additionalProperties": False,
}

_DERIVATION = {
    "type": "object",
    "required": ["subkind", "statement", "assumptions"],
    "properties": {
        "subkind": {"const": "derivation"},
        "statement": NonEmptyText,
        "assumptions": StringList,
        "assumption_refs": REF_LIST,
        "conventions": {"type": ["object", "string", "null"]},
        "intermediate_results": StringList,
        "gaps_checks": StringList,
        "source_refs": REF_LIST,
        "code_refs": REF_LIST,
        "review_state": {"enum": list(REVIEW_STATES)},
        "evidence_state": {"enum": list(EVIDENCE_STATES)},
        "assessment_rationale": NullableText,
    },
    "additionalProperties": False,
}

_METHOD = {
    "type": "object",
    "required": ["subkind", "procedure", "version"],
    "properties": {
        "subkind": {"const": "method"},
        "procedure": NonEmptyText,
        "version": NonEmptyText,
        "prerequisites": StringList,
        "inputs": StringList,
        "outputs": StringList,
        "applicability": {"type": ["object", "string", "null"]},
        "validation_checks": StringList,
        "reproducibility_refs": REF_LIST,
        "code_refs": REF_LIST,
    },
    "additionalProperties": False,
}

_DECISION = {
    "type": "object",
    "required": ["subkind", "choice", "alternatives", "reasons", "decision_maker"],
    "properties": {
        "subkind": {"const": "decision"},
        "choice": NonEmptyText,
        "alternatives": {"type": "array", "items": {"type": "object"}},
        "reasons": StringList,
        "decision_maker": NonEmptyText,
        "applicability": {"type": ["object", "string", "null"]},
        "evidence_refs": REF_LIST,
        "reconsideration_conditions": StringList,
    },
    "additionalProperties": False,
}

_QUESTION = {
    "type": "object",
    "required": ["subkind", "question", "why_it_matters", "answer_criteria"],
    "properties": {
        "subkind": {"const": "question"},
        "question": NonEmptyText,
        "why_it_matters": NonEmptyText,
        "dependencies": StringList,
        "dependency_refs": REF_LIST,
        "answer_criteria": NonEmptyText,
    },
    "additionalProperties": False,
}

_CAVEAT = {
    "type": "object",
    "required": ["subkind", "affected_scope", "severity", "effect", "resolution_criterion"],
    "properties": {
        "subkind": {"enum": ["caveat", "issue"]},
        "affected_scope": {"type": ["object", "string"]},
        "severity": {"enum": list(BLOCKER_SEVERITIES)},
        "effect": NonEmptyText,
        "blocking_operations": StringList,
        "resolution_criterion": NonEmptyText,
        "status": {"enum": ["open", "resolved", "dismissed"]},
        "resolved_by_ref": {"oneOf": [REF, {"type": "null"}]},
        "resolution_rationale": NullableText,
        "criterion_met": NullableText,
        "evidence_refs": REF_LIST,
        "expiry": NullableText,
    },
    "additionalProperties": False,
}

_CLAIM = {
    "type": "object",
    "required": ["subkind", "statement", "domain_applicability", "evidence_criteria"],
    "properties": {
        "subkind": {"const": "claim"},
        "statement": NonEmptyText,
        "domain_applicability": {"type": ["object", "string"]},
        "quantifiers": NullableText,
        "assumption_refs": REF_LIST,
        "evidence_criteria": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["criterion"],
                "properties": {
                    "criterion": NonEmptyText,
                    "criterion_revision": {"type": ["integer", "null"], "minimum": 1},
                    "criterion_ref": {"oneOf": [REF, {"type": "null"}]},
                },
                "additionalProperties": False,
            },
        },
        "assessment": {
            "type": ["object", "null"],
            "properties": {
                "evidence_state": {"enum": list(EVIDENCE_STATES)},
                "review_state": {"enum": list(REVIEW_STATES)},
                "rationale": NonEmptyText,
                "assessor_actor_id": NullableText,
                "assessed_revision": {"type": ["integer", "null"], "minimum": 1},
                "criterion_ref": {"oneOf": [REF, {"type": "null"}]},
                "support_refs": REF_LIST,
                "contradiction_refs": REF_LIST,
                "exclusions": StringList,
                "missing_checks": StringList,
                "assessed_seq": {"type": ["integer", "null"]},
            },
            "additionalProperties": False,
        },
        "review_state": {"enum": list(REVIEW_STATES)},
        "evidence_state": {"enum": list(EVIDENCE_STATES)},
        "assessment_rationale": NullableText,
    },
    "additionalProperties": False,
}

_WORK = {
    "type": "object",
    "required": ["subkind", "objective", "work_state", "priority", "priority_reason", "completion_criteria", "owner"],
    "properties": {
        "subkind": {"enum": list(WORK_SUBKINDS)},
        "objective": NonEmptyText,
        "work_state": {"enum": list(WORK_STATES)},
        "priority": {"enum": list(PRIORITIES)},
        "priority_reason": NonEmptyText,
        "completion_criteria": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "owner": NonEmptyText,
        "deadline": {"type": ["object", "string", "null"]},
        "estimate": {"type": ["object", "string", "null"]},
        "next_action": NullableText,
        "parent_goal_ref": {"oneOf": [REF, {"type": "null"}]},
        "required_inputs": StringList,
        "completion_refs": REF_LIST,
        "completion_report": {"type": ["object", "string", "null"]},
        "blocked_reason": NullableText,
        "review_state": {"enum": list(REVIEW_STATES)},
        "reviewer": NullableText,
    },
    "additionalProperties": False,
}

_STUDY = {
    "type": "object",
    "required": ["subkind", "question", "protocol", "required_outputs", "completion_criteria", "study_state"],
    "properties": {
        "subkind": {"enum": list(STUDY_SUBKINDS)},
        "question": NonEmptyText,
        "protocol": {"type": "object"},
        "required_outputs": {"type": "array", "items": {"type": "object"}},
        "applicable_claim_refs": REF_LIST,
        "comparison_plan": {"type": "object"},
        "completion_criteria": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "study_state": {"enum": list(STUDY_STATES)},
        "executable": Bool,
        "trial_design": {"type": ["object", "null"]},
        "conclusion_ref": {"oneOf": [REF, {"type": "null"}]},
        "conclusion": {"type": ["object", "string", "null"]},
        "state_reason": NullableText,
    },
    "additionalProperties": False,
}

_SOURCE = {
    "type": "object",
    "required": ["subkind", "source_type", "title", "version", "identity_assurance"],
    "properties": {
        "subkind": {"enum": list(SOURCE_SUBKINDS)},
        "source_type": {"enum": list(SOURCE_SUBKINDS)},
        "title": NonEmptyText,
        "author": NullableText,
        "external_id": NullableText,
        "version": {"type": ["string", "object"]},
        "locator": NullableText,
        "mirror_locator": NullableText,
        "access_restrictions": NullableText,
        "publication_time": NullableText,
        "retrieval_time": NullableText,
        "import_time": NullableText,
        "blob_hash": NullableText,
        "byte_size": {"type": ["integer", "null"], "minimum": 0},
        "identity_assurance": {"enum": list(ASSURANCE_LEVELS)},
        "extraction_status": {"enum": list(EXTRACTION_STATUSES)},
        "extraction_ids": StringList,
        "preservation": {"enum": ["local_copy_allowed", "metadata_only", "external_embedding_allowed", "restricted"]},
        "known_omissions": StringList,
        "supplied_by": NullableText,
        "extracted_by": NullableText,
        "author_attribution_notes": NullableText,
    },
    "additionalProperties": False,
}

_ARTIFACT = {
    "type": "object",
    "required": ["subkind", "role", "content_identity", "assurance", "availability"],
    "properties": {
        "subkind": {"enum": list(ARTIFACT_SUBKINDS)},
        "role": NonEmptyText,
        "media_type": NullableText,
        "byte_size": {"type": ["integer", "null"], "minimum": 0},
        "content_identity": {
            "type": "object",
            "required": ["kind", "value"],
            "properties": {
                "kind": {"enum": ["sha256", "manifest", "metadata_only", "unverified"]},
                "value": NonEmptyText,
            },
            "additionalProperties": False,
        },
        "assurance": {"enum": list(ASSURANCE_LEVELS)},
        "locations": {"type": "array", "items": {"type": "object"}},
        "producer_ref": {"oneOf": [REF, {"type": "null"}]},
        "input_lineage": REF_LIST,
        "availability": {"enum": ["available", "missing", "unverified", "restricted"]},
        "last_verification": NullableText,
        "preservation_policy": NullableText,
        "results": _RESULT_SCHEMA,
        "selection_manifest": {"type": ["object", "null"]},
        "comparison_assessments": {"type": "object"},
    },
    "additionalProperties": False,
}

_RUN = {
    "type": "object",
    "required": ["subkind", "study_ref", "attempt_no", "manifest", "status", "validity"],
    "properties": {
        "subkind": {"enum": list(RUN_SUBKINDS)},
        "study_ref": REF,
        "trial_slot_ref": {"oneOf": [REF, {"type": "null"}]},
        "attempt_no": {"type": "integer", "minimum": 1},
        "manifest": {"type": "object"},
        "manifest_hash": NullableText,
        "execution_id": NullableText,
        "status": {"enum": list(RUN_STATUSES)},
        "validity": {"enum": list(RUN_VALIDITY)},
        "validity_reason": NullableText,
        "validity_assessment": {"type": ["object", "null"]},
        "partial_outputs": REF_LIST,
        "comparison_assessments": {"type": "object"},
        "reconciliation": NullableText,
        "result_schema": {"type": ["object", "string", "null"]},
        "output_checksums": {"type": "object"},
    },
    "additionalProperties": False,
}

_RESOURCE = {
    "type": "object",
    "required": ["subkind", "machine_id", "capabilities", "capacity", "admin_state"],
    "properties": {
        "subkind": {"enum": ["machine", "gpu", "cpu", "scheduler"]},
        "machine_id": NonEmptyText,
        "display_name": NullableText,
        "capabilities": {"type": "object"},
        "capacity": {
            "oneOf": [
                {"type": "integer", "minimum": 1},
                {
                    "type": "object",
                    "properties": {"units": {"type": "integer", "minimum": 1}},
                    "required": ["units"],
                    "additionalProperties": True,
                },
            ]
        },
        "admin_state": {"enum": ["enabled", "draining", "disabled", "unknown"]},
        "limitations": StringList,
        "parent_resource_ref": {"oneOf": [REF, {"type": "null"}]},
        "physical_identity": NullableText,
        "allocation_domain": NullableText,
    },
    "additionalProperties": False,
}

_LINK = {
    "type": "object",
    "required": ["subkind", "predicate"],
    "properties": {
        "subkind": {"const": "link"},
        "predicate": NonEmptyText,
        "qualifiers": {"type": "object"},
        "rationale": NullableText,
        "review_state": {"enum": list(REVIEW_STATES)},
        "assessed_by": NullableText,
        "applicability": {"type": ["object", "string", "null"]},
    },
    "additionalProperties": False,
}

_HANDOFF = {
    "type": "object",
    "required": ["subkind", "summary", "next_step", "snapshot_cursor"],
    "properties": {
        "subkind": {"enum": list(HANDOFF_SUBKINDS)},
        "summary": NonEmptyText,
        "completed_refs": REF_LIST,
        "open_refs": REF_LIST,
        "pending_proposals": StringList,
        "in_flight_executions": StringList,
        "next_step": NonEmptyText,
        "snapshot_cursor": NonEmptyText,
        "unresolved_issues": StringList,
    },
    "additionalProperties": False,
}

_CLAIM_ASSESSMENT = {
    "type": "object",
    "required": ["evidence_state", "review_state", "rationale", "assessed_revision"],
    "properties": {
        "evidence_state": {"enum": list(EVIDENCE_STATES)},
        "review_state": {"enum": list(REVIEW_STATES)},
        "rationale": NonEmptyText,
        "assessor_actor_id": NullableText,
        "assessed_revision": {"type": "integer", "minimum": 1},
        "criterion_ref": {"oneOf": [REF, {"type": "null"}]},
        "support_refs": REF_LIST,
        "contradiction_refs": REF_LIST,
        "exclusions": StringList,
        "missing_checks": StringList,
    },
    "additionalProperties": False,
}

RECORD_SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    ("project", "project"): _PROJECT,
    ("knowledge", "idea"): _IDEA,
    ("knowledge", "hypothesis"): _IDEA,
    ("knowledge", "observation"): _OBSERVATION,
    ("knowledge", "interpretation"): _INTERPRETATION,
    ("knowledge", "conclusion"): _INTERPRETATION,
    ("knowledge", "negative_result"): _NEGATIVE_RESULT,
    ("knowledge", "definition"): _DEFINITION,
    ("knowledge", "assumption"): _ASSUMPTION,
    ("knowledge", "derivation"): _DERIVATION,
    ("knowledge", "method"): _METHOD,
    ("knowledge", "decision"): _DECISION,
    ("knowledge", "question"): _QUESTION,
    ("knowledge", "caveat"): _CAVEAT,
    ("knowledge", "issue"): _CAVEAT,
    ("claim", "claim"): _CLAIM,
    ("work", "task"): _WORK,
    ("work", "goal"): _WORK,
    ("work", "milestone"): _WORK,
    ("study", "analytical"): _STUDY,
    ("study", "numerical"): _STUDY,
    ("study", "experimental"): _STUDY,
    ("study", "literature"): _STUDY,
    ("source", "paper"): _SOURCE,
    ("source", "book"): _SOURCE,
    ("source", "note"): _SOURCE,
    ("source", "meeting_excerpt"): _SOURCE,
    ("source", "message"): _SOURCE,
    ("source", "web_page"): _SOURCE,
    ("source", "code_document"): _SOURCE,
    ("source", "dataset_documentation"): _SOURCE,
    ("source", "result_report"): _SOURCE,
    ("artifact", "dataset"): _ARTIFACT,
    ("artifact", "figure"): _ARTIFACT,
    ("artifact", "table"): _ARTIFACT,
    ("artifact", "analysis_result"): _ARTIFACT,
    ("artifact", "notebook_export"): _ARTIFACT,
    ("artifact", "source_snapshot"): _ARTIFACT,
    ("artifact", "environment_manifest"): _ARTIFACT,
    ("artifact", "checkpoint"): _ARTIFACT,
    ("artifact", "manuscript_target"): _ARTIFACT,
    ("run", "attempt"): _RUN,
    ("resource", "machine"): _RESOURCE,
    ("resource", "gpu"): _RESOURCE,
    ("resource", "cpu"): _RESOURCE,
    ("resource", "scheduler"): _RESOURCE,
    ("link", "link"): _LINK,
    ("handoff", "session_handoff"): _HANDOFF,
}

SUPPLEMENTAL_SCHEMAS: dict[str, dict[str, Any]] = {
    "claim_assessment": _CLAIM_ASSESSMENT,
    "result_entry": RESULT_ENTRY,
    "results": _RESULT_SCHEMA,
}

for _record_schema in RECORD_SCHEMAS.values():
    _properties = _record_schema.setdefault("properties", {})
    _properties.setdefault("tombstone_reason", NullableText)
    _properties.setdefault("tombstone_note", NullableText)


def schema_for(kind: str, subkind: str) -> dict[str, Any]:
    schema = RECORD_SCHEMAS.get((kind, subkind))
    if schema is None:
        raise schema_validation_failed(
            f"No payload schema for kind/subkind {kind}/{subkind}.",
            kind=kind,
            subkind=subkind,
        )
    return schema


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _type_matches(expected: str | list[str], value: Any) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    return any(_TYPE_CHECKS[option](value) for option in options)


def _fail(path: str, message: str, **details: Any) -> None:
    raise schema_validation_failed(f"{path}: {message}" if path else message, path=path, **details)


def validate_value(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    if "const" in schema and value != schema["const"]:
        _fail(path, f"must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        _fail(path, f"must be one of {sorted(map(str, schema['enum']))}", allowed=list(schema["enum"]))
    if "type" in schema and not _type_matches(schema["type"], value):
        _fail(path, f"must be of type {schema['type']}", actual_type=type(value).__name__)
    if value is None:
        return
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                validate_value(option, value, path)
                matches += 1
            except Exception:
                continue
        if matches != 1:
            _fail(path, "must match exactly one permitted shape", matched=matches)
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_value(option, value, path)
                return
            except Exception:
                continue
        _fail(path, "must match at least one permitted shape")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            _fail(path, f"must have at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _fail(path, f"must have at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            _fail(path, f"must match pattern {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _fail(path, f"must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            _fail(path, f"must be <= {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            _fail(path, f"must contain at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _fail(path, f"must contain at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_value(item_schema, item, f"{path}[{index}]")
    if isinstance(value, dict):
        required = schema.get("required", [])
        for field in required:
            if field not in value:
                _fail(f"{path}.{field}", "is required")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate_value(properties[key], item, f"{path}.{key}")
            elif additional is False:
                _fail(f"{path}.{key}", "is not a permitted field")
            elif isinstance(additional, dict):
                validate_value(additional, item, f"{path}.{key}")


def validate_record_payload(
    kind: str,
    subkind: str,
    state_json: dict[str, Any],
    *,
    body_md: str = "",
) -> None:
    if not isinstance(state_json, dict):
        raise schema_validation_failed("state_json must be an object.", kind=kind, subkind=subkind)
    schema = schema_for(kind, subkind)
    validate_value(schema, state_json, "$")
    stored_subkind = state_json.get("subkind")
    if stored_subkind is not None and stored_subkind != subkind:
        raise schema_validation_failed(
            "state_json.subkind does not match the declared subkind.",
            declared=subkind,
            payload=stored_subkind,
        )
    if kind == "artifact" and isinstance(state_json.get("results"), dict):
        validate_value(_RESULT_SCHEMA, state_json["results"], "$.results")
    if kind == "knowledge" and subkind == "derivation" and not body_md.strip():
        raise schema_validation_failed(
            "A derivation must preserve its coherent steps in body_md.",
            hint="Do not split a derivation into one object per algebraic line.",
        )
    if kind == "claim" and not body_md.strip() and not state_json.get("statement"):
        raise schema_validation_failed("A claim requires a precise statement.")



def iter_refs(value: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(value, dict):
        if "object_id" in value and isinstance(value.get("object_id"), str):
            found.append((path, value))
        for key, item in value.items():
            found.extend(iter_refs(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(iter_refs(item, f"{path}[{index}]"))
    return found


def summarize_schema(kind: str, subkind: str) -> dict[str, Any]:
    schema = schema_for(kind, subkind)
    return {
        "kind": kind,
        "subkind": subkind,
        "required": list(schema.get("required", [])),
        "fields": sorted(schema.get("properties", {})),
    }


def available_record_schemas() -> list[dict[str, Any]]:
    result = []
    for (kind, subkind) in sorted(RECORD_SCHEMAS):
        result.append(summarize_schema(kind, subkind))
    return result
