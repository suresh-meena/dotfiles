from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from research_kb.config import ENV_ACTOR, find_project_root, load_routing
from research_kb.errors import KBError, project_mismatch, schema_validation_failed
from research_kb.service.api import ResearchKB, error_envelope
from research_kb.version import API_VERSION, RUNTIME_VERSION, SCHEMA_VERSION

EXIT_CODES = {
    "SCHEMA_VALIDATION_FAILED": 3,
    "REVISION_CONFLICT": 3,
    "IDEMPOTENCY_CONFLICT": 3,
    "CURSOR_INVALID": 3,
    "NOT_FOUND": 3,
    "REFERENCE_UNRESOLVED": 3,
    "WRONG_REFERENCE_TYPE": 3,
    "UNSUPPORTED_VERSION": 3,
    "PROJECT_REQUIRED": 3,
    "PROJECT_MISMATCH": 3,
    "PERMISSION_DENIED": 4,
    "APPROVAL_REQUIRED": 4,
    "APPROVAL_STALE": 4,
    "BLOCKED": 5,
    "PROVENANCE_INCOMPLETE": 5,
    "EXECUTION_PENDING": 6,
    "EXECUTION_AMBIGUOUS": 6,
    "EPOCH_CHANGED": 6,
    "INDEX_DEGRADED": 7,
    "RETRIEVAL_INCOMPLETE": 7,
    "ARTIFACT_UNAVAILABLE": 7,
    "CAPABILITY_UNAVAILABLE": 7,
    "TEMPORARY_UNAVAILABLE": 7,
    "STORAGE_FAILURE": 8,
}


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _render(payload)


def _render(payload: dict[str, Any]) -> None:
    status = payload.get("status", "ok")
    print(f"status: {status}")
    if payload.get("project_id"):
        print(f"project: {payload['project_id']}")
    if payload.get("commit_seq") is not None:
        print(f"commit_seq: {payload['commit_seq']}")
    for key in ("created", "updated"):
        for item in payload.get(key, []) or []:
            print(f"{key}: {item.get('object_id')}@{item.get('revision')}")
    if "error" in payload:
        error = payload["error"]
        print(f"{error['code']}: {error['message']}", file=sys.stderr)
        if error.get("recovery"):
            print(f"recovery: {error['recovery']}", file=sys.stderr)
        return
    result = payload.get("result") or payload.get("records") or payload.get("proposal") or payload.get("proposals")
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _read_json_file(path: str) -> Any:
    from research_kb.domain.canonical import parse_json_bytes

    try:
        return parse_json_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise schema_validation_failed(f"Could not read JSON file: {exc}") from exc


def _resolve_routing(args: argparse.Namespace):
    root = getattr(args, "root", None)
    routing = load_routing(root) if root else load_routing()
    assert routing is not None
    expected = getattr(args, "project", None)
    if expected and expected != routing.project_id:
        raise project_mismatch(
            "The --project UUID does not match the routing file.",
            routing.project_id,
            expected,
        )
    return routing


def _open_api(args: argparse.Namespace) -> ResearchKB:
    routing = _resolve_routing(args)
    actor = getattr(args, "actor", None) or os.environ.get(ENV_ACTOR)
    return ResearchKB.open(routing, actor_id=actor, session_id=getattr(args, "session", None))


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.service.context import initialize_project

    root = Path(args.root or os.getcwd()).resolve()
    routing = initialize_project(
        root,
        args.name,
        namespace=args.namespace,
        state_root=args.state_root,
        actor_id=args.actor,
    )
    return {
        "api_version": API_VERSION,
        "status": "initialized",
        "project_id": routing.project_id,
        "routing_file": str(routing.config_path),
        "state_dir": str(routing.state_dir),
        "local_state": True,
        "note": "The knowledge base state is stored under the working directory by default.",
    }


