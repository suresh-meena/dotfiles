from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_kb.config import (
    ENV_ACTOR,
    ProjectRouting,
    default_policy,
    ensure_local_layout,
    load_policy,
    policy_hash,
    save_policy,
    write_routing,
)
from research_kb.errors import storage_failure, unsupported_version
from research_kb.storage import migrations
from research_kb.storage.db import connect, new_id, probe_features, utc_now, write_tx
from research_kb.storage.repo import (
    append_commit_event,
    capabilities_for,
    ensure_actor,
    get_actor,
)
from research_kb.version import API_VERSION, RUNTIME_VERSION, SCHEMA_VERSION

DEFAULT_ACTOR = "local-user"


@dataclass
class ServiceContext:
    routing: ProjectRouting
    conn: sqlite3.Connection
    actor_id: str
    epoch: str
    policy: dict[str, Any]
    policy_revision: str
    features: dict[str, Any]
    read_only: bool = False
    session_id: str | None = None

    @property
    def project_id(self) -> str:
        return self.routing.project_id

    @property
    def state_dir(self) -> Path:
        return self.routing.state_dir

    def require_actor(self) -> sqlite3.Row:
        return ensure_actor(self.conn, self.actor_id)

    def capabilities(self) -> set[str]:
        try:
            actor = get_actor(self.conn, self.actor_id)
        except Exception:
            return set()
        return capabilities_for(actor)

    def latest_seq(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM commit_events WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()
        return int(row["seq"])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ServiceContext":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def capabilities_report(self) -> dict[str, Any]:
        enabled = {
            "embeddings": bool(self.policy.get("retrieval", {}).get("embeddings", False)),
            "execution": bool(self.policy.get("execution", {}).get("enabled", False)),
            "mcp_transport": True,
        }
        index_health = None
        try:
            from research_kb.storage.search import check_integrity

            index_health = check_integrity(self.conn)
        except sqlite3.Error:
            index_health = {"healthy": False, "detail": "search index unavailable"}
        return {
            "runtime_version": RUNTIME_VERSION,
            "api_version": API_VERSION,
            "schema_version": SCHEMA_VERSION,
            "controller_epoch": self.epoch,
            "project_id": self.project_id,
            "actor_id": self.actor_id,
            "capabilities": sorted(self.capabilities()),
            "policy_revision": self.policy_revision,
            "enabled_modules": enabled,
            "sqlite": self.features,
            "index_health": index_health,
            "read_only": self.read_only,
        }


def _ensure_epoch(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM controller_meta WHERE key = 'controller_epoch'").fetchone()
    if row is not None:
        return row["value"]
    epoch = new_id()
    conn.execute(
        "INSERT INTO controller_meta (key, value) VALUES ('controller_epoch', ?)",
        (epoch,),
    )
    return epoch


def initialize_project(
    repo_root: str | Path,
    name: str,
    *,
    namespace: str | None = None,
    state_root: str | None = None,
    actor_id: str | None = None,
    actor_kind: str = "human",
) -> ProjectRouting:
    root = Path(repo_root).resolve()
    project_id = new_id()
    kwargs: dict[str, Any] = {}
    if state_root:
        kwargs["state_root"] = state_root
    config_path = write_routing(root, project_id, name, namespace=namespace or "", **kwargs)
    routing = _load_routing_from_path(config_path)
    ensure_local_layout(routing, create=True)
    conn = connect(routing.db_path)
    try:
        migrations.apply_all(conn)
        with write_tx(conn):
            epoch = _ensure_epoch(conn)
            conn.execute(
                "INSERT INTO controller_meta (key, value) VALUES ('controller_id', ?)",
                (new_id(),),
            )
            conn.execute(
                "INSERT INTO controller_meta (key, value) VALUES ('created_at', ?)",
                (utc_now(),),
            )
            resolved_actor = actor_id or os.environ.get(ENV_ACTOR) or DEFAULT_ACTOR
            ensure_actor(
                conn,
                resolved_actor,
                kind=actor_kind,
                display_name=resolved_actor,
                roles=["administrator", "reviewer", "operator"],
            )
            conn.execute(
                "INSERT INTO projects (project_id, name, namespace, created_seq, created_at) VALUES (?, ?, ?, 0, ?)",
                (project_id, name, namespace or project_id[:8], utc_now()),
            )
            seq = append_commit_event(
                conn,
                project_id=project_id,
                actor_id=resolved_actor,
                action="project_bootstrap",
                epoch=epoch,
                reason="Project namespace initialization.",
                changed=[{"project_id": project_id, "name": name}],
            )
            conn.execute(
                "UPDATE projects SET created_seq = ? WHERE project_id = ?",
                (seq, project_id),
            )
            from research_kb.service.objects import create_bootstrap_project_object

            object_id = create_bootstrap_project_object(
                conn,
                project_id=project_id,
                actor_id=resolved_actor,
                epoch=epoch,
                name=name,
                commit_seq=seq,
            )
            conn.execute(
                "UPDATE projects SET project_object_id = ? WHERE project_id = ?",
                (object_id, project_id),
            )
            policy = default_policy()
            revision = policy_hash(policy)
            save_policy(routing.state_dir, policy)
            conn.execute(
                """
                INSERT INTO policy_revisions
                  (policy_revision, project_id, profile_json, accepted_seq, accepted_by, accepted_at, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision,
                    project_id,
                    __import__("json").dumps(policy, sort_keys=True),
                    seq,
                    resolved_actor,
                    utc_now(),
                    "Bootstrap conservative default profile.",
                ),
            )
    finally:
        conn.close()
    return routing


def _load_routing_from_path(path: Path) -> ProjectRouting:
    from research_kb.config import load_routing

    routing = load_routing(path)
    assert routing is not None
    return routing


def open_service(
    routing: ProjectRouting | None = None,
    *,
    project_root: str | Path | None = None,
    actor_id: str | None = None,
    read_only: bool = False,
    session_id: str | None = None,
    allow_pending_migrations: bool = False,
) -> ServiceContext:
    from research_kb.config import load_routing

    resolved = routing
    if resolved is None:
        resolved = load_routing(project_root)
    assert resolved is not None
    if not resolved.db_path.exists():
        raise storage_failure(
            f"Project state database is missing: {resolved.db_path}",
            hint=(
                "A copied repository must not silently create a second writable canonical store. "
                "Restore the registered state root or explicitly initialize a new store."
            ),
        )
    conn = connect(resolved.db_path, read_only=read_only)
    try:
        migrations.ensure_registry(conn)
        migrations.verify_checksums(conn)
        pending = migrations.pending(conn)
        if pending and not read_only:
            if not allow_pending_migrations:
                from research_kb.errors import unsupported_version

                raise unsupported_version(
                    "Pending schema migrations require an explicit migration operation.",
                    pending=[migration.version for migration in pending],
                )
            migrations.apply_all(conn)
        features = probe_features(conn)
        epoch = _ensure_epoch(conn) if not read_only else _read_epoch(conn)
        policy = load_policy(resolved.state_dir)
        resolved_actor = actor_id or os.environ.get(ENV_ACTOR) or DEFAULT_ACTOR
        return ServiceContext(
            routing=resolved,
            conn=conn,
            actor_id=resolved_actor,
            epoch=epoch,
            policy=policy,
            policy_revision=policy_hash(policy),
            features=features,
            read_only=read_only,
            session_id=session_id,
        )
    except BaseException:
        conn.close()
        raise


def _read_epoch(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM controller_meta WHERE key = 'controller_epoch'").fetchone()
    return row["value"] if row else "unknown"
