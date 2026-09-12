from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from research_kb.domain.vocab import (
    ACYCLIC_PREDICATES,
    PREDICATE_DIRECTION,
    PREDICATE_PIN_RULE,
    PREDICATES,
)
from research_kb.errors import wrong_reference_type


@dataclass(frozen=True)
class PredicateSpec:
    name: str
    direction: tuple[str, str]
    pin_rule: str
    acyclic: bool
    required_qualifiers: tuple[str, ...] = ()

    @property
    def pins_required(self) -> bool:
        return self.pin_rule in ("pinned_required", "pinned_required_reviewed")

    @property
    def review_required(self) -> bool:
        return self.pin_rule == "pinned_required_reviewed"

    @property
    def tracking_allowed(self) -> bool:
        return self.pin_rule in ("tracking", "either_declared")


_ENDPOINT_GROUP_KINDS: dict[str, tuple[str, ...]] = {
    "record": (
        "project",
        "knowledge",
        "claim",
        "source",
        "artifact",
        "work",
        "study",
        "run",
        "resource",
        "handoff",
    ),
    "topic": ("project", "knowledge", "claim", "artifact", "work", "study", "run"),
    "evidence": ("knowledge", "claim", "artifact", "source", "run", "study"),
    "claim": ("claim", "knowledge"),
    "output": ("knowledge", "claim", "artifact", "run", "study"),
    "input": ("knowledge", "claim", "source", "artifact", "run", "study", "method"),
    "consumer": ("claim", "method", "derivation", "knowledge", "study", "run", "artifact"),
    "assumption": ("knowledge",),
    "replacement": ("knowledge", "claim", "source", "artifact"),
    "replaced": ("knowledge", "claim", "source", "artifact"),
    "dependent": ("work", "study", "run", "knowledge", "claim"),
    "prerequisite": ("work", "study", "run", "knowledge", "claim", "artifact", "source"),
    "blocker": ("knowledge", "artifact", "work", "study"),
    "target": ("work", "study", "run", "claim", "artifact", "knowledge", "project"),
    "resolution": ("knowledge", "decision", "artifact", "run", "study", "work"),
    "issue": ("knowledge",),
    "artifact": ("artifact",),
    "producer": ("run", "study", "artifact", "work"),
    "item": ("knowledge", "claim", "artifact", "run", "study"),
    "any": (
        "project",
        "knowledge",
        "claim",
        "source",
        "artifact",
        "work",
        "study",
        "run",
        "resource",
        "handoff",
    ),
}

_SUBKIND_GROUP_KINDS: dict[str, tuple[str, ...]] = {
    "method": ("knowledge",),
    "derivation": ("knowledge",),
    "decision": ("knowledge",),
}


def _endpoint_kinds(group: str, kind: str, subkind: str) -> tuple[str, ...]:
    if group == "any":
        return _ENDPOINT_GROUP_KINDS["any"]
    kinds = _ENDPOINT_GROUP_KINDS.get(group)
    if kinds is None:
        return ()
    if kind == "knowledge" and subkind in _SUBKIND_GROUP_KINDS:
        pass
    return kinds


PREDICATE_REGISTRY: dict[str, PredicateSpec] = {
    name: PredicateSpec(
        name=name,
        direction=PREDICATE_DIRECTION[name],
        pin_rule=PREDICATE_PIN_RULE[name],
        acyclic=name in ACYCLIC_PREDICATES,
        required_qualifiers=("condition",) if name == "depends_on" else (),
    )
    for name in PREDICATES
}


def get_predicate(name: str) -> PredicateSpec:
    spec = PREDICATE_REGISTRY.get(name)
    if spec is None:
        from research_kb.errors import schema_validation_failed

        raise schema_validation_failed(
            f"Unknown relationship predicate: {name}",
            allowed=sorted(PREDICATE_REGISTRY),
        )
    return spec


def validate_endpoints(
    predicate: str,
    src_kind: str,
    src_subkind: str,
    dst_kind: str,
    dst_subkind: str,
) -> None:
    spec = get_predicate(predicate)
    src_group, dst_group = spec.direction
    allowed_src = _endpoint_kinds(src_group, src_kind, src_subkind)
    allowed_dst = _endpoint_kinds(dst_group, dst_kind, dst_subkind)
    if src_kind not in allowed_src:
        raise wrong_reference_type(
            f"Predicate '{predicate}' does not accept a {src_kind}/{src_subkind} source endpoint.",
            predicate=predicate,
            endpoint="source",
            allowed_kinds=sorted(allowed_src),
        )
    if dst_kind not in allowed_dst:
        raise wrong_reference_type(
            f"Predicate '{predicate}' does not accept a {dst_kind}/{dst_subkind} destination endpoint.",
            predicate=predicate,
            endpoint="destination",
            allowed_kinds=sorted(allowed_dst),
        )


def requires_pin(predicate: str) -> bool:
    return get_predicate(predicate).pins_required


def tracking_allowed(predicate: str) -> bool:
    return get_predicate(predicate).tracking_allowed


def is_acyclic(predicate: str) -> bool:
    return get_predicate(predicate).acyclic


IMPACT_DIRECTIONS: dict[str, str] = {
    "derived_from": "incoming",
    "assumes": "incoming",
    "uses": "incoming",
    "produced_by": "incoming",
    "depends_on": "incoming",
    "included_in": "outgoing",
}


def impact_predicates(direction: str) -> tuple[str, ...]:
    return tuple(name for name, value in IMPACT_DIRECTIONS.items() if value == direction)


def registry_document() -> list[dict[str, Any]]:
    return [
        {
            "predicate": spec.name,
            "source_group": spec.direction[0],
            "destination_group": spec.direction[1],
            "pin_rule": spec.pin_rule,
            "acyclic": spec.acyclic,
            "required_qualifiers": list(spec.required_qualifiers),
        }
        for spec in PREDICATE_REGISTRY.values()
    ]