def cmd_capabilities(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.capabilities()


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.status(scope=args.scope)


def cmd_get(args: argparse.Namespace) -> dict[str, Any]:
    refs: list[Any] = []
    for ref in args.refs:
        refs.append({"object_id": ref, "revision": args.revision, "history": args.history})
    with _open_api(args) as api:
        return api.get(refs)


def cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.search(
            query=args.query,
            as_of_cursor=args.as_of,
            kind=args.kind,
            subkind=args.subkind,
            limit=args.limit,
            advanced=args.advanced,
            page_cursor=args.page,
            effective_at=args.effective_at,
        )


def cmd_context(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.context(
            mode=args.mode,
            query=args.query,
            focus_refs=args.focus,
            budget_tokens=args.budget,
            as_of_cursor=args.as_of,
            limit=args.limit,
            effective_at=args.effective_at,
        )


def cmd_changes(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.changes(
            after=args.after,
            page_cursor=args.page,
            limit=args.limit,
            acknowledge=args.acknowledge,
        )


def cmd_propose(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_json_file(args.file)
    if args.draft:
        with _open_api(args) as api:
            return api.propose_draft(
                draft=payload,
                reason=args.reason,
                request_id=args.request_id,
                persist=not args.dry_run,
                auto_apply=args.auto_apply,
            )
    operations = payload.get("operations") if isinstance(payload, dict) else payload
    if not isinstance(operations, list):
        raise schema_validation_failed("The proposal file must contain an operations list.")
    with _open_api(args) as api:
        return api.propose(
            operations=operations,
            reason=args.reason or (payload.get("reason") if isinstance(payload, dict) else None),
            request_id=args.request_id,
            persist=not args.dry_run,
            auto_apply=args.auto_apply,
        )


def cmd_approve(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.approve(proposal_id=args.proposal, ttl_seconds=args.ttl)


def cmd_apply(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.apply(
            proposal_id=args.proposal,
            proposal_hash=args.proposal_hash,
            request_id=args.request_id,
            approval_id=args.approval_id,
            approval_token=args.approval_token,
        )


def cmd_proposals(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.proposals(status=args.status)


def cmd_import(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_json_file(args.file)
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise schema_validation_failed("The import file must contain an items list.")
    with _open_api(args) as api:
        return api.import_items(
            connector_namespace=args.connector,
            items=items,
            request_id=args.request_id,
            reason=args.reason,
            dry_run=args.dry_run,
            source_scope={"approved": True, "connector": args.connector},
        )


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    checks = [check.strip() for check in args.checks.split(",") if check.strip()] if args.checks else None
    with _open_api(args) as api:
        return api.verify(scope=args.scope, checks=checks, sample=args.sample)


def cmd_export(args: argparse.Namespace) -> dict[str, Any]:
    sections = [section.strip() for section in args.sections.split(",")] if args.sections else None
    with _open_api(args) as api:
        return api.export(
            format=args.format,
            output=args.output,
            as_of_cursor=args.as_of,
            sections=sections,
        )


def cmd_execute(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_json_file(args.file) if args.file else {}
    with _open_api(args) as api:
        return api.execute(
            operation=args.operation,
            payload=payload,
            request_id=args.request_id,
            approval_id=args.approval_id,
            approval_token=args.approval_token,
            reason=args.reason,
        )


def cmd_backup(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return api.backup(destination=args.output, tag=args.tag)


def cmd_restore(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.service.backup import restore_backup

    result = restore_backup(source=args.source, destination=args.destination)
    return {"api_version": API_VERSION, "status": "restored", **result}


def cmd_migrate(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.storage import migrations
    from research_kb.storage.db import connect, write_tx

    routing = _resolve_routing(args)
    conn = connect(routing.db_path)
    try:
        with write_tx(conn):
            pass
        applied = migrations.apply_all(conn)
    finally:
        conn.close()
    return {"api_version": API_VERSION, "status": "migrated", "applied_versions": applied}


def cmd_receipts(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.service.imports import list_receipts

    with _open_api(args) as api:
        return {
            "api_version": API_VERSION,
            "status": "ok",
            "receipts": list_receipts(api.ctx, connector_namespace=args.connector),
        }


def cmd_outbox(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.execution.outbox import pending_intents

    with _open_api(args) as api:
        rows = pending_intents(api.ctx.conn, api.ctx.project_id)
        return {
            "api_version": API_VERSION,
            "status": "ok",
            "pending": [
                {
                    "outbox_id": row["outbox_id"],
                    "execution_id": row["execution_id"],
                    "state": row["state"],
                    "attempts": row["attempts"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }


def cmd_embeddings_drain(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.retrieval import embeddings
    from research_kb.storage.db import write_tx

    with _open_api(args) as api:
        with write_tx(api.ctx.conn):
            result = embeddings.drain(api.ctx, limit=args.limit)
        return {"api_version": API_VERSION, "status": "ok", **result}


def cmd_embeddings_status(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.retrieval import embeddings

    with _open_api(args) as api:
        provider, reason = embeddings.provider_from_policy(api.ctx.policy)
        return {
            "api_version": API_VERSION,
            "status": "ok",
            "provider": provider.identity.provider,
            "reason": reason,
            "watermark": embeddings.watermark(api.ctx),
            "pending_jobs": api.ctx.conn.execute(
                "SELECT COUNT(*) AS c FROM embedding_outbox WHERE project_id = ? AND state IN ('pending','failed')",
                (api.ctx.project_id,),
            ).fetchone()["c"],
            "note": "Embeddings are optional and disabled by default; exact and FTS reads remain available.",
        }


def cmd_gc(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.errors import permission_denied
    from research_kb.storage import blobs

    with _open_api(args) as api:
        if args.apply and "administer" not in api.ctx.capabilities():
            raise permission_denied(
                "Blob garbage collection requires the administer capability.",
                capability="administer",
                actor=api.ctx.actor_id,
            )
        plan = blobs.gc_plan(
            api.ctx.conn,
            root=api.ctx.routing.sources_dir.parent,
            grace_seconds=args.grace,
            dry_run=not args.apply,
        )
        return {
            "api_version": API_VERSION,
            "status": "applied" if args.apply else "dry_run",
            "candidates": plan,
            "deleted": [item["blob"] for item in plan] if args.apply else [],
            "note": (
                "GC considers all retained historical references and applies a grace period; "
                "ordinary agents do not perform canonical evidence deletion."
            ),
        }


def cmd_backup_verify(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.service.backup_verify import verify_backup

    result = verify_backup(args.dir, sample_blobs=args.sample)
    return {"api_version": API_VERSION, "status": "ok" if result["ok"] else "error", **result}


def cmd_legacy_inventory(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.service.legacy import inventory

    result = inventory(args.db)
    return {"api_version": API_VERSION, "status": "ok", **result}


def cmd_fts_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.storage import migrations
    from research_kb.storage.db import connect
    from research_kb.storage.search import check_integrity, rebuild_projections

    routing = _resolve_routing(args)
    conn = connect(routing.db_path)
    try:
        migrations.ensure_registry(conn)
        before = check_integrity(conn)

        def section_resolver(project_id: str, object_id: str, revision: int):
            from research_kb.ingestion import extract as extraction
            from research_kb.storage import blobs

            row = conn.execute(
                """
                SELECT extraction_blob_hash, status FROM source_extractions
                WHERE project_id = ? AND source_object_id = ? AND source_revision = ?
                ORDER BY created_seq DESC LIMIT 1
                """,
                (project_id, object_id, revision),
            ).fetchone()
            if row is None or not row["extraction_blob_hash"]:
                return []
            text = blobs.read_blob(
                routing.extractions_dir.parent, row["extraction_blob_hash"]
            ).decode("utf-8", errors="replace")
            return extraction.attach_line_ranges(text, extraction.chunk_markdown(text))

        rebuilt = rebuild_projections(
            conn, project_id=routing.project_id, section_resolver=section_resolver
        )
        after = check_integrity(conn)
    finally:
        conn.close()
    return {
        "api_version": API_VERSION,
        "status": "rebuilt",
        "before": before,
        "after": after,
        "projections": rebuilt,
    }


def cmd_lease_quarantine(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.execution import leases
    from research_kb.storage.db import write_tx

    with _open_api(args) as api:
        with write_tx(api.ctx.conn):
            quarantined = leases.quarantine_expired(api.ctx.conn, api.ctx.project_id)
        return {
            "api_version": API_VERSION,
            "status": "quarantined",
            "lease_ids": quarantined,
            "note": "Expiry does not prove a process stopped; quarantine until termination is verified.",
        }


def cmd_actor_add(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.storage.db import write_tx
    from research_kb.storage.repo import ensure_actor

    capabilities = [cap.strip() for cap in args.capabilities.split(",") if cap.strip()] if args.capabilities else []
    roles = [role.strip() for role in args.roles.split(",") if role.strip()] if args.roles else ["reader"]
    with _open_api(args) as api:
        with write_tx(api.ctx.conn):
            ensure_actor(
                api.ctx.conn,
                args.actor_id,
                kind=args.kind,
                display_name=args.display_name or args.actor_id,
                roles=roles,
                capabilities=capabilities,
            )
        return {
            "api_version": API_VERSION,
            "status": "actor_registered",
            "actor_id": args.actor_id,
            "roles": roles,
            "capabilities": capabilities,
        }


def cmd_actor_list(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        rows = api.ctx.conn.execute(
            "SELECT actor_id, kind, display_name, roles_json, capabilities_json FROM actors ORDER BY actor_id"
        ).fetchall()
        return {"api_version": API_VERSION, "status": "ok", "actors": [dict(row) for row in rows]}


def cmd_schemas(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.domain.schemas import available_record_schemas

    return {"api_version": API_VERSION, "status": "ok", "schemas": available_record_schemas()}


def cmd_relations(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.domain.relation_registry import registry_document

    return {"api_version": API_VERSION, "status": "ok", "predicates": registry_document()}


def cmd_operations(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.domain.transition_registry import operation_document

    return {"api_version": API_VERSION, "status": "ok", "operations": operation_document()}


def cmd_policy_show(args: argparse.Namespace) -> dict[str, Any]:
    with _open_api(args) as api:
        return {
            "api_version": API_VERSION,
            "status": "ok",
            "policy_revision": api.ctx.policy_revision,
            "policy": api.ctx.policy,
        }


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    from research_kb.storage.db import (
        connect,
        probe_features,
        wal_reset_fix_present,
    )

    routing = None
    routing_path = None
    root = Path(args.root or os.getcwd()).resolve()
    candidate = find_project_root(root)
    if candidate is not None:
        routing = load_routing(candidate)
        assert routing is not None
        routing_path = str(routing.config_path)
    features: dict[str, Any] = {"sqlite_version": __import__("sqlite3").sqlite_version}
    db = Path(args.db) if args.db else None
    if db is None and routing is not None and routing.db_path.exists():
        db = routing.db_path
    if db is not None and db.exists():
        conn = connect(db, read_only=True)
        try:
            features = probe_features(conn)
        finally:
            conn.close()
    import shutil

    return {
        "api_version": API_VERSION,
        "status": "ok",
        "runtime_version": RUNTIME_VERSION,
        "schema_version": SCHEMA_VERSION,
        "python_version": sys.version.split()[0],
        "routing_file": routing_path,
        "routing": routing.to_dict() if routing else None,
        "database": str(db) if db else None,
        "sqlite_features": features,
        "wal_reset_fix_present": wal_reset_fix_present(features["sqlite_version"]),
        "rkb_discoverable": shutil.which("rkb"),
        "note": (
            "doctor.py inspects local prerequisites and routing; it does not open a project database "
            "unless one already exists, contact a controller, or assert backend readiness."
        ),
    }


def cmd_version(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
    }


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _add_json_flag(child)
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit structured JSON on stdout.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rkb",
        description="Research knowledge base runtime. State is stored locally under the project directory by default.",
    )
    parser.add_argument("--project", help="Expected project UUID; must match the routing file.")
    parser.add_argument("--root", help="Project root containing .research/project.toml.")
    parser.add_argument("--actor", help="Authenticated actor ID for this connection.")
    parser.add_argument("--session", help="Session ID used for cursors and handoffs.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON on stdout.")
    parser.add_argument("--version", action="store_true", help="Print version information.")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Initialize project routing and the local state store.")
    init.add_argument("name")
    init.add_argument("--namespace")
    init.add_argument("--state-root", help="State root, relative to the project root by default.")
    init.set_defaults(func=cmd_init)

    capabilities = sub.add_parser("capabilities", help="Report versions, capabilities, and health.")
    capabilities.set_defaults(func=cmd_capabilities)

    status = sub.add_parser("status", help="Project status: goals, work, blockers, reviews.")
    status.add_argument("--scope")
    status.set_defaults(func=cmd_status)

    get = sub.add_parser("get", help="Fetch canonical revisions and source spans.")
    get.add_argument("refs", nargs="+")
    get.add_argument("--revision", type=int)
    get.add_argument("--history", action="store_true")
    get.set_defaults(func=cmd_get)

    search = sub.add_parser("search", help="Lexical search over titles, bodies, aliases, and source sections.")
    search.add_argument("query")
    search.add_argument("--kind")
    search.add_argument("--subkind")
    search.add_argument("--as-of")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--advanced", action="store_true")
    search.add_argument("--page", help="Page cursor from a previous search result.")
    search.add_argument("--effective-at", help="ISO timestamp; filter applicability intervals.")
    search.set_defaults(func=cmd_search)

    context = sub.add_parser("context", help="Build a focused evidence package.")
    context.add_argument("--mode", required=True)
    context.add_argument("--query")
    context.add_argument("--focus", action="append", default=[])
    context.add_argument("--budget", type=int)
    context.add_argument("--as-of")
    context.add_argument("--limit", type=int, default=40)
    context.add_argument("--effective-at", help="ISO timestamp; filter applicability intervals.")
    context.set_defaults(func=cmd_context)

    changes = sub.add_parser("changes", help="Ordered changes and impacts after a cursor.")
    changes.add_argument("--after")
    changes.add_argument("--page")
    changes.add_argument("--limit", type=int, default=50)
    changes.add_argument("--acknowledge", action="store_true")
    changes.set_defaults(func=cmd_changes)

    propose = sub.add_parser("propose", help="Validate or store a typed operation batch.")
    propose.add_argument("--file", required=True)
    propose.add_argument("--reason")
    propose.add_argument("--request-id")
    propose.add_argument("--dry-run", action="store_true")
    propose.add_argument("--auto-apply", action="store_true")
    propose.add_argument("--draft", action="store_true", help="Treat --file as an editable draft export.")
    propose.set_defaults(func=cmd_propose)

    approve = sub.add_parser("approve", help="Issue a bounded approval for a stored proposal.")
    approve.add_argument("--proposal", required=True)
    approve.add_argument("--ttl", type=int, default=3600)
    approve.set_defaults(func=cmd_approve)

    apply = sub.add_parser("apply", help="Apply a stored proposal atomically.")
    apply.add_argument("--proposal")
    apply.add_argument("--proposal-hash")
    apply.add_argument("--request-id")
    apply.add_argument("--approval-id")
    apply.add_argument("--approval-token")
    apply.set_defaults(func=cmd_apply)

    proposals = sub.add_parser("proposals", help="List proposals.")
    proposals.add_argument("--status")
    proposals.set_defaults(func=cmd_proposals)

    import_cmd = sub.add_parser("import", help="Staged, resumable source import.")
    import_cmd.add_argument("--connector", required=True)
    import_cmd.add_argument("--file", required=True)
    import_cmd.add_argument("--request-id")
    import_cmd.add_argument("--reason")
    import_cmd.add_argument("--dry-run", action="store_true")
    import_cmd.set_defaults(func=cmd_import)

    receipts = sub.add_parser("receipts", help="List import receipts.")
    receipts.add_argument("--connector")
    receipts.set_defaults(func=cmd_receipts)

    outbox = sub.add_parser("outbox", help="List pending execution intents.")
    outbox.set_defaults(func=cmd_outbox)

    verify_cmd = sub.add_parser("verify", help="Integrity, provenance, and readiness findings.")
    verify_cmd.add_argument("--scope")
    verify_cmd.add_argument("--checks")
    verify_cmd.add_argument("--sample", type=int, default=50)
    verify_cmd.set_defaults(func=cmd_verify)

    export = sub.add_parser("export", help="Generate exports.")
    export.add_argument("--format", default="markdown", choices=["markdown", "jsonl", "rocrate", "draft"])
    export.add_argument("--output", required=True)
    export.add_argument("--as-of")
    export.add_argument("--sections")
    export.set_defaults(func=cmd_export)

    execute = sub.add_parser("execute", help="Optional execution operations (disabled by default).")
    execute.add_argument("operation", choices=["prepare", "launch", "cancel", "reconcile"])
    execute.add_argument("--file")
    execute.add_argument("--request-id")
    execute.add_argument("--reason")
    execute.add_argument("--approval-id")
    execute.add_argument("--approval-token")
    execute.set_defaults(func=cmd_execute)

    backup = sub.add_parser("backup", help="Consistent online backup of database and blobs.")
    backup.add_argument("--output")
    backup.add_argument("--tag", default="manual")
    backup.set_defaults(func=cmd_backup)

    gc = sub.add_parser("gc", help="Plan or apply unreferenced blob garbage collection.")
    gc.add_argument("--apply", action="store_true", help="Delete unreferenced blobs past the grace period.")
    gc.add_argument("--grace", type=int, default=86400, help="Grace period in seconds.")
    gc.set_defaults(func=cmd_gc)

    backup_verify = sub.add_parser("backup-verify", help="Verify a backup's integrity, keys, and blobs.")
    backup_verify.add_argument("--dir", required=True)
    backup_verify.add_argument("--sample", type=int, default=20)
    backup_verify.set_defaults(func=cmd_backup_verify)

    legacy = sub.add_parser("legacy-inventory", help="Read-only inventory of a v2-style database.")
    legacy.add_argument("--db", required=True)
    legacy.set_defaults(func=cmd_legacy_inventory)

    restore = sub.add_parser("restore", help="Restore into read-only reconciliation mode.")
    restore.add_argument("--source", required=True)
    restore.add_argument("--destination", required=True)
    restore.set_defaults(func=cmd_restore)

    migrate = sub.add_parser("migrate", help="Apply pending schema migrations explicitly.")
    migrate.set_defaults(func=cmd_migrate)

    fts = sub.add_parser("fts-rebuild", help="Rebuild the FTS projection from canonical revisions.")
    fts.set_defaults(func=cmd_fts_rebuild)

    lease = sub.add_parser("lease-quarantine", help="Quarantine expired resource leases.")
    lease.set_defaults(func=cmd_lease_quarantine)

    embeddings = sub.add_parser("embeddings", help="Optional embedding cache maintenance.")
    embeddings_sub = embeddings.add_subparsers(dest="embeddings_command")
    embeddings_drain = embeddings_sub.add_parser("drain")
    embeddings_drain.add_argument("--limit", type=int, default=100)
    embeddings_drain.set_defaults(func=cmd_embeddings_drain)
    embeddings_status = embeddings_sub.add_parser("status")
    embeddings_status.set_defaults(func=cmd_embeddings_status)

    actor = sub.add_parser("actor", help="Actor administration.")
    actor_sub = actor.add_subparsers(dest="actor_command")
    actor_add = actor_sub.add_parser("add")
    actor_add.add_argument("actor_id")
    actor_add.add_argument("--kind", default="human")
    actor_add.add_argument("--display-name")
    actor_add.add_argument("--roles")
    actor_add.add_argument("--capabilities")
    actor_add.set_defaults(func=cmd_actor_add)
    actor_list = actor_sub.add_parser("list")
    actor_list.set_defaults(func=cmd_actor_list)

    schemas = sub.add_parser("schemas", help="List record schemas and required fields.")
    schemas.set_defaults(func=cmd_schemas)

    relations = sub.add_parser("relations", help="List the typed relationship registry.")
    relations.set_defaults(func=cmd_relations)

    operations = sub.add_parser("operations", help="List operation contracts.")
    operations.set_defaults(func=cmd_operations)

    policy = sub.add_parser("policy", help="Show the accepted policy profile.")
    policy.add_argument("action", nargs="?", default="show", choices=["show"])
    policy.set_defaults(func=cmd_policy_show)

    doctor = sub.add_parser("doctor", help="Local non-mutating preflight.")
    doctor.add_argument("--db")
    doctor.set_defaults(func=cmd_doctor)

    version = sub.add_parser("version", help="Print runtime version information.")
    version.set_defaults(func=cmd_version)
    for child in sub.choices.values():
        _add_json_flag(child)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version and not args.command:
        _emit(cmd_version(args), as_json=args.json)
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if args.command == "execute":
        args.operation = f"execute_{args.operation}"
    as_json = getattr(args, "json", False)
    project_id = None
    epoch = None
    try:
        payload = args.func(args)
        _emit(payload, as_json=as_json)
        error = payload.get("error") if isinstance(payload, dict) else None
        if error:
            return EXIT_CODES.get(error["code"], 1)
        return 0
    except KBError as exc:
        try:
            routing = _resolve_routing(args)
            project_id = routing.project_id
        except Exception:
            pass
        _emit(error_envelope(exc, project_id=project_id, epoch=epoch), as_json=as_json)
        return EXIT_CODES.get(exc.code, 1)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        error = KBError("STORAGE_FAILURE", f"Unhandled failure: {type(exc).__name__}: {exc}")
        _emit(error_envelope(error, project_id=project_id, epoch=epoch), as_json=as_json)
        return 8


if __name__ == "__main__":
    raise SystemExit(main())
