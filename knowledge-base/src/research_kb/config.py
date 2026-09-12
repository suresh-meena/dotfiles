from __future__ import annotations

import os
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_kb.domain.canonical import canonical_hash
from research_kb.errors import schema_validation_failed, storage_failure

ROUTING_FILE = Path(".research") / "project.toml"
DEFAULT_STATE_ROOT = Path(".research") / "state"
ENV_STATE_ROOT = "RESEARCH_KB_STATE_ROOT"
ENV_UNPATCHED_SQLITE = "RESEARCH_KB_ALLOW_UNPATCHED_SQLITE"
ENV_ACTOR = "RESEARCH_KB_ACTOR"

_ALLOWED_TOP_LEVEL = {"project", "state", "controller", "display"}
_ALLOWED_SECTIONS = {
    "project": {"id", "name", "namespace"},
    "state": {"root", "explicit_external"},
    "controller": {"transport", "api_range", "endpoint"},
    "display": {"timezone", "naming"},
}


def find_project_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ROUTING_FILE).is_file():
            return candidate
    return None


@dataclass
class ProjectRouting:
    repo_root: Path
    config_path: Path
    project_id: str
    name: str
    state_root: str
    explicit_external: bool = False
    transport: str = "cli"
    api_range: str = ">=1.0,<2.0"
    endpoint: str = ""
    timezone: str = "UTC"
    namespace: str = ""

    @property
    def resolved_state_root(self) -> Path:
        override = os.environ.get(ENV_STATE_ROOT)
        if override:
            return Path(override).expanduser().resolve()
        root = Path(self.state_root)
        if root.is_absolute():
            return root.resolve()
        return (self.repo_root / root).resolve()

    @property
    def state_dir(self) -> Path:
        return self.resolved_state_root / self.project_id

    @property
    def db_path(self) -> Path:
        return self.state_dir / "research.db"

    @property
    def sources_dir(self) -> Path:
        return self.state_dir / "sources" / "sha256"

    @property
    def extractions_dir(self) -> Path:
        return self.state_dir / "extractions" / "sha256"

    @property
    def spool_dir(self) -> Path:
        return self.state_dir / "spool"

    @property
    def exports_dir(self) -> Path:
        return self.state_dir / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "repo_root": str(self.repo_root),
            "state_root": str(self.resolved_state_root),
            "state_dir": str(self.state_dir),
            "transport": self.transport,
            "api_range": self.api_range,
            "explicit_external": self.explicit_external,
            "timezone": self.timezone,
        }


def load_routing(path: str | Path | None = None, *, allow_missing: bool = False) -> ProjectRouting | None:
    if path is None:
        root = find_project_root()
        if root is None:
            if allow_missing:
                return None
            from research_kb.errors import project_required

            raise project_required(
                "No .research/project.toml found from the current directory upward. "
                "Run 'rkb init' in the project directory to create local project routing."
            )
        config_path = root / ROUTING_FILE
    else:
        candidate = Path(path)
        if candidate.is_dir():
            config_path = candidate / ROUTING_FILE
            root = candidate.resolve()
        else:
            config_path = candidate
            root = candidate.parent.parent.resolve()
        if not config_path.is_file():
            if allow_missing:
                return None
            raise storage_failure(f"Project routing file not found: {config_path}")
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise schema_validation_failed(f"Could not parse project routing: {exc}") from exc
    unknown = set(data) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise schema_validation_failed(
            "Unknown routing configuration keys.",
            unknown=sorted(unknown),
            allowed=sorted(_ALLOWED_TOP_LEVEL),
        )
    for section, keys in data.items():
        if not isinstance(keys, dict):
            raise schema_validation_failed(f"Routing section '{section}' must be a table.")
        extra = set(keys) - _ALLOWED_SECTIONS.get(section, set())
        if extra:
            raise schema_validation_failed(
                f"Unknown keys in routing section '{section}'.",
                unknown=sorted(extra),
            )
    project = data.get("project", {})
    state = data.get("state", {})
    controller = data.get("controller", {})
    display = data.get("display", {})
    project_id = project.get("id")
    if not project_id:
        raise schema_validation_failed("Routing file must declare project.id.")
    try:
        uuid.UUID(str(project_id))
    except ValueError as exc:
        raise schema_validation_failed("project.id must be a UUID.") from exc
    return ProjectRouting(
        repo_root=root,
        config_path=config_path,
        project_id=str(project_id),
        name=str(project.get("name", "")),
        state_root=str(state.get("root", str(DEFAULT_STATE_ROOT))),
        explicit_external=bool(state.get("explicit_external", False)),
        transport=str(controller.get("transport", "cli")),
        api_range=str(controller.get("api_range", ">=1.0,<2.0")),
        endpoint=str(controller.get("endpoint", "")),
        timezone=str(display.get("timezone", "UTC")),
        namespace=str(project.get("namespace", "")),
    )


def render_routing(project_id: str, name: str, *, state_root: str = str(DEFAULT_STATE_ROOT), namespace: str = "") -> str:
    safe_name = name.replace('"', "'")
    return (
        "# Research KB project routing. Committed to the repository; no secrets here.\n"
        "[project]\n"
        f'id = "{project_id}"\n'
        f'name = "{safe_name}"\n'
        + (f'namespace = "{namespace}"\n' if namespace else "")
        + "\n[state]\n"
        f'root = "{state_root}"\n'
        "explicit_external = false\n"
        "\n[controller]\n"
        'transport = "cli"\n'
        'api_range = ">=1.0,<2.0"\n'
        "\n[display]\n"
        'timezone = "UTC"\n'
    )


