"""Live KIAC controller discovery (stages 7-8) and sbatch --test-only (stage 9).

Scheduler state is gathered in a small, bounded number of calls — one
discovery sweep (cached with a TTL), one accounting query, one test-only
run — because excess client RPC traffic degrades slurmctl for everyone.
Structured output (sinfo --json) is preferred when the installed Slurm
supports it, with scontrol's stable formatted text as the fallback.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .parser import expand_hostlist, parse_time

DEFAULT_TTL = 300.0
_FIELD_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


class Runner:
    """Subprocess facade that never raises; (returncode, combined output)."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def run(self, cmd: List[str]) -> Tuple[int, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError:
            return 127, f"command not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {self.timeout}s: {' '.join(cmd)}"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


@dataclass
class PartitionLive:
    name: str
    max_time: Optional[int] = None  # seconds; -1 = unlimited
    default_time: Optional[int] = None
    nodes: List[str] = field(default_factory=list)


@dataclass
class NodeLive:
    name: str
    state: str = "UNKNOWN"
    gres: List[Tuple[str, Optional[str], int]] = field(default_factory=list)  # (name, type, count)
    real_memory: Optional[int] = None
    partitions: List[str] = field(default_factory=list)

    def is_down(self) -> bool:
        base = self.state.split("+")[0].rstrip("*").lower()
        return base in {"down", "drain", "fail"}

    def gpu_entries(self) -> List[Tuple[Optional[str], int]]:
        return [(gtype, count) for (name, gtype, count) in self.gres if name == "gpu"]


@dataclass
class ClusterState:
    partitions: Dict[str, PartitionLive] = field(default_factory=dict)
    nodes: Dict[str, NodeLive] = field(default_factory=dict)
    source: str = "unknown"
    fetched_at: float = 0.0

    # -- queries ----------------------------------------------------------

    def partition_nodes(self, part: Optional[str]) -> List[str]:
        entry = self.partitions.get(part) if part else None
        return entry.nodes if entry else []

    def partition_gres_types(self, part: Optional[str]) -> set:
        """Typed gpu GRES strings configured on the partition's nodes."""
        types = set()
        for name in self.partition_nodes(part):
            node = self.nodes.get(name)
            if node is None:
                continue
            for gname, gtype, _count in node.gres:
                if gname == "gpu" and gtype:
                    types.add(gtype)
        return types

    def partition_has_gpu(self, part: Optional[str]) -> bool:
        for name in self.partition_nodes(part):
            node = self.nodes.get(name)
            if node is None:
                continue
            if any(gname == "gpu" for gname, _t, _c in node.gres):
                return True
        return False

    def partition_gpu_max(self, part: Optional[str], gtype: Optional[str] = None) -> Optional[int]:
        """Largest per-node GPU count of the given type on the partition."""
        best = None
        for name in self.partition_nodes(part):
            node = self.nodes.get(name)
            if node is None:
                continue
            total = 0
            found = False
            for gname, gtype_i, count in node.gres:
                if gname != "gpu":
                    continue
                if gtype is None or gtype_i == gtype:
                    total += count
                    found = True
            if found:
                best = total if best is None else max(best, total)
        return best

    def gpu_state_summary(self) -> List[str]:
        agg: Dict[str, int] = {}
        for node in self.nodes.values():
            for gname, gtype, count in node.gres:
                if gname != "gpu":
                    continue
                key = gtype or "gpu"
                agg[key] = agg.get(key, 0) + count
        return [f"{key}:{count}" for key, count in sorted(agg.items())]

    # -- cache serialization ----------------------------------------------

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "source": self.source,
            "partitions": {
                name: {
                    "max_time": p.max_time,
                    "default_time": p.default_time,
                    "nodes": p.nodes,
                }
                for name, p in self.partitions.items()
            },
            "nodes": {
                name: {
                    "state": n.state,
                    "gres": [[a, b, c] for a, b, c in n.gres],
                    "real_memory": n.real_memory,
                    "partitions": n.partitions,
                }
                for name, n in self.nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClusterState":
        state = cls(source=str(data.get("source") or "cache"))
        state.fetched_at = float(data.get("fetched_at") or 0)
        for name, p in (data.get("partitions") or {}).items():
            state.partitions[name] = PartitionLive(
                name=name,
                max_time=p.get("max_time"),
                default_time=p.get("default_time"),
                nodes=list(p.get("nodes") or []),
            )
        for name, n in (data.get("nodes") or {}).items():
            state.nodes[name] = NodeLive(
                name=name,
                state=str(n.get("state") or "UNKNOWN"),
                gres=[(g[0], g[1], int(g[2])) for g in (n.get("gres") or []) if len(g) == 3],
                real_memory=n.get("real_memory"),
                partitions=list(n.get("partitions") or []),
            )
        return state


def default_cache_dir(site_name=None) -> Path:
    env = os.environ.get("KIAC_SLURM_CACHE_DIR")
    if env:
        return Path(env)
    site = site_name or os.environ.get("KIAC_SLURM_SITE") or "kiac"
    return Path.home() / ".cache" / "kiac-slurm" / site


def slurm_available(runner: Optional[Runner] = None) -> bool:
    runner = runner or Runner()
    rc, _ = runner.run(["sinfo", "--version"])
    return rc == 0


def discover(
    runner: Optional[Runner] = None,
    cache_dir=None,
    ttl: float = DEFAULT_TTL,
    force: bool = False,
    site_name=None,
) -> Optional[ClusterState]:
    """One-shot cluster discovery, cached (per site) to avoid RPC spam."""
    runner = runner or Runner()
    cache_file = (Path(cache_dir) if cache_dir else default_cache_dir(site_name)) / "cluster.json"
    if not force:
        cached = _load_cache(cache_file, ttl)
        if cached is not None:
            return cached

    state: Optional[ClusterState] = None
    rc, out = runner.run(["sinfo", "--json"])
    if rc == 0 and out.strip().startswith("{"):
        try:
            partitions, nodes = parse_sinfo_json(out)
        except (ValueError, json.JSONDecodeError):
            partitions, nodes = {}, {}
        # Semantic gate: a clean-parsing JSON with the wrong field names must
        # not masquerade as an authoritative empty/unknown cluster — fall back
        # to scontrol instead of silently skipping live checks (or worse,
        # erroring valid GPU jobs with "partition has no GPUs").
        if _json_semantically_valid(partitions, nodes):
            state = ClusterState(partitions=partitions, nodes=nodes, source="sinfo --json")
    if state is None:
        rc_p, text_p = runner.run(["scontrol", "-o", "show", "partitions"])
        if rc_p != 0:
            return None
        rc_n, text_n = runner.run(["scontrol", "-o", "show", "nodes"])
        state = ClusterState(
            partitions=parse_scontrol_partitions(text_p),
            nodes=parse_scontrol_nodes(text_n) if rc_n == 0 else {},
            source="scontrol show",
        )
        if not state.partitions:
            return None  # do not treat empty output as an authoritative empty cluster
    state.fetched_at = time.time()
    _save_cache(cache_file, state)
    return state


def _json_semantically_valid(partitions, nodes) -> bool:
    """True only if the parsed JSON actually carried usable scheduler facts.

    Requires at least one partition with a resolvable MaxTime and at least
    one node name anywhere; anything less means the JSON shape did not match
    what the parser understands (e.g. a different data_parser version).
    """
    if not partitions:
        return False
    with_time = sum(1 for p in partitions.values() if p.max_time is not None)
    if with_time == 0:
        return False
    partition_nodes = [n for p in partitions.values() for n in p.nodes]
    return bool(nodes) or bool(partition_nodes)


def _load_cache(cache_file: Path, ttl: float) -> Optional[ClusterState]:
    try:
        if not cache_file.exists():
            return None
        data = json.loads(cache_file.read_text())
        if time.time() - float(data.get("fetched_at") or 0) > ttl:
            return None
        state = ClusterState.from_dict(data)
        state.source = f"{state.source} (cached)"
        return state
    except (OSError, ValueError):
        return None


def _save_cache(cache_file: Path, state: ClusterState) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(state.to_dict()))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# scontrol text parsing (stable across Slurm versions)
# ---------------------------------------------------------------------------

