"""Strict TOML configuration for fleetqd (§6.1: unknown keys are errors).

Nodes and cluster sites are declared here, and each is **disabled by default**
(§10). Enabling one is an explicit, evidenced act: its facts, resource
controls, recovery protocol and remote-call policy must be approved first.
"""

from __future__ import annotations

import tomllib
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import FqError


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _reject_unknown(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise FqError("invalid_argument", f"unknown keys in {where}: {sorted(unknown)}")


def _ui_origin(value: Any, *, dev_mode: bool) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FqError("invalid_argument", "[daemon] ui_origin must be a URL string")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise FqError("invalid_argument", "[daemon] ui_origin must be one canonical HTTPS origin") from exc
    loopback_dev = dev_mode and parsed.scheme == "http" and host in ("127.0.0.1", "localhost", "::1")
    if (not (parsed.scheme == "https" or loopback_dev) or not host or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.netloc):
        raise FqError("invalid_argument", "[daemon] ui_origin must be one canonical HTTPS origin")
    host = host.lower()
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FqError("invalid_argument", "[daemon] ui_origin host must use canonical ASCII or IDNA form") from exc
    netloc = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        netloc += f":{port}"
    return f"{parsed.scheme}://{netloc}"


@dataclass
class NodeConfig:
    id: str
    backend: str
    enabled: bool = False
    mode: str | None = None
    fleetctl_target: str | None = None
    control_root: str | None = None
    node_python: str = "python3"
    capacity: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    in_place_roots: list[str] = field(default_factory=list)
    reserve_gpus: list[str] = field(default_factory=list)
    gpu_profile: dict[str, Any] = field(default_factory=dict)
    login_shell: bool = False
    site: dict[str, Any] = field(default_factory=dict)
    caps: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    observe_interval_s: float | None = None   # None: the backend's default cadence
    evidence_file: str | None = None

    def config_json(self) -> dict[str, Any]:
        return {
            "fleetctl_target": self.fleetctl_target or self.id,
            "control_root": self.control_root,
            "node_python": self.node_python,
            "capacity": self.capacity,
            "defaults": self.defaults,
            "in_place_roots": self.in_place_roots,
            "reserve_gpus": self.reserve_gpus,
            "gpu_profile": self.gpu_profile,
            "login_shell": self.login_shell,
            "site": self.site,
            "caps": self.caps,
            "budget": self.budget,
            "observe_interval_s": self.observe_interval_s,
        }


@dataclass
class DaemonConfig:
    state_dir: Path
    bind: str = "127.0.0.1:8089"
    fleetctl: Path = Path("~/.local/bin/fleetctl").expanduser()
    fleet_config_home: Path | None = None
    volume_id: str | None = None
    state_fs_uuid: str | None = None
    sqlite_backport_attestation: str | None = None
    dev_mode: bool = False
    clock_provider: str | None = None
    notify_url: str | None = None
    fleetmon_feed_url: str | None = None
    ui_origin: str | None = None
    fleetctl_concurrency: int = 3        # each is a Python process (~25 MB); the unit's MemoryMax counts them
    nodes: list[NodeConfig] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    controller: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "fleetq.db"

    @property
    def bundle_dir(self) -> Path:
        return self.state_dir / "bundles"


_DAEMON_KEYS = {"state_dir", "bind", "fleetctl", "fleet_config_home", "volume_id",
                "state_fs_uuid", "sqlite_backport_attestation", "dev_mode", "notify_url", "fleetmon_feed_url",
                "fleetctl_concurrency", "ui_origin", "clock_provider"}
_NODE_KEYS = {"id", "backend", "enabled", "mode", "fleetctl_target", "control_root", "capacity", "defaults",
              "node_python", "in_place_roots", "reserve_gpus", "gpu_profile", "login_shell", "site", "caps", "budget",
              "observe_interval_s", "evidence_file"}


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _evidence_path(value: str, *, target: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise FqError("invalid_argument", f"enabled target {target!r}: evidence_file must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise FqError("invalid_argument", f"enabled target {target!r}: evidence path is unavailable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise FqError("invalid_argument", f"enabled target {target!r}: evidence_file must be an owned, non-symlink regular file")
    if info.st_size > 64 * 1024:
        raise FqError("invalid_argument", f"enabled target {target!r}: evidence_file is too large")
    return path


def validate_target_evidence(node: NodeConfig) -> None:
    """Require a reviewed capture bound to the exact enabled target profile (§10).

    The evidence remains an operator assertion, not a substitute for the
    required local integration and production canaries. The capture digest is
    checked against the retained raw output so an approval cannot silently
    outlive a changed capture or target configuration.
    """
    if not node.evidence_file:
        raise FqError("invalid_argument", f"enabled target {node.id!r} needs evidence_file")
    if node.backend == "slurm":
        budget = node.budget or {}
        for field_name in ("monitor_per_minute", "action_per_minute", "transfer_per_minute", "burst",
                           "sessions_per_minute", "bytes_per_minute"):
            try:
                if isinstance(budget[field_name], bool):
                    raise ValueError("boolean is not a rate")
                value = float(budget[field_name])
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                raise FqError("invalid_argument", f"enabled cluster {node.id!r}: invalid {field_name} budget") from exc
            if not math.isfinite(value) or value <= 0:
                raise FqError("invalid_argument", f"enabled cluster {node.id!r}: {field_name} must be positive")
        for field_name in ("max_bytes_per_transfer", "max_sessions_per_operation"):
            value = budget.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise FqError("invalid_argument", f"enabled cluster {node.id!r}: {field_name} must be positive")
        sessions_burst = budget.get("sessions_burst", max(1.0, float(budget["sessions_per_minute"])))
        bytes_burst = budget.get("bytes_burst", max(1.0, float(budget["bytes_per_minute"])))
        try:
            if isinstance(sessions_burst, bool) or isinstance(bytes_burst, bool):
                raise ValueError("boolean is not a burst")
            sessions_burst = float(sessions_burst)
            bytes_burst = float(bytes_burst)
        except (TypeError, ValueError, OverflowError) as exc:
            raise FqError("invalid_argument", f"enabled cluster {node.id!r}: invalid dimension burst") from exc
        if (not math.isfinite(sessions_burst) or not math.isfinite(bytes_burst)
                or sessions_burst < budget["max_sessions_per_operation"]
                or bytes_burst < budget["max_bytes_per_transfer"]):
            raise FqError("invalid_argument", f"enabled cluster {node.id!r}: session/byte bursts must cover one bounded transfer")
        if float(budget["burst"]) < 8:
            raise FqError("invalid_argument", f"enabled cluster {node.id!r}: operation burst must cover the conservative 8-RPC envelope")
        action_reserve = budget.get("action_session_reserve")
        if (isinstance(action_reserve, bool) or not isinstance(action_reserve, int)
                or action_reserve < budget["max_sessions_per_operation"]
                or action_reserve > sessions_burst):
            raise FqError("invalid_argument", f"enabled cluster {node.id!r}: action_session_reserve must cover one operation and fit within sessions_burst")
    path = _evidence_path(node.evidence_file, target=node.id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FqError("invalid_argument", f"enabled target {node.id!r}: unreadable evidence: {exc}") from exc
    if not isinstance(record, dict) or record.get("schema") != "fq.target-evidence/v1":
        raise FqError("invalid_argument", f"enabled target {node.id!r}: invalid evidence schema")
    if record.get("target") != node.id or record.get("backend") != node.backend or record.get("mode") != node.mode:
        raise FqError("invalid_argument", f"enabled target {node.id!r}: evidence names another target/profile")
    config_digest = hashlib.sha256(json.dumps(node.config_json(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if record.get("config_sha256") != config_digest:
        raise FqError("invalid_argument", f"enabled target {node.id!r}: evidence predates this target configuration")
    if record.get("approved") is not True or not isinstance(record.get("reviewer"), str) or not record["reviewer"].strip():
        raise FqError("invalid_argument", f"enabled target {node.id!r}: evidence lacks reviewer approval")
    if not isinstance(record.get("command_profile_version"), str) or not record["command_profile_version"].strip():
        raise FqError("invalid_argument", f"enabled target {node.id!r}: command/profile version is missing")
    timestamps = {}
    for field_name in ("captured_at", "approved_at"):
        try:
            when = datetime.fromisoformat(str(record[field_name]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise FqError("invalid_argument", f"enabled target {node.id!r}: invalid {field_name}") from exc
        if when.tzinfo is None or when > datetime.now(timezone.utc):
            raise FqError("invalid_argument", f"enabled target {node.id!r}: {field_name} needs a past UTC time")
        timestamps[field_name] = when
    if timestamps["approved_at"] < timestamps["captured_at"]:
        raise FqError("invalid_argument", f"enabled target {node.id!r}: approval predates capture")
    capture_name = record.get("capture_file")
    if not isinstance(capture_name, str) or Path(capture_name).name != capture_name:
        raise FqError("invalid_argument", f"enabled target {node.id!r}: capture_file must be beside evidence")
    capture = _evidence_path(str(path.parent / capture_name), target=node.id)
    if capture.stat().st_size > 4 * 1024 * 1024:
        raise FqError("invalid_argument", f"enabled target {node.id!r}: capture file is too large")
    expected = record.get("capture_sha256")
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected) or hashlib.sha256(capture.read_bytes()).hexdigest() != expected:
        raise FqError("invalid_argument", f"enabled target {node.id!r}: capture digest does not match")
    checks = record.get("checks")
    required = {"owner_approval", "control_root", "cancellation", "result_durability"}
    if node.backend == "bare":
        required |= {"uuid_inventory", "process_visibility", "linger", "cgroup_memory", "systemd_user"}
        if node.mode == "shared":
            required |= {"shared_map", "gpu_baselines", "post_launch_contention"}
    else:
        required |= {"automation_policy", "control_budget", "controller_identity", "account_partition_qos",
                     "accounting_visibility", "no_requeue", "query_cancel"}
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required):
        missing = sorted(name for name in required if not isinstance(checks, dict) or checks.get(name) is not True)
        raise FqError("invalid_argument", f"enabled target {node.id!r}: evidence lacks approved checks: {missing}")


def load_config(path: Path) -> DaemonConfig:
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    _reject_unknown(raw, {"daemon", "node", "limits", "controller"}, str(path))
    _reject_unknown(raw.get("limits") or {}, {"bundle"}, "[limits]")
    from .engine.controller import ControllerConfig
    _reject_unknown(raw.get("controller") or {}, set(ControllerConfig.__dataclass_fields__), "[controller]")
    daemon = raw.get("daemon") or {}
    _reject_unknown(daemon, _DAEMON_KEYS, "[daemon]")
    if "dev_mode" in daemon and not isinstance(daemon["dev_mode"], bool):
        raise FqError("invalid_argument", "[daemon] dev_mode must be a boolean")
    supported_clock_providers = {"chrony", "systemd-timesyncd"}
    if "clock_provider" in daemon and (
        not isinstance(daemon["clock_provider"], str)
        or daemon["clock_provider"] not in supported_clock_providers
    ):
        choices = " or ".join(f"'{provider}'" for provider in sorted(supported_clock_providers))
        raise FqError("invalid_argument", f"[daemon] clock_provider must be {choices}")
    if "state_dir" not in daemon:
        raise FqError("invalid_argument", "[daemon] state_dir is required")
    nodes = []
    seen = set()
    seen_routes: dict[str, str] = {}
    for item in raw.get("node") or []:
        _reject_unknown(item, _NODE_KEYS, f"[[node]] {item.get('id')}")
        if not isinstance(item.get("id"), str) or not item["id"] or item["id"] != item["id"].strip():
            raise FqError("invalid_argument", "node id must be a non-empty string without surrounding whitespace")
        if item.get("backend") not in ("bare", "slurm"):
            raise FqError("invalid_argument", f"node {item.get('id')!r}: backend must be 'bare' or 'slurm'")
        if item["id"] in seen:
            raise FqError("invalid_argument", f"node {item['id']!r} declared twice")
        seen.add(item["id"])
        for flag in ("enabled", "login_shell"):
            if flag in item and not isinstance(item[flag], bool):
                raise FqError("invalid_argument", f"node {item['id']!r}: {flag} must be a boolean")
        route = item.get("fleetctl_target", item["id"])
        if not isinstance(route, str) or not route or route != route.strip():
            raise FqError("invalid_argument", f"node {item['id']!r}: fleetctl_target must be a non-empty string")
        if item.get("enabled", False):
            if route in seen_routes:
                raise FqError("invalid_argument", f"enabled nodes {seen_routes[route]!r} and {item['id']!r} share fleetctl target {route!r}")
            seen_routes[route] = item["id"]
        if item.get("backend") == "bare" and item.get("mode") not in ("exclusive", "shared"):
            raise FqError("invalid_argument", f"node {item['id']!r}: mode must be exclusive or shared")
        node_python = item.get("node_python", "python3")
        if (not isinstance(node_python, str) or not node_python or "\x00" in node_python
                or "\n" in node_python or "\r" in node_python
                or (node_python != "python3" and not Path(node_python).is_absolute())):
            raise FqError("invalid_argument", f"node {item['id']!r}: node_python must be 'python3' or an absolute executable path")
        nodes.append(NodeConfig(**item))
    cfg = DaemonConfig(
        state_dir=_expand(daemon["state_dir"]),
        bind=daemon.get("bind", "127.0.0.1:8089"),
        fleetctl=_expand(daemon.get("fleetctl", "~/.local/bin/fleetctl")),
        fleet_config_home=_expand(daemon["fleet_config_home"]) if daemon.get("fleet_config_home") else None,
        volume_id=daemon.get("volume_id"),
        state_fs_uuid=daemon.get("state_fs_uuid"),
        sqlite_backport_attestation=daemon.get("sqlite_backport_attestation") or None,
        dev_mode=daemon.get("dev_mode", False),
        clock_provider=daemon.get("clock_provider"),
        notify_url=daemon.get("notify_url"),
        fleetmon_feed_url=daemon.get("fleetmon_feed_url"),
        ui_origin=_ui_origin(daemon.get("ui_origin"), dev_mode=daemon.get("dev_mode", False)),
        fleetctl_concurrency=int(daemon.get("fleetctl_concurrency", 3)),
        nodes=nodes,
        limits=raw.get("limits") or {},
        controller=raw.get("controller") or {},
    )
    # Validate controller limits at config load, before a restored/production
    # service reaches the artifact collector.
    try:
        ControllerConfig(**{k: v for k, v in cfg.controller.items()
                            if k in ControllerConfig.__dataclass_fields__})
    except (TypeError, ValueError) as exc:
        raise FqError("invalid_argument", f"[controller] {exc}") from exc
    # Slurm's result collection reserves its complete bounded pull before
    # touching the site. A smaller per-transfer ceiling would leave finished
    # jobs stuck in artifact collection forever, so reject that policy early.
    for node in cfg.nodes:
        if not node.enabled or node.backend != "slurm":
            continue
        try:
            collect_bytes = cfg.controller.get("collect_max_bytes", 2 * 1024 ** 3)
            collect_files = cfg.controller.get("collect_max_files", 10_000)
            if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                   for value in (collect_bytes, collect_files)):
                raise ValueError("collection limits must be positive integers")
            pull_ceiling = collect_bytes + min(64 * 1024 ** 2, collect_files * 4096)
            transfer_cap = node.budget.get("max_bytes_per_transfer")
            byte_burst = node.budget.get("bytes_burst", node.budget.get("bytes_per_minute"))
            if (isinstance(transfer_cap, bool) or not isinstance(transfer_cap, int)
                    or isinstance(byte_burst, bool) or not isinstance(byte_burst, (int, float))
                    or transfer_cap < pull_ceiling or byte_burst < pull_ceiling):
                raise ValueError("transfer cap and byte burst must cover bounded result pull")
        except (TypeError, ValueError, OverflowError) as exc:
            raise FqError("invalid_argument", f"enabled cluster {node.id!r}: {exc}") from exc
    if not cfg.dev_mode:
        for node in cfg.nodes:
            if node.enabled:
                validate_target_evidence(node)
    return cfg


def sync_nodes(conn, cfg: DaemonConfig) -> None:
    """Make the nodes table reflect config. Runtime state (drains, fences) survives."""
    import json
    from .util import utcnow

    declared = {n.id for n in cfg.nodes}
    for node in cfg.nodes:
        if node.enabled and not cfg.dev_mode:
            validate_target_evidence(node)
        conn.execute(
            "INSERT INTO nodes (id, backend, mode, enabled, config_json, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET backend=excluded.backend, mode=excluded.mode,"
            " enabled=excluded.enabled, config_json=excluded.config_json, updated_at=excluded.updated_at",
            (node.id, node.backend, node.mode, int(node.enabled), json.dumps(node.config_json(), sort_keys=True),
             utcnow()),
        )
    for row in conn.execute("SELECT id FROM nodes").fetchall():
        if row["id"] not in declared:
            conn.execute("UPDATE nodes SET enabled = 0, updated_at = ? WHERE id = ?", (utcnow(), row["id"]))
