from __future__ import annotations

from typing import Any

from research_kb.config import ProjectRouting
from research_kb.errors import KBError, schema_validation_failed
from research_kb.service.backup import create_backup
from research_kb.service.context import ServiceContext, open_service
from research_kb.service.exports import (
    export_jsonl,
    export_markdown,
    export_rocrate,
    write_export,
)
from research_kb.service.imports import run_import
from research_kb.service.objects import find_object_by_alias_or_id, get_object_bundle
from research_kb.service.proposals import apply as apply_proposal
from research_kb.service.proposals import approve as approve_proposal
from research_kb.service.proposals import list_proposals, propose
from research_kb.service.retrieval import (
    changes,
    context,
    resolve_snapshot,
    search,
    status,
)
from research_kb.service.verify import verify
from research_kb.storage.db import encode_snapshot_cursor, new_id, read_tx
from research_kb.version import API_VERSION

class ResearchKB:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    @classmethod
    def open(
        cls,
        routing: ProjectRouting | None = None,
        *,
        actor_id: str | None = None,
        read_only: bool = False,
        project_root: str | None = None,
        session_id: str | None = None,
    ) -> "ResearchKB":
        return cls(
            open_service(
                routing,
                project_root=project_root,
                actor_id=actor_id,
                read_only=read_only,
                session_id=session_id,
            )
        )

    def close(self) -> None:
        self.ctx.close()

    def __enter__(self) -> "ResearchKB":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _envelope(
        self,
        *,
        status: str = "ok",
        request_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "api_version": API_VERSION,
            "project_id": self.ctx.project_id,
            "controller_epoch": self.ctx.epoch,
            "status": status,
        }
        if request_id:
            payload["request_id"] = request_id
        payload.update(extra)
        return payload

    def capabilities(self) -> dict[str, Any]:
        report = self.ctx.capabilities_report()
        capabilities = report.pop("capabilities")
        return self._envelope(
            status="ok",
            snapshot={
                "cursor": encode_snapshot_cursor(self.ctx.epoch, self.ctx.latest_seq()),
                "historical": False,
            },
            authenticated_capabilities=capabilities,
            **report,
        )

    def status(self, *, scope: str | None = None) -> dict[str, Any]:
        scope_ref = None
        if scope:
            match = find_object_by_alias_or_id(self.ctx.conn, self.ctx.project_id, scope)
            if match["status"] != "unique":
                raise schema_validation_failed(
                    "Ambiguous scope alias; resolve it explicitly.", candidates=match["matches"]
                )
            scope_ref = {"object_id": match["matches"][0]}
        result = status(self.ctx, scope_ref=scope_ref)
        return self._envelope(snapshot=result["snapshot"], result=result)

    def get(self, refs: list[Any]) -> dict[str, Any]:
        from research_kb.service.objects import resolve_object_token

        if len(refs) > 100:
            raise schema_validation_failed("Bound the batch: at most 100 refs per get call.", count=len(refs))
        bundles = []
        with read_tx(self.ctx.conn):
            for ref in refs:
                if isinstance(ref, str):
                    object_id = resolve_object_token(ref, self.ctx.conn, self.ctx.project_id)
                    revision = None
                else:
                    object_id = resolve_object_token(
                        ref["object_id"], self.ctx.conn, self.ctx.project_id
                    )
                    revision = ref.get("revision")
                bundle = get_object_bundle(
                    self.ctx.conn,
                    self.ctx.project_id,
                    object_id,
                    revision=revision,
                    include_history=bool(ref.get("history")) if isinstance(ref, dict) else False,
                )
                bundle["source_spans"] = [
                    citation for citation in bundle.get("citations", []) if citation.get("excerpt")
                ]
                bundles.append(bundle)
        return self._envelope(
            snapshot={
                "cursor": encode_snapshot_cursor(self.ctx.epoch, self.ctx.latest_seq()),
                "historical": False,
            },
            records=bundles,
        )

    def search(
        self,
        *,
        query: str,
        as_of_cursor: str | None = None,
        kind: str | None = None,
        subkind: str | None = None,
        limit: int = 50,
        advanced: bool = False,
        page_cursor: str | None = None,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        result = search(
            self.ctx,
            query=query,
            as_of_cursor=as_of_cursor,
            kind=kind,
            subkind=subkind,
            limit=limit,
            advanced=advanced,
            page_cursor=page_cursor,
            effective_at=effective_at,
        )
        return self._envelope(snapshot=result["snapshot"], result=result)

    def context(
        self,
        *,
        mode: str,
        query: str | None = None,
        focus_refs: list[Any] | None = None,
        budget_tokens: int | None = None,
        as_of_cursor: str | None = None,
        limit: int = 40,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        with read_tx(self.ctx.conn):
            result = context(
                self.ctx,
                mode=mode,
                query=query,
                focus_refs=focus_refs,
                budget_tokens=budget_tokens,
                as_of_cursor=as_of_cursor,
                limit=limit,
                effective_at=effective_at,
            )
        return self._envelope(snapshot=result["snapshot"], result=result)

    def changes(
        self,
        *,
        after: str | None = None,
        page_cursor: str | None = None,
        limit: int = 50,
        acknowledge: bool = False,
    ) -> dict[str, Any]:
        result = changes(
            self.ctx,
            after=after,
            page_cursor=page_cursor,
            limit=limit,
            acknowledge=acknowledge,
        )
        return self._envelope(snapshot=result["snapshot"], result=result)

    def propose(
        self,
        *,
        operations: list[dict[str, Any]],
        reason: str | None = None,
        request_id: str | None = None,
        persist: bool = True,
        auto_apply: bool = False,
    ) -> dict[str, Any]:
        request_id = request_id or new_id()
        result = propose(
            self.ctx,
            operations=operations,
            reason=reason,
            request_id=request_id,
            persist=persist,
            auto_apply=auto_apply,
        )
        committed = bool(result.get("applied"))
        return self._envelope(
            status="applied" if committed else "stored" if result.get("persisted") else "validated",
            request_id=request_id,
            committed=committed,
            commit_seq=result.get("commit_seq"),
            created=result.get("created", []),
            updated=result.get("updated", []),
            details=result.get("details", {}),
            verified=committed,
            snapshot={
                "cursor": encode_snapshot_cursor(self.ctx.epoch, self.ctx.latest_seq()),
                "historical": False,
            },
            proposal=result,
        )

    def propose_draft(
        self,
        *,
        draft: dict[str, Any],
        reason: str | None = None,
        request_id: str | None = None,
        persist: bool = True,
        auto_apply: bool = False,
    ) -> dict[str, Any]:
        from research_kb.service.drafts import propose_draft

        request_id = request_id or new_id()
        result = propose_draft(
            self.ctx,
            draft=draft,
            request_id=request_id,
            reason=reason,
            persist=persist,
            auto_apply=auto_apply,
        )
        committed = bool(result.get("applied"))
        return self._envelope(
            status=result.get("status", "stored"),
            request_id=request_id,
            committed=committed,
            commit_seq=result.get("commit_seq"),
            created=result.get("created", []),
            updated=result.get("updated", []),
            details=result.get("details", {}),
            verified=committed,
            snapshot={
                "cursor": encode_snapshot_cursor(self.ctx.epoch, self.ctx.latest_seq()),
                "historical": False,
            },
            proposal=result,
        )

    def apply(
        self,
        *,
        proposal_id: str | None = None,
        proposal_hash: str | None = None,
        request_id: str | None = None,
        approval_id: str | None = None,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or new_id()
        result = apply_proposal(
            self.ctx,
            proposal_id=proposal_id,
            proposal_hash=proposal_hash,
            request_id=request_id,
            approval_id=approval_id,
            approval_token=approval_token,
        )
        committed = bool(result.get("committed"))
        return self._envelope(
            status="applied" if committed else result.get("status", "unknown"),
            request_id=request_id,
            committed=committed,
            commit_seq=result.get("commit_seq"),
            created=result.get("created", []),
            updated=result.get("updated", []),
            details=result.get("details", {}),
            verified=result.get("verified", False),
            result=result,
        )

    def approve(self, *, proposal_id: str, ttl_seconds: int = 3600) -> dict[str, Any]:
        result = approve_proposal(self.ctx, proposal_id=proposal_id, ttl_seconds=ttl_seconds)
        return self._envelope(status="approval_issued", **result)

    def proposals(self, *, status: str | None = None) -> dict[str, Any]:
        return self._envelope(status="ok", proposals=list_proposals(self.ctx, status=status))

    def import_items(
        self,
        *,
        connector_namespace: str,
        items: list[dict[str, Any]],
        request_id: str | None = None,
        reason: str | None = None,
        dry_run: bool = False,
        source_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or new_id()
        result = run_import(
            self.ctx,
            connector_namespace=connector_namespace,
            items=items,
            request_id=request_id,
            reason=reason,
            dry_run=dry_run,
            source_scope=source_scope,
        )
        return self._envelope(
            status=result["status"],
            request_id=request_id,
            committed=not dry_run,
            snapshot={
                "cursor": encode_snapshot_cursor(self.ctx.epoch, self.ctx.latest_seq()),
                "historical": False,
            },
            result=result,
        )

    def verify(
        self,
        *,
        scope: str | None = None,
        checks: list[str] | None = None,
        sample: int = 50,
    ) -> dict[str, Any]:
        scope_id = None
        if scope:
            match = find_object_by_alias_or_id(self.ctx.conn, self.ctx.project_id, scope)
            if match["status"] != "unique":
                raise schema_validation_failed(
                    "Ambiguous scope alias; resolve it explicitly.", candidates=match["matches"]
                )
            scope_id = match["matches"][0]
        result = verify(self.ctx, checks=checks, scope_ref=scope_id, sample=sample)
        return self._envelope(status="ok", result=result)

    def export(
        self,
        *,
        format: str,
        output: str,
        as_of_cursor: str | None = None,
        sections: list[str] | None = None,
    ) -> dict[str, Any]:
        if format == "markdown":
            result = export_markdown(self.ctx, as_of_cursor=as_of_cursor, sections=sections)
        elif format == "jsonl":
            result = export_jsonl(self.ctx, as_of_cursor=as_of_cursor)
        elif format == "rocrate":
            result = export_rocrate(self.ctx, as_of_cursor=as_of_cursor)
        elif format == "draft":
            from research_kb.service.drafts import build_draft, write_draft

            draft = build_draft(self.ctx, as_of_cursor=as_of_cursor)
            written = write_draft(self.ctx, output, draft)
            return self._envelope(
                status="ok",
                snapshot=draft["cursor"],
                export=written,
                omissions=[],
            )
        else:
            raise schema_validation_failed("Unknown export format.", allowed=["markdown", "jsonl", "rocrate", "draft"])
        written = write_export(self.ctx, output, result)
        return self._envelope(
            status="ok",
            snapshot=result.get("cursor"),
            export=written,
            omissions=result.get("omissions", []),
        )

    def backup(self, *, destination: str | None = None, tag: str = "manual") -> dict[str, Any]:
        result = create_backup(self.ctx, destination=destination, tag=tag)
        return self._envelope(status="ok", backup=result)

    def execute(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        request_id: str | None = None,
        approval_id: str | None = None,
        approval_token: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or new_id()
        allowed = {
            "execute_prepare",
            "execute_launch",
            "execute_cancel",
            "execute_reconcile",
        }
        if operation not in allowed:
            raise schema_validation_failed("Unknown execution operation.", allowed=sorted(allowed))
        if not self.ctx.policy.get("execution", {}).get("enabled", False):
            from research_kb.errors import capability_unavailable

            raise capability_unavailable(
                "The optional execution module is disabled by policy.",
                hint="Launch/cancel tools become available only after their safety and recovery tests pass.",
            )
        result = propose(
            self.ctx,
            operations=[{"op": operation, "payload": payload}],
            reason=reason or f"Execution operation {operation}.",
            request_id=request_id,
            persist=True,
            auto_apply=False,
        )
        if approval_id or approval_token:
            apply_result = apply_proposal(
                self.ctx,
                proposal_id=result["proposal_id"],
                proposal_hash=None,
                request_id=f"{request_id}:apply",
                approval_id=approval_id,
                approval_token=approval_token,
            )
            return self._envelope(
                status="applied",
                request_id=request_id,
                committed=True,
                commit_seq=apply_result.get("commit_seq"),
                created=apply_result.get("created", []),
                updated=apply_result.get("updated", []),
                result=apply_result,
            )
        return self._envelope(
            status="stored",
            request_id=request_id,
            committed=False,
            proposal=result,
            required_approval=True,
        )

    def raw_snapshot(self) -> tuple[str, int]:
        seq, historical = resolve_snapshot(self.ctx, None)
        return encode_snapshot_cursor(self.ctx.epoch, seq), seq


def error_envelope(exc: KBError, *, project_id: str | None = None, epoch: str | None = None) -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "project_id": project_id,
        "controller_epoch": epoch,
        "status": "error",
        "error": exc.to_dict(),
    }
