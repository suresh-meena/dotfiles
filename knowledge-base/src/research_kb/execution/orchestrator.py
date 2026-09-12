from __future__ import annotations

import json
from typing import Any

from research_kb.errors import (
    capability_unavailable,
    not_found,
    schema_validation_failed,
)
from research_kb.execution import leases, outbox, slots
from research_kb.execution.adapters import get_adapter
from research_kb.execution.manifests import (
    build_manifest,
    manifest_fingerprint,
    validate_manifest_provenance,
)
from research_kb.execution.reconciler import observe, reconcile_receipt, record_receipt
from research_kb.service.objects import Mutation, create_object
from research_kb.storage import repo
from research_kb.storage.db import new_id


def _require_execution(ctx) -> None:
    if not ctx.policy.get("execution", {}).get("enabled", False):
        raise capability_unavailable(
            "The optional execution module is disabled by policy.",
            hint="Execution is enabled only through explicit reviewed configuration.",
        )


DIAGNOSTIC_EXCEPTION_FIELDS = ("blocker_ref", "operation", "study_ref", "reason", "expires_at")


def _validate_diagnostic_exception(ctx, study, trial: dict[str, Any]) -> dict[str, Any] | None:
    from research_kb.domain.blocking_rules import applicability_context, blocker_evaluation

    conn = ctx.conn
    state = repo.parse_state(study)
    context = applicability_context(state)
    applying: list[str] = []
    for link in repo.list_links(
        conn, project_id=ctx.project_id, object_id=study["object_id"], predicate="blocks", direction="in"
    ):
        source = repo.object_row_or_none(conn, ctx.project_id, link["src_object_id"])
        if source is None:
            continue
        blocker_state = repo.parse_state(repo.current_revision(conn, ctx.project_id, link["src_object_id"]))
        if blocker_state.get("status") == "resolved":
            continue
        qualifiers = json.loads(link["qualifiers_json"] or "{}")
        applies, evaluation = blocker_evaluation(qualifiers, context)
        if applies and (evaluation == "unknown" or blocker_state.get("severity") in ("high", "critical")):
            applying.append(link["src_object_id"])
    if not applying:
        return None
    exception = trial.get("diagnostic_exception")
    if not exception:
        from research_kb.errors import blocked

        raise blocked(
            "A critical blocker applies to this study; a narrow diagnostic exception is required.",
            blockers=applying,
            hint=(
                "The exception must name the blocker, operation, study revision, reason, and expiry; "
                "it does not waive the blocker for paper conclusions."
            ),
        )
    for field in DIAGNOSTIC_EXCEPTION_FIELDS:
        if not exception.get(field):
            raise schema_validation_failed(
                "Diagnostic exceptions require all bound fields.",
                missing=field,
                required=list(DIAGNOSTIC_EXCEPTION_FIELDS),
            )
    if exception["blocker_ref"] not in applying:
        raise schema_validation_failed(
            "The diagnostic exception must name the blocker it actually addresses.",
            blocker_ref=exception["blocker_ref"],
            applying=applying,
        )
    return exception


