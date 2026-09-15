"""Site configuration loader for config/kiac.yaml.

The KIAC manual contradicts itself in places, so the config keeps every
documented candidate value and marks disputed entries instead of silently
picking a winner. Live scheduler data, when available, is always more
authoritative than this file.

YAML is parsed with PyYAML when installed; otherwise a stdlib subset loader
handles the shipped file (nested maps, block/flow lists, quoted scalars).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .parser import expand_hostlist, parse_time


class ConfigError(RuntimeError):
    pass


def repo_root() -> Path:
    """Skill root (contains config/, templates/, bin/, tests/)."""
    return Path(__file__).resolve().parents[2]


def default_config_path(site_name=None) -> Optional[Path]:
    env = os.environ.get("KIAC_SLURM_CONFIG")
    if env:
        return Path(env)
    site = site_name or os.environ.get("KIAC_SLURM_SITE") or "kiac"
    candidate = repo_root() / "config" / f"{site}.yaml"
    return candidate if candidate.exists() else None


@dataclass
class PartitionDoc:
    name: str
    nodes_raw: List[str] = field(default_factory=list)
    nodes: List[str] = field(default_factory=list)
    max_times_raw: List[str] = field(default_factory=list)
    max_times: List[Optional[int]] = field(default_factory=list)
    status: str = "documented"
    account_required: bool = False
    interactive_qos: Optional[str] = None
    batch_qos: Optional[str] = None


@dataclass
class SiteConfig:
    raw: Dict[str, Any]
    cluster_name: str = "kiac"
    rule_prefix: str = "KIAC"
    partitions: Dict[str, PartitionDoc] = field(default_factory=dict)
    preferred_storage: str = "/storage"
    assume_storage_writable: bool = False
    preferred_storage_caveat: Optional[str] = None
    home_path_prefix: Optional[str] = None
    home_quota_note: Optional[str] = None
    gpu_jobs_only_partitions: List[str] = field(default_factory=list)
    gpu_vendor: str = "nvidia"
    gpu_catalog: List[str] = field(default_factory=list)
    discover_gres_types_live: bool = True
    node_partition_map: Dict[str, str] = field(default_factory=dict)
    verified: Dict[str, Any] = field(default_factory=dict)

    @property
    def partition_names(self) -> List[str]:
        return sorted(self.partitions)

    @property
    def verified_as_of(self) -> Optional[str]:
        value = self.verified.get("as_of")
        return str(value) if value else None

    def verified_partition(self, name: Optional[str]) -> Optional[Dict[str, Any]]:
        """Live-verified facts for a partition (gres types, account policy), or None."""
        if not name:
            return None
        entry = (self.verified.get("partitions") or {}).get(name)
        return entry if isinstance(entry, dict) else None

    def verified_gres_types(self, name: Optional[str]) -> List[str]:
        entry = self.verified_partition(name) or {}
        return [str(g) for g in entry.get("gres_types") or []]

    def verified_max_time(self, name: Optional[str]):
        """Learned MaxTime (seconds) for a partition, recorded by `learn apply`."""
        if not name:
            return None
        raw = (self.verified.get("max_times") or {}).get(name)
        return parse_time(raw) if raw else None


def load_site_config(path=None, site=None) -> SiteConfig:
    if path is None:
        resolved = default_config_path(site)
        if resolved is None:
            wanted = site or os.environ.get("KIAC_SLURM_SITE") or "kiac"
            raise ConfigError(
                f"site config not found for '{wanted}'; pass --config PATH or set "
                f"KIAC_SLURM_CONFIG (expected {repo_root() / 'config' / (wanted + '.yaml')})"
            )
    else:
        resolved = Path(path)
        if not resolved.exists():
            raise ConfigError(f"site config not found: {resolved}")

    text = resolved.read_text()
    data = _load_yaml(text)

    if site and str(data.get("cluster") or "kiac") != site:
        # an explicit --site that disagrees with the loaded file (e.g. a stale
        # KIAC_SLURM_CONFIG) must be loud, never a silent site swap
        raise ConfigError(
            f"--site {site} requested but config {resolved} declares cluster "
            f"'{data.get('cluster')}'; unset KIAC_SLURM_CONFIG or pass the matching site"
        )

    partitions: Dict[str, PartitionDoc] = {}
    node_map: Dict[str, str] = {}
    for name, spec in (data.get("partitions") or {}).items():
        if not isinstance(spec, dict):
            continue
        nodes_raw = [str(n) for n in spec.get("documented_nodes") or []]
        times_raw = [str(t) for t in spec.get("documented_max_times") or []]
        doc = PartitionDoc(
            name=name,
            nodes_raw=nodes_raw,
            nodes=[n for raw_node in nodes_raw for n in expand_hostlist(raw_node)],
            max_times_raw=times_raw,
            max_times=[parse_time(t) for t in times_raw],
            status=str(spec.get("status") or "documented"),
            account_required=bool(spec.get("account_required", False)),
            interactive_qos=spec.get("interactive_qos"),
            batch_qos=spec.get("batch_qos"),
        )
        partitions[name] = doc
        for node in doc.nodes:
            node_map[node] = name

    policy = data.get("policy") or {}
    verified = data.get("verified_live") or {}
    return SiteConfig(
        raw=data,
        cluster_name=str(data.get("cluster") or "kiac"),
        rule_prefix=str(data.get("rule_prefix") or "KIAC"),
        partitions=partitions,
        preferred_storage=str(policy.get("preferred_storage") or "/storage"),
        assume_storage_writable=bool(policy.get("assume_storage_writable", False)),
        preferred_storage_caveat=policy.get("preferred_storage_caveat"),
        home_path_prefix=policy.get("home_path_prefix"),
        home_quota_note=policy.get("home_quota_note"),
        gpu_jobs_only_partitions=[str(p) for p in policy.get("gpu_jobs_only_partitions") or []],
        gpu_vendor=str(data.get("gpu_vendor") or "nvidia").lower(),
        gpu_catalog=[str(g) for g in data.get("gpu_catalog_documented") or []],
        discover_gres_types_live=bool(data.get("discover_gres_types_live", True)),
        node_partition_map=node_map,
        verified=verified if isinstance(verified, dict) else {},
    )


# ---------------------------------------------------------------------------
# Minimal YAML subset loader (fallback when PyYAML is absent)
# ---------------------------------------------------------------------------

def _load_yaml(text: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        return _mini_yaml(text)


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        else:
            if ch == "#":
                break
            if ch in "\"'":
                quote = ch
            out.append(ch)
    return "".join(out)


def _parse_scalar(token: str) -> Any:
    token = token.strip()
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_flow(inner)]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    lowered = token.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(token)
    except ValueError:
        return token


def _split_flow(inner: str) -> List[str]:
    parts, buf, quote, depth = [], [], None, 0
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _mini_yaml(text: str) -> Dict[str, Any]:
    entries = []
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        entries.append((indent, line.strip()))
    if not entries:
        return {}
    value, _ = _parse_node(entries, 0, entries[0][0])
    return value if isinstance(value, dict) else {}


def _parse_node(entries, i, indent):
    if entries[i][1].startswith("- "):
        items = []
        while i < len(entries) and entries[i][0] == indent and entries[i][1].startswith("- "):
            items.append(_parse_scalar(entries[i][1][2:]))
            i += 1
        return items, i

    mapping = {}
    while i < len(entries) and entries[i][0] == indent and not entries[i][1].startswith("- "):
        content = entries[i][1]
        key, sep, rest = content.partition(":")
        if not sep:
            i += 1
            continue
        key = _parse_scalar(key)
        rest = rest.strip()
        if rest:
            mapping[key] = _parse_scalar(rest)
            i += 1
        else:
            i += 1
            if i < len(entries) and entries[i][0] > indent:
                value, i = _parse_node(entries, i, entries[i][0])
                mapping[key] = value
            else:
                mapping[key] = None
    return mapping, i