def parse_scontrol_partitions(text: str) -> Dict[str, PartitionLive]:
    out: Dict[str, PartitionLive] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("PartitionName="):
            continue
        fields = dict(_FIELD_RE.findall(line))
        name = fields.get("PartitionName")
        if not name:
            continue
        nodes = expand_hostlist(fields["Nodes"]) if fields.get("Nodes") else []
        out[name] = PartitionLive(
            name=name,
            max_time=parse_time(fields.get("MaxTime", "")),
            default_time=parse_time(fields.get("DefaultTime", "")),
            nodes=nodes,
        )
    return out


def parse_scontrol_nodes(text: str) -> Dict[str, NodeLive]:
    out: Dict[str, NodeLive] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("NodeName="):
            continue
        fields = dict(_FIELD_RE.findall(line))
        name = fields.get("NodeName")
        if not name:
            continue
        gres_raw = fields.get("Gres", "")
        gres: List[Tuple[str, Optional[str], int]] = []
        if gres_raw and gres_raw != "(null)":
            gres = _parse_gres_list(gres_raw)
        try:
            memory = int(fields.get("RealMemory", ""))
        except ValueError:
            memory = None
        out[name] = NodeLive(
            name=name,
            state=fields.get("State", "UNKNOWN"),
            gres=gres,
            real_memory=memory,
            partitions=[p for p in fields.get("Partitions", "").split(",") if p],
        )
    return out


def _parse_gres_list(raw: str) -> List[Tuple[str, Optional[str], int]]:
    """Parse 'gpu:a100:8(S:0-1),shard:8' style GRES lists."""
    entries = []
    for chunk in raw.split(","):
        chunk = re.sub(r"\([^)]*\)", "", chunk.strip())
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) == 3 and parts[2].isdigit():
            entries.append((parts[0], parts[1], int(parts[2])))
        elif len(parts) == 2 and parts[1].isdigit():
            entries.append((parts[0], None, int(parts[1])))
        elif len(parts) == 2:
            entries.append((parts[0], parts[1], 1))
        else:
            entries.append((chunk, None, 1))
    return entries