def handle_operation(mutation: Mutation, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = mutation.ctx
    _require_execution(ctx)
    if operation == "execute_prepare":
        return _prepare(mutation, payload)
    if operation == "execute_launch":
        return _launch(mutation, payload)
    if operation == "execute_cancel":
        return _cancel(mutation, payload)
    if operation == "execute_reconcile":
        return _reconcile(mutation, payload)
    raise schema_validation_failed("Unknown execution operation.", operation=operation)


def _prepare(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = mutation.ctx
    conn = ctx.conn
    study = repo.resolve_ref(conn, ctx.project_id, payload["study_ref"])
    if study["kind"] != "study":
        raise schema_validation_failed("Execution requires a study record.", kind=study["kind"])
    state = repo.parse_state(study)
    if state.get("study_state") not in ("planned", "active"):
        raise schema_validation_failed(
            "Only planned or active studies can be prepared for execution.",
            study_state=state.get("study_state"),
        )
    if not state.get("executable"):
        raise schema_validation_failed("The study is not declared executable.")
    trial = dict(payload["trial"])
    diagnostic_exception = _validate_diagnostic_exception(ctx, study, trial)
    if diagnostic_exception:
        trial["exceptions"] = list(trial.get("exceptions") or []) + [diagnostic_exception]
    protocol = state.get("protocol") or {}
    method_profile = trial.get("method_profile") or protocol.get("method_profile") or ctx.policy.get(
        "provenance", {}
    ).get("default_profile", "numerical")
    intended_use = trial.get("intended_use", "exploratory")
    deterministic = bool(trial.get("deterministic", False))
    allow_dirty = bool(trial.get("allow_dirty_snapshot", False))
    workspace_identity = state.get("workspace_identity") or ctx.routing.project_id
    design = trial.get("design")
    created_slots: list[dict[str, Any]] = []
    if design:
        rows = slots.expand_design(design)
        replicate = trial.get("replicate_identity")
        for row in rows:
            conditions = slots.normalize_trial({"conditions": row})
            created_slots.append(
                slots.ensure_trial_slot(
                    conn,
                    project_id=ctx.project_id,
                    study_object_id=study["object_id"],
                    study_revision=study["revision"],
                    conditions=conditions,
                    replicate_identity=replicate,
                    required=bool(trial.get("required", True)),
                    seq=mutation.commit_seq,
                )
            )
        if not trial.get("conditions"):
            return {
                "phase": "slots_created",
                "study_ref": {"object_id": study["object_id"], "revision": study["revision"]},
                "slot_count": len(created_slots),
                "slots": created_slots,
                "coverage": slots.coverage(conn, ctx.project_id, study["object_id"], study["revision"]),
                "note": "Provide conditions to prepare an attempt.",
            }
    conditions = slots.normalize_trial(trial)
    slot = slots.ensure_trial_slot(
        conn,
        project_id=ctx.project_id,
        study_object_id=study["object_id"],
        study_revision=study["revision"],
        conditions=conditions,
        replicate_identity=trial.get("replicate_identity"),
        required=bool(trial.get("required", True)),
        seq=mutation.commit_seq,
    )
    attempt_no = slots.next_attempt_no(conn, slot["slot_id"])
    manifest = build_manifest(
        project_id=ctx.project_id,
        study_object_id=study["object_id"],
        study_revision=study["revision"],
        trial_key=slot["trial_key_string"],
        attempt_no=attempt_no,
        protocol_revision=trial.get("protocol_revision") or str(study["revision"]),
        policy_revision=ctx.policy_revision,
        trial=trial,
        workspace_identity=workspace_identity,
        repo_root=payload.get("repo_root"),
    )
    provenance = validate_manifest_provenance(
        manifest,
        method_profile=method_profile,
        intended_use=intended_use,
        deterministic_declared=deterministic,
        allow_dirty_snapshot=allow_dirty,
    )
    execution_id = new_id()
    run_state = {
        "subkind": "attempt",
        "study_ref": {"object_id": study["object_id"], "revision": study["revision"]},
        "trial_slot_ref": {"object_id": study["object_id"], "revision": study["revision"]},
        "attempt_no": attempt_no,
        "manifest": manifest,
        "manifest_hash": manifest_fingerprint(manifest),
        "execution_id": execution_id,
        "status": "queued",
        "validity": "unknown",
    }
    run = create_object(
        mutation,
        kind="run",
        subkind="attempt",
        title=f"Run attempt {attempt_no} for {study['title'][:120]}",
        state_json=run_state,
        record_state="active",
    )
    conn.execute(
        """
        INSERT INTO run_attempts
          (attempt_id, project_id, run_object_id, run_revision, slot_id, attempt_no, execution_id,
           manifest_json, manifest_hash, created_seq, created_at)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            new_id(),
            ctx.project_id,
            run["object_id"],
            slot["slot_id"],
            attempt_no,
            execution_id,
            json.dumps(manifest, sort_keys=True),
            run_state["manifest_hash"],
            mutation.commit_seq,
        ),
    )
    lease_results = []
    for resource in trial.get("resources") or []:
        if isinstance(resource, str):
            resource = {"resource_id": resource}
        lease = leases.acquire_lease(
            conn,
            project_id=ctx.project_id,
            resource_id=resource["resource_id"],
            execution_id=execution_id,
            seq=mutation.commit_seq,
            ttl_seconds=int(resource.get("ttl_seconds", leases.DEFAULT_LEASE_TTL_SECONDS)),
        )
        lease_results.append(lease)
    intent = {
        "execution_id": execution_id,
        "manifest_hash": run_state["manifest_hash"],
        "project_id": ctx.project_id,
        "study_ref": run_state["study_ref"],
        "trial_key": slot["trial_key_string"],
        "attempt_no": attempt_no,
        "adapter": trial.get("adapter", "import_only"),
        "invocation": manifest["invocation"],
        "authorization": manifest["authorization"],
        "policy_revision": ctx.policy_revision,
        "controller_epoch": ctx.epoch,
        "slurm_script": trial.get("slurm_script"),
        "external_ref": trial.get("external_ref"),
    }
    outbox_id = outbox.append_intent(
        conn,
        project_id=ctx.project_id,
        execution_id=execution_id,
        intent=intent,
        seq=mutation.commit_seq,
    )
    return {
        "phase": "prepared",
        "execution_id": execution_id,
        "outbox_id": outbox_id,
        "run": run,
        "slot": slot,
        "attempt_no": attempt_no,
        "manifest_hash": run_state["manifest_hash"],
        "provenance": provenance,
        "leases": lease_results,
        "study_revision": study["revision"],
    }


def _launch(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = mutation.ctx
    conn = ctx.conn
    execution_id = payload["execution_id"]
    record = outbox.fetch_intent(conn, ctx.project_id, execution_id)
    if record["state"] == "dispatched":
        from research_kb.errors import execution_pending

        raise execution_pending(execution_id)
    intent = json.loads(record["intent_json"])
    adapter_name = payload.get("adapter") or intent.get("adapter") or "import_only"
    allowed = ctx.policy.get("execution", {}).get("adapters", [])
    if adapter_name not in allowed:
        raise capability_unavailable(
            "The requested adapter is not enabled by policy.",
            adapter=adapter_name,
            allowed=allowed,
        )
    adapter = get_adapter(adapter_name)
    if mutation.dry_run:
        return {
            "phase": "validated",
            "execution_id": execution_id,
            "adapter": adapter_name,
            "note": "Dry run: no dispatch performed.",
        }
    result = adapter.dispatch(intent, spool_dir=ctx.routing.spool_dir)
    outbox.mark_dispatched(conn, project_id=ctx.project_id, execution_id=execution_id, dispatched=True)
    record_receipt(
        conn,
        project_id=ctx.project_id,
        execution_id=execution_id,
        phase=result.phase,
        payload={"external_ref": result.external_ref, **result.detail},
        external_ref=result.external_ref,
        generation=None,
        seq=mutation.commit_seq,
    )
    observe(
        conn,
        project_id=ctx.project_id,
        subject_kind="run_attempt",
        subject_id=execution_id,
        status=result.phase,
        detail=result.detail,
    )
    return {
        "phase": result.phase,
        "execution_id": execution_id,
        "external_ref": result.external_ref,
        "idempotent_submission": result.idempotent_submission,
        "detail": result.detail,
        "note": "Launch accepted is not a completed run.",
    }


def _cancel(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = mutation.ctx
    conn = ctx.conn
    execution_id = payload["execution_id"]
    attempt = conn.execute(
        "SELECT * FROM run_attempts WHERE project_id = ? AND execution_id = ?",
        (ctx.project_id, execution_id),
    ).fetchone()
    if attempt is None:
        raise not_found("No run attempt is registered for this execution ID.", execution_id=execution_id)
    intent = json.loads(
        conn.execute(
            "SELECT intent_json FROM dispatch_outbox WHERE project_id = ? AND execution_id = ?",
            (ctx.project_id, execution_id),
        ).fetchone()["intent_json"]
    )
    adapter_name = payload.get("adapter") or intent.get("adapter") or "import_only"
    adapter = get_adapter(adapter_name)
    if mutation.dry_run:
        return {"phase": "validated", "execution_id": execution_id, "adapter": adapter_name}
    result = adapter.cancel(intent.get("external_ref", ""), spool_dir=ctx.routing.spool_dir)
    observe(
        conn,
        project_id=ctx.project_id,
        subject_kind="run_attempt",
        subject_id=execution_id,
        status="cancel_requested",
        detail=result,
    )
    return {
        "phase": "cancel_requested",
        "execution_id": execution_id,
        "verified_termination": result.get("verified_termination", False),
        "note": "A successful cancel request is not verified termination; resources stay held until verified.",
    }


def _reconcile(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    return reconcile_receipt(
        mutation,
        execution_id=payload["execution_id"],
        phase=payload["phase"],
        payload=payload["payload"],
        external_ref=payload.get("external_ref"),
        generation=payload.get("generation"),
    )
