from __future__ import annotations

import math
from typing import Any

import pytest

from research_kb.domain.canonical import canonical_hash, canonicalize, normalize_alias
from research_kb.domain.relation_registry import (
    get_predicate,
    requires_pin,
    validate_endpoints,
)
from research_kb.domain.schemas import (
    available_record_schemas,
    validate_record_payload,
)
from research_kb.domain.trial_keys import (
    normalize_conditions,
    trial_key,
    trial_key_string,
)
from research_kb.errors import KBError, schema_validation_failed, wrong_reference_type


def test_rfc8785_canonicalization_vectors():
    assert canonicalize({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonicalize([1, 2.5, True, None, "x"]) == '[1,2.5,true,null,"x"]'
    assert canonicalize({"z": [1, {"b": 2, "a": 1}]}) == '{"z":[1,{"a":1,"b":2}]}'
    assert canonicalize({"s": "line\nbreak"}) == '{"s":"line\\nbreak"}'
    assert canonicalize(0.0) == "0"
    assert canonicalize(100) == "100"
    assert canonical_hash({"a": 1}) == canonical_hash({"a": 1.0})


def test_canonicalization_rejects_nonfinite_and_duplicate_like_inputs():
    with pytest.raises(KBError):
        canonicalize({"x": math.nan})
    with pytest.raises(KBError):
        canonicalize({"x": math.inf})
    with pytest.raises(KBError):
        canonicalize({1: "integer key"})


def test_unicode_sorting_uses_utf16_code_units():
    assert canonicalize({"€": 1, "z": 2}) == '{"z":2,"€":1}'


def test_normalize_alias():
    assert normalize_alias("  Δt ") == normalize_alias("Δt")
    assert normalize_alias("F1   SCORE") == "f1 score"


def test_definition_schema_requires_core_fields():
    validate_record_payload(
        "knowledge",
        "definition",
        {"subkind": "definition", "meaning": "m", "symbol": "x", "namespace": "ns"},
    )
    with pytest.raises(KBError):
        validate_record_payload("knowledge", "definition", {"subkind": "definition", "meaning": "m"})
    with pytest.raises(KBError):
        validate_record_payload(
            "knowledge",
            "definition",
            {"subkind": "definition", "meaning": "m", "symbol": "x", "namespace": "ns", "extra": 1},
        )


def test_work_schema_requires_completion_criteria_and_owner():
    base = {
        "subkind": "task",
        "objective": "Do the thing",
        "work_state": "open",
        "priority": "high",
        "priority_reason": "blocks X",
        "completion_criteria": ["criterion"],
        "owner": "unassigned",
    }
    validate_record_payload("work", "task", base)
    with pytest.raises(KBError):
        validate_record_payload("work", "task", {**base, "owner": None})


def test_claim_schema_requires_criteria():
    with pytest.raises(KBError):
        validate_record_payload(
            "claim",
            "claim",
            {"subkind": "claim", "statement": "X holds.", "domain_applicability": {}, "evidence_criteria": []},
        )


def test_unknown_subkind_is_rejected():
    with pytest.raises(KBError):
        validate_record_payload("knowledge", "not_a_kind", {"subkind": "not_a_kind"})


def test_relation_registry_pins_and_endpoint_rules():
    assert requires_pin("supports")
    assert not requires_pin("related_to")
    spec = get_predicate("supports")
    assert spec.direction == ("evidence", "claim")
    validate_endpoints("supports", "knowledge", "observation", "claim", "claim")
    validate_endpoints("supports", "claim", "claim", "claim", "claim")
    with pytest.raises(KBError):
        validate_endpoints("supports", "work", "task", "claim", "claim")
    with pytest.raises(KBError):
        validate_endpoints("assumes", "claim", "claim", "work", "task")
    with pytest.raises(KBError):
        get_predicate("not_a_predicate")


def test_acyclic_predicates_marked():
    assert get_predicate("depends_on").acyclic
    assert get_predicate("supersedes").acyclic
    assert not get_predicate("related_to").acyclic


def test_record_schemas_cover_all_subkinds():
    entries = available_record_schemas()
    keys = {(entry["kind"], entry["subkind"]) for entry in entries}
    assert ("knowledge", "derivation") in keys
    assert ("artifact", "manuscript_target") in keys
    assert ("run", "attempt") in keys


def test_condition_normalization_preserves_null_and_order():
    left = normalize_conditions({"a": [1, 2], "b": None})
    right = normalize_conditions({"a": [1, 2], "b": None})
    assert left == right
    reordered = normalize_conditions({"a": [2, 1], "b": None})
    assert left != reordered


def test_condition_units_and_set_fields():
    normalized = normalize_conditions(
        {"window": {"value": 500, "unit": "ms"}},
        unit_fields=("window",),
    )
    assert normalized["window"] == {"value": 0.5, "unit": "s"}
    sets = normalize_conditions({"tags": ["b", "a"]}, set_fields=("tags",))
    assert sets["tags"] == ["a", "b"]


def test_trial_key_depends_on_revision_and_replicate():
    base: dict[str, Any] = dict(
        project_id="p",
        study_object_id="s",
        study_revision=1,
        conditions={"x": 1},
        replicate_identity=None,
    )
    key1 = trial_key(**base)
    key2 = trial_key(**{**base, "study_revision": 2})
    key3 = trial_key(**{**base, "replicate_identity": "r1"})
    assert key1["hash"] != key2["hash"]
    assert key1["hash"] != key3["hash"]
    assert trial_key_string(key1).startswith("tk1:")


def test_trial_key_rejects_nonfinite():
    with pytest.raises(KBError):
        trial_key(
            project_id="p",
            study_object_id="s",
            study_revision=1,
            conditions={"x": math.nan},
        )


def test_precise_decimals_encoded_as_typed_strings():
    normalized = normalize_conditions({"eps": 0.1, "big": 2**60})
    assert normalized["big"] == {"$int": str(2**60)}


def test_rfc8785_published_number_vector():
    from research_kb.domain.canonical import canonicalize

    value = {"numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27]}
    expected = '{"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27]}'
    assert canonicalize(value) == expected


def test_rfc8785_literals_and_string_escaping():
    from research_kb.domain.canonical import canonicalize

    assert canonicalize({"literals": [None, True, False]}) == '{"literals":[null,true,false]}'
    value = {"s": "\u20ac$\u000f\nA'B\"\\\\\"/"}
    rendered = canonicalize(value)
    assert "\\u000f" in rendered
    assert "\\u20ac" not in rendered
    assert "\\n" in rendered
