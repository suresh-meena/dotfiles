from __future__ import annotations

from typing import Any

from research_kb.errors import schema_validation_failed

COMPARISON_OPS = ("eq", "neq", "in", "not_in", "range", "exists", "not_exists")


def validate_condition(condition: dict[str, Any]) -> None:
    if not isinstance(condition, dict):
        raise schema_validation_failed("A blocking-rule condition must be an object.")
    op = condition.get("op")
    if op not in COMPARISON_OPS:
        raise schema_validation_failed(
            "Unknown blocking-rule operator.", op=op, allowed=list(COMPARISON_OPS)
        )
    if op in ("exists", "not_exists"):
        if "field" not in condition:
            raise schema_validation_failed("A blocking-rule condition requires a field.")
        return
    if "field" not in condition or "value" not in condition:
        raise schema_validation_failed("A blocking-rule condition requires field and value.")
    if op == "range":
        value = condition["value"]
        if not isinstance(value, dict) or not any(key in value for key in ("min", "max")):
            raise schema_validation_failed("A range condition requires value.min and/or value.max.")


def validate_rule(rule: dict[str, Any]) -> None:
    if not isinstance(rule, dict):
        raise schema_validation_failed("A blocking rule must be an object.")
    if "all" in rule or "any" in rule:
        key = "all" if "all" in rule else "any"
        children = rule[key]
        if not isinstance(children, list) or not children:
            raise schema_validation_failed(f"Rule '{key}' must be a non-empty list.")
        for child in children:
            validate_rule(child)
        return
    if "not" in rule:
        validate_rule(rule["not"])
        return
    validate_condition(rule)


def _lookup(context: dict[str, Any], field: str) -> tuple[bool, Any]:
    node: Any = context
    for part in field.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


def evaluate_condition(condition: dict[str, Any], context: dict[str, Any]) -> str:
    op = condition["op"]
    present, actual = _lookup(context, condition["field"])
    if op == "exists":
        return "applies" if present else "not_applies"
    if op == "not_exists":
        return "not_applies" if present else "applies"
    if not present:
        return "unknown"
    expected = condition.get("value")
    if op == "eq":
        return "applies" if actual == expected else "not_applies"
    if op == "neq":
        return "applies" if actual != expected else "not_applies"
    if op == "in":
        if not isinstance(expected, list):
            return "unknown"
        return "applies" if actual in expected else "not_applies"
    if op == "not_in":
        if not isinstance(expected, list):
            return "unknown"
        return "applies" if actual not in expected else "not_applies"
    if op == "range":
        bounds = expected if isinstance(expected, dict) else {}
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return "unknown"
        if "min" in bounds and actual < bounds["min"]:
            return "not_applies"
        if "max" in bounds and actual > bounds["max"]:
            return "not_applies"
        return "applies"
    return "unknown"


def evaluate_rule(rule: dict[str, Any], context: dict[str, Any]) -> str:
    validate_rule(rule)
    if "all" in rule:
        statuses = [evaluate_rule(child, context) for child in rule["all"]]
        if any(status == "not_applies" for status in statuses):
            return "not_applies"
        if any(status == "unknown" for status in statuses):
            return "unknown"
        return "applies"
    if "any" in rule:
        statuses = [evaluate_rule(child, context) for child in rule["any"]]
        if any(status == "applies" for status in statuses):
            return "applies"
        if any(status == "unknown" for status in statuses):
            return "unknown"
        return "not_applies"
    if "not" in rule:
        inner = evaluate_rule(rule["not"], context)
        if inner == "unknown":
            return "unknown"
        return "not_applies" if inner == "applies" else "applies"
    return evaluate_condition(rule, context)


def applicability_context(state: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in ("domain_applicability", "applicability", "conditions", "affected_scope"):
        value = state.get(key)
        if isinstance(value, dict):
            for field, item in value.items():
                context.setdefault(field, item)
    if isinstance(state.get("affected_scope"), str):
        context.setdefault("scope", state["affected_scope"])
    return context


def blocker_evaluation(qualifiers: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str]:
    rule = qualifiers.get("applies_when")
    if rule is None:
        return True, "applies"
    status = evaluate_rule(rule, context)
    return status in ("applies", "unknown"), status