def write_routing(
    repo_root: Path,
    project_id: str,
    name: str,
    *,
    state_root: str = str(DEFAULT_STATE_ROOT),
    namespace: str = "",
) -> Path:
    config_path = repo_root / ROUTING_FILE
    if config_path.exists():
        raise storage_failure(
            f"Routing already exists at {config_path}.",
            hint="A copied repository must not silently create a second writable canonical store.",
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        render_routing(project_id, name, state_root=state_root, namespace=namespace),
        encoding="utf-8",
    )
    return config_path


def ensure_local_layout(routing: ProjectRouting, *, create: bool = True) -> None:
    state_dir = routing.state_dir
    if not state_dir.exists() and not create:
        raise storage_failure(
            f"Project state directory is missing: {state_dir}",
            hint="Restore the registered state root or explicitly initialize a new store.",
        )
    if create:
        for path in (
            state_dir,
            routing.sources_dir,
            routing.extractions_dir,
            routing.spool_dir,
            routing.exports_dir,
            routing.backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


DEFAULT_POLICY: dict[str, Any] = {
    "policy_name": "conservative",
    "capture": {
        "allowed_import_roots": [],
        "auto_capture_kinds": [
            "idea",
            "hypothesis",
            "observation",
            "interpretation",
            "conclusion",
            "negative_result",
            "definition",
            "assumption",
            "derivation",
            "method",
            "decision",
            "question",
            "caveat",
            "issue",
            "claim",
        ],
        "default_record_state": "draft",
        "allow_low_risk_auto_apply": True,
        "import_scope": "explicit_only",
    },
    "retrieval": {
        "exact": True,
        "fts": True,
        "embeddings": False,
        "external_embeddings_allowed": False,
        "candidate_window": 50,
        "rrf_k": 60,
        "orientation_budget_tokens": 3000,
        "focused_budget_tokens": 8000,
    },
    "review": {
        "auto_apply_low_risk": True,
        "require_approval_for_default": list(
            sorted(
                {
                    "assess_evidence",
                    "resolve_critical",
                    "supersede",
                    "tombstone",
                    "execute_launch",
                    "execute_cancel",
                    "policy_change",
                    "approve_selection",
                }
            )
        ),
    },
    "provenance": {
        "default_profile": "numerical",
        "exploratory_gaps_allowed": True,
        "require_clean_snapshot_for_paper_critical": True,
    },
    "execution": {
        "enabled": False,
        "adapters": [],
        "allow_unrestricted_shell": False,
        "max_walltime_seconds": 86400,
    },
    "preservation": {
        "backup_frequency": "daily",
        "source_capture": "explicit_only",
        "retain_release_snapshots": True,
        "allow_external_embedding": False,
    },
    "limits": {
        "max_import_bytes": 100_000_000,
        "max_context_records": 200,
        "max_traversal_nodes": 500,
        "max_page_size": 200,
        "idempotency_retention_days": 365,
    },
}


ALLOWED_POLICY_KEYS: dict[str, set[str] | None] = {
    "policy_name": None,
    "capture": {
        "auto_capture_kinds",
        "default_record_state",
        "allow_low_risk_auto_apply",
        "import_scope",
        "allowed_import_roots",
    },
    "retrieval": {
        "exact",
        "fts",
        "embeddings",
        "embedding_provider",
        "external_embeddings_allowed",
        "candidate_window",
        "rrf_k",
        "orientation_budget_tokens",
        "focused_budget_tokens",
    },
    "review": {"auto_apply_low_risk", "require_approval_for_default"},
    "provenance": {
        "default_profile",
        "exploratory_gaps_allowed",
        "require_clean_snapshot_for_paper_critical",
    },
    "execution": {
        "enabled",
        "adapters",
        "allow_unrestricted_shell",
        "max_walltime_seconds",
    },
    "preservation": {
        "backup_frequency",
        "source_capture",
        "retain_release_snapshots",
        "allow_external_embedding",
    },
    "limits": {
        "max_import_bytes",
        "max_context_records",
        "max_traversal_nodes",
        "max_page_size",
        "idempotency_retention_days",
    },
}


def validate_policy(policy: dict[str, Any]) -> None:
    unknown_sections = set(policy) - set(ALLOWED_POLICY_KEYS)
    if unknown_sections:
        raise schema_validation_failed(
            "Unknown policy sections.", unknown=sorted(unknown_sections)
        )
    for section, allowed in ALLOWED_POLICY_KEYS.items():
        if section not in policy or allowed is None:
            continue
        value = policy[section]
        if not isinstance(value, dict):
            raise schema_validation_failed(f"Policy section '{section}' must be a table.")
        unknown = set(value) - allowed
        if unknown:
            raise schema_validation_failed(
                f"Unknown keys in policy section '{section}'.",
                unknown=sorted(unknown),
                allowed=sorted(allowed),
            )


def policy_hash(policy: dict[str, Any]) -> str:
    return canonical_hash(policy)


def default_policy() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DEFAULT_POLICY)


def load_policy(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "policy.json"
    if not path.is_file():
        return default_policy()
    import json

    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise schema_validation_failed(f"Could not parse policy profile: {exc}") from exc
    if not isinstance(policy, dict):
        raise schema_validation_failed("Policy profile must be an object.")
    validate_policy(policy)
    return policy


def save_policy(state_dir: Path, policy: dict[str, Any]) -> str:
    import json

    validate_policy(policy)
    path = state_dir / "policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return policy_hash(policy)
