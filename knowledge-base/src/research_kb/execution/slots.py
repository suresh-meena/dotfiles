from __future__ import annotations

import itertools
import json
import sqlite3
from typing import Any

from research_kb.domain.trial_keys import normalize_conditions, trial_key, trial_key_string
from research_kb.errors import schema_validation_failed
from research_kb.storage.db import new_id, utc_now

DEFAULT_EXPANSION_CAP = 10_000


def expand_design(design: dict[str, Any], *, cap: int = DEFAULT_EXPANSION_CAP) -> list[dict[str, Any]]:
    sparse = design.get("sparse")
    if sparse is not None:
        if not isinstance(sparse, list):
            raise schema_validation_failed("Sparse trial designs must be a list of condition rows.")
        if len(sparse) > cap:
            raise schema_validation_failed("Sparse design exceeds the configured expansion cap.", cap=cap)
        return [dict(row) for row in sparse]
    factors = design.get("factors")
    if not factors:
        raise schema_validation_failed("A study trial design needs factors or explicit sparse rows.")
    names = list(factors)
    value_lists = []
    for name in names:
        values = factors[name]
        if not isinstance(values, list) or not values:
            raise schema_validation_failed(f"Factor '{name}' must be a non-empty list.", factor=name)
        value_lists.append(values)
    total = 1
    for values in value_lists:
        total *= len(values)
    if total > cap:
        raise schema_validation_failed(
            "Expanded condition count exceeds the configured cap.",
            expanded=total,
            cap=cap,
            hint="Use explicit sparse rows or raise the reviewed cap deliberately.",
        )
    constraints = design.get("constraints") or []
    rows: list[dict[str, Any]] = []
    for combination in itertools.product(*value_lists):
        row = dict(zip(names, combination))
        if all(_constraint_holds(row, constraint) for constraint in constraints):
            rows.append(row)
    return rows


def _constraint_holds(row: dict[str, Any], constraint: dict[str, Any]) -> bool:
    field = constraint.get("field")
    op = constraint.get("op", "in")
    values = constraint.get("values", [])
    if field not in row:
        return False
    if op == "in":
        return row[field] in values
    if op == "not_in":
        return row[field] not in values
    if op == "eq":
        return row[field] == constraint.get("value")
    if op == "neq":
        return row[field] != constraint.get("value")
    raise schema_validation_failed("Unsupported constraint operator.", op=op)


def normalize_trial(trial: dict[str, Any], *, set_fields: tuple[str, ...] = (), unit_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    conditions = trial.get("conditions")
    if not isinstance(conditions, dict):
        raise schema_validation_failed("A trial must declare conditions as an object.")
    return normalize_conditions(
        conditions,
        defaults=trial.get("defaults"),
        set_fields=set_fields,
        unit_fields=unit_fields,
    )


def ensure_trial_slot(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    study_object_id: str,
    study_revision: int,
    conditions: dict[str, Any],
    replicate_identity: str | None,
    required: bool,
    seq: int,
) -> dict[str, Any]:
    key = trial_key(
        project_id=project_id,
        study_object_id=study_object_id,
        study_revision=study_revision,
        conditions=conditions,
        replicate_identity=replicate_identity,
    )
    key_string = trial_key_string(key)
    row = conn.execute(
        """
        SELECT * FROM trial_slots
        WHERE project_id = ? AND study_object_id = ? AND study_revision = ?
          AND trial_key = ? AND replicate_identity IS ?
        """,
        (project_id, study_object_id, study_revision, key["hash"], replicate_identity),
    ).fetchone()
    if row is not None:
        return {"slot_id": row["slot_id"], "trial_key": key, "trial_key_string": key_string, "created": False}
    slot_id = new_id()
    conn.execute(
        """
        INSERT INTO trial_slots
          (slot_id, project_id, study_object_id, study_revision, trial_key, trial_key_format,
           replicate_identity, conditions_json, required, created_seq, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slot_id,
            project_id,
            study_object_id,
            study_revision,
            key["hash"],
            key["format"],
            replicate_identity,
            json.dumps(conditions, sort_keys=True),
            1 if required else 0,
            seq,
            utc_now(),
        ),
    )
    return {"slot_id": slot_id, "trial_key": key, "trial_key_string": key_string, "created": True}


def next_attempt_no(conn: sqlite3.Connection, slot_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next FROM run_attempts WHERE slot_id = ?",
        (slot_id,),
    ).fetchone()
    return int(row["next"])


def coverage(conn: sqlite3.Connection, project_id: str, study_object_id: str, study_revision: int) -> dict[str, Any]:
    slots = conn.execute(
        """
        SELECT s.slot_id, s.trial_key, s.replicate_identity, s.conditions_json, s.required,
               COUNT(a.attempt_id) AS attempts,
               MAX(
                 CASE WHEN json_extract(r.state_json, '$.validity') = 'valid'
                       AND o.record_state = 'active' THEN 1 ELSE 0 END
               ) AS eligible
        FROM trial_slots s
        LEFT JOIN run_attempts a ON a.slot_id = s.slot_id
        LEFT JOIN objects o ON o.project_id = a.project_id AND o.object_id = a.run_object_id
        LEFT JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                            AND r.revision = o.current_revision
        WHERE s.project_id = ? AND s.study_object_id = ? AND s.study_revision = ?
        GROUP BY s.slot_id
        ORDER BY s.trial_key, s.replicate_identity
        """,
        (project_id, study_object_id, study_revision),
    ).fetchall()
    required = [slot for slot in slots if slot["required"]]
    covered = [slot for slot in required if slot["eligible"]]
    missing = [slot["slot_id"] for slot in required if not slot["eligible"]]
    attempts_without_eligible = [
        slot["slot_id"] for slot in required if slot["attempts"] and not slot["eligible"]
    ]
    return {
        "study_object_id": study_object_id,
        "study_revision": study_revision,
        "denominator": len(required),
        "covered": len(covered),
        "missing_slots": missing,
        "attempted_but_ineligible": attempts_without_eligible,
        "note": (
            "Coverage counts required slots with eligible selected evidence under the specified policy, "
            "not run rows."
        ),
    }