# ---------------------------------------------------------------------------
# sinfo --json parsing (best effort; falls back to scontrol on any surprise)
# ---------------------------------------------------------------------------

def parse_sinfo_json(text: str):
    data = json.loads(text)
    p_list = data.get("partitions")
    if not isinstance(p_list, list):
        raise ValueError("no partitions list in sinfo --json output")

    def _num(value):
        """REST data_parser wraps numbers as {"set":..,"infinite":..,"number":..};
        older shapes used "seconds"; bare ints also accepted."""
        if isinstance(value, dict):
            if value.get("infinite"):
                return -1
            for key in ("number", "seconds"):
                if isinstance(value.get(key), int):
                    return value[key]
            return None
        return value if isinstance(value, int) else None

    def _hostlist_names(value) -> List[str]:
        if isinstance(value, list):
            return [n.get("name") for n in value if isinstance(n, dict) and n.get("name")]
        if isinstance(value, dict):
            raw = value.get("list") or value.get("names") or ""
            return expand_hostlist(str(raw)) if raw else []
        if isinstance(value, str) and value:
            return expand_hostlist(value)
        return []

    partitions: Dict[str, PartitionLive] = {}
    for entry in p_list:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        tmax = (entry.get("time") or {}).get("maximum") or {}
        tdef = (entry.get("time") or {}).get("default") or {}
        partitions[name] = PartitionLive(
            name=name,
            max_time=_num(tmax),
            default_time=_num(tdef),
            nodes=_hostlist_names(entry.get("nodes")),
        )
    if not partitions:
        raise ValueError("empty partitions in sinfo --json output")

    nodes_out: Dict[str, NodeLive] = {}
    for entry in data.get("nodes") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        state = entry.get("state")
        if isinstance(state, list):
            state = "+".join(str(s) for s in state)
        state = str(state or "UNKNOWN")
        gres = _parse_tres_gpu(str(entry.get("tres") or "")) or _parse_gres_list(
            str(entry.get("gres") or "")
        )
        parts = entry.get("partitions") or []
        if isinstance(parts, dict):
            parts = parts.get("list") or []
        elif isinstance(parts, str):
            parts = [p for p in parts.split(",") if p]
        nodes_out[name] = NodeLive(
            name=name,
            state=state,
            gres=gres,
            real_memory=_num(entry.get("real_memory")),
            partitions=[str(p) for p in parts if p],
        )
    return partitions, nodes_out


def _parse_tres_gpu(tres: str) -> List[Tuple[str, Optional[str], int]]:
    """'cpu=64,mem=500G,gres/gpu=8,gres/gpu:h200=4' -> gpu entries."""
    entries = []
    if not tres:
        return []
    for token in tres.split(","):
        token = token.strip()
        if not token.startswith("gres/gpu"):
            continue
        key, _, count = token.partition("=")
        suffix = key[len("gres/gpu"):].lstrip(":")
        if not count.isdigit():
            continue
        entries.append(("gpu", suffix or None, int(count)))
    return entries


# ---------------------------------------------------------------------------
# Accounting and test-only
# ---------------------------------------------------------------------------

def accounts(runner: Optional[Runner] = None) -> Optional[List[str]]:
    """The user's Slurm account associations, or None when undiscoverable.

    Never guesses: the H200 account string is an association object, not
    something derivable from the manual.
    """
    acc_qos = accounts_qos(runner)
    return sorted(acc_qos) if acc_qos else None


def accounts_qos(runner: Optional[Runner] = None) -> Dict[str, set]:
    """Account -> set of QOS names from sacctmgr ({} when undiscoverable)."""
    runner = runner or Runner()
    user = getpass.getuser()
    found: Dict[str, set] = {}
    rc, out = runner.run(
        ["sacctmgr", "-nP", "show", "assoc", "user=" + user, "format=Account,QOS"]
    )
    if rc == 0 and out.strip():
        for line in out.strip().splitlines():
            fields = line.split("|")
            acc = fields[0].strip() if fields else ""
            qos = {q.strip() for q in fields[1].split(",") if q.strip()} if len(fields) > 1 else set()
            if acc:
                found.setdefault(acc, set()).update(qos)
    if not found:
        rc, out = runner.run(["sshare", "-U", "-n", "-o", "Account"])
        if rc == 0:
            for line in out.splitlines():
                acc = line.strip()
                if acc and acc != "Account":
                    found.setdefault(acc, set())
    return found


def test_only(runner: Runner, path: str) -> Tuple[bool, str]:
    """Authoritative scheduler-side preflight; submits nothing.

    Note: this validates the request against the scheduler; it cannot catch
    application failures (bad imports, dataset errors, CUDA faults) because
    no job is executed.
    """
    rc, out = runner.run(["sbatch", "--test-only", path])
    return rc == 0, out.strip()
