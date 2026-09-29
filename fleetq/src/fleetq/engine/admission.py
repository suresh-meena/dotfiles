"""Admission: normalize a submission, enforce idempotency, quotas and authorization.

Everything here runs in one transaction with the job insert, so a request is
either admitted completely (job, bundle reference, idempotency record, event)
or not at all.

Admission rejects contradictions and *proven permanent* impossibilities only
(§4.3). A busy GPU, a down node, stale inventory or an unreachable site is not
a proof. Those jobs are admitted and wait with a reason.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..auth import Principal
from ..errors import FqError
from ..util import canonical_json, digest, parse_utc, utcnow
from . import fence, state

SPEC_VERSION = 1
MAX_ARGV = 4096
MAX_ARG_LEN = 32 * 1024
MAX_ENV = 64
MAX_ENV_VALUE = 4096
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Variables the scheduler owns (§6.1). Setting them would silently change
# placement (CUDA_VISIBLE_DEVICES) or make a framework think it's under Slurm.
_RESERVED_ENV_PREFIXES = ("FQ_", "SLURM_", "FLEETCTL_", "FLEETQ_")
_RESERVED_ENV = frozenset({
    "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "NVIDIA_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
})
RETRY_CLASSES = frozenset({"node_fail", "exit", "timeout", "oom", "preempted"})

# Features that exist in the spec grammar but are gated until their phase's
# tests pass (§0.3). An advertised-but-unavailable option is refused, never
# ignored.
FEATURES: dict[str, bool] = {
    "each": True,
    "array": True,
    "dependencies": True,
    "clusters": True,
    "collect": True,
    "hold_remote": False,
}


def _bad(message: str, **details: Any) -> FqError:
    return FqError("invalid_argument", message, details=details)


def _int(value: Any, name: str, *, minimum: int, maximum: int, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise _bad(f"{name} must be between {minimum} and {maximum}", value=value)
    return value


def _str_list(value: Any, name: str, *, max_items: int = 64, max_len: int = 4096) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise _bad(f"{name} must be a list of at most {max_items} strings")
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > max_len or "\x00" in item:
            raise _bad(f"{name} entries must be non-empty strings without NUL")
        out.append(item)
    return out


def _abs_path(value: str, name: str) -> str:
    if not value.startswith("/") or "\x00" in value or any(part == ".." for part in value.split("/")):
        raise _bad(f"{name} must be a canonical absolute path (no '..', no '~')", value=value)
    return "/" + "/".join(p for p in value.split("/") if p not in ("", "."))


def _rel_path(value: str, name: str) -> str:
    if (not value or value.startswith("/") or "\x00" in value
            or any(part in ("..",) for part in value.split("/"))):
        raise _bad(f"{name} must be a relative path inside the code root", value=value)
    return "/".join(p for p in value.split("/") if p not in ("", "."))


def normalize_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize a JobSpec. The result is immutable and hashed."""
    if not isinstance(raw, dict):
        raise _bad("job spec must be an object")
    allowed = {"name", "command", "workdir", "placement", "resources", "needs", "needs_rw", "env",
               "setup", "control", "collect", "notify", "provenance", "spec_version"}
    unknown = set(raw) - allowed
    if unknown:
        raise _bad(f"unknown job spec fields: {sorted(unknown)}")

    name = raw.get("name") or "job"
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _bad("name must be 1-128 of [A-Za-z0-9._-], starting alphanumeric")

    # Exactly one job form (§6.1).
    cmd = raw.get("command")
    if not isinstance(cmd, dict) or len(cmd) != 1 or next(iter(cmd)) not in ("argv", "script", "wrap"):
        raise _bad("command must be exactly one of {argv}, {script, args}, or {wrap}")
    if "argv" in cmd:
        argv = _str_list(cmd["argv"], "command.argv", max_items=MAX_ARGV, max_len=MAX_ARG_LEN)
        if not argv:
            raise _bad("command.argv must not be empty")
        command: dict[str, Any] = {"argv": argv}
    elif "script" in cmd:
        script = cmd["script"]
        if not isinstance(script, dict) or "path" not in script:
            raise _bad("command.script must be {path, args}")
        command = {"script": {"path": _rel_path(script["path"], "command.script.path"),
                              "args": _str_list(script.get("args"), "command.script.args", max_items=MAX_ARGV)}}
    else:
        wrap = cmd["wrap"]
        if not isinstance(wrap, str) or not wrap.strip() or len(wrap) > MAX_ARG_LEN:
            raise _bad("command.wrap must be a non-empty string")
        command = {"wrap": wrap}

    wd = raw.get("workdir") or {}
    if not isinstance(wd, dict) or len(wd) == 0:
        raise _bad("workdir must be {bundle, subdir} or {in_place}")
    if "bundle" in wd:
        bundle = wd["bundle"]
        if not isinstance(bundle, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", bundle):
            raise _bad("workdir.bundle must be a sha256 digest")
        workdir: dict[str, Any] = {"bundle": bundle, "subdir": _rel_path(wd.get("subdir") or ".", "workdir.subdir") or "."}
    elif "in_place" in wd:
        workdir = {"in_place": _abs_path(wd["in_place"], "workdir.in_place")}
    else:
        raise _bad("workdir must name a bundle or an in_place path")
    if "script" in command and "in_place" in workdir:
        # A script path is validated relative to the code root on the node.
        pass

    pl = raw.get("placement") or {}
    if not isinstance(pl, dict):
        raise _bad("placement must be an object")
    on = _str_list(pl.get("on"), "placement.on", max_items=64, max_len=128) or None
    each = _str_list(pl.get("each"), "placement.each", max_items=64, max_len=128) or None
    if on and each:
        raise _bad("use either placement.on (any one) or placement.each (one per machine), not both")
    if "in_place" in workdir and not (on or each):
        raise _bad("an in_place workdir requires explicit destinations (placement.on or .each)")
    spill = pl.get("spill")
    if spill is not None:
        # "Owned machines first; if it hasn't started within after_s, these too" (§4.4).
        if not isinstance(spill, dict) or set(spill) - {"after_s", "to"} or not spill.get("to"):
            raise _bad("placement.spill is {after_s, to: [destinations]}")
        if each:
            raise _bad("an --each member keeps its destination; spill applies to --on/any-one jobs")
        spill = {"after_s": _int(spill.get("after_s"), "placement.spill.after_s", minimum=60, maximum=30 * 86400),
                 "to": _str_list(spill["to"], "placement.spill.to", max_items=8, max_len=128)}
    placement = {
        "on": on,
        "each": each,
        "allow_clusters": bool(pl.get("allow_clusters", False)),
        "queue": pl.get("queue"),
        "account": pl.get("account"),
        "qos": pl.get("qos"),
        "spill": spill,
    }
    for key in ("queue", "account", "qos"):
        if placement[key] is not None and (not isinstance(placement[key], str) or not placement[key]):
            raise _bad(f"placement.{key} must be a non-empty string")

    res = raw.get("resources") or {}
    if not isinstance(res, dict):
        raise _bad("resources must be an object")
    if "gpus" not in res:
        raise _bad("resources.gpus is required; CPU-only jobs request gpus: 0 explicitly (§0.3)")
    resources = {
        "gpus": _int(res.get("gpus"), "resources.gpus", minimum=0, maximum=64),
        "vram_mb": _int(res.get("vram_mb"), "resources.vram_mb", minimum=1, maximum=10_000_000, allow_none=True),
        "gpu_model": res.get("gpu_model"),
        "cpus": _int(res.get("cpus"), "resources.cpus", minimum=1, maximum=4096, allow_none=True),
        "mem_mb": _int(res.get("mem_mb"), "resources.mem_mb", minimum=1, maximum=100_000_000, allow_none=True),
        "time_s": _int(res.get("time_s"), "resources.time_s", minimum=1, maximum=366 * 86400, allow_none=True),
        "scratch_mb": _int(res.get("scratch_mb", 0), "resources.scratch_mb", minimum=0, maximum=100_000_000),
    }
    if resources["gpus"] == 0 and (resources["vram_mb"] or resources["gpu_model"]):
        raise _bad("vram_mb/gpu_model make no sense with gpus: 0")
    if resources["gpu_model"] is not None and not isinstance(resources["gpu_model"], str):
        raise _bad("resources.gpu_model must be a string")

    needs = [_abs_path(p, "needs") for p in _str_list(raw.get("needs"), "needs", max_items=32)]
    needs_rw = [_abs_path(p, "needs_rw") for p in _str_list(raw.get("needs_rw"), "needs_rw", max_items=32)]

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict) or len(env_raw) > MAX_ENV:
        raise _bad(f"env must be an object of at most {MAX_ENV} entries")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not _ENV_KEY_RE.match(key):
            raise _bad(f"invalid environment variable name {key!r}")
        if key in _RESERVED_ENV or key.startswith(_RESERVED_ENV_PREFIXES):
            raise _bad(f"{key} is owned by the scheduler and cannot be set by a job")
        if not isinstance(value, str) or len(value) > MAX_ENV_VALUE or "\x00" in value:
            raise _bad(f"env[{key}] must be a string of at most {MAX_ENV_VALUE} bytes")
        env[key] = value

    setup = raw.get("setup")
    if setup is not None and (not isinstance(setup, str) or len(setup) > MAX_ARG_LEN):
        raise _bad("setup must be a string")

    ctl = raw.get("control") or {}
    if not isinstance(ctl, dict):
        raise _bad("control must be an object")
    unknown = set(ctl) - _CONTROL_KEYS
    if unknown:
        raise _bad(f"unknown control fields {sorted(unknown)}")
    begin = ctl.get("begin")
    if begin is not None:
        try:
            begin = parse_utc(begin) and begin
        except (TypeError, ValueError):
            raise _bad("control.begin must be a UTC time like 2026-09-24T09:00:00.000000Z")
    retry_on = _str_list(ctl.get("retry_on"), "control.retry_on", max_items=8, max_len=32)
    bad_classes = set(retry_on) - RETRY_CLASSES
    if bad_classes:
        raise _bad(f"unknown retry classes {sorted(bad_classes)}; choose from {sorted(RETRY_CLASSES)}")
    after = ctl.get("after") or []
    if not isinstance(after, list) or len(after) > 64:
        raise _bad("control.after must be a list of at most 64 dependencies")
    deps = []
    for dep in after:
        if not isinstance(dep, dict) or set(dep) - {"job", "group", "type"} or ("job" in dep) == ("group" in dep):
            raise _bad("each dependency is {job, type} or {group, type}")
        dep_type = dep.get("type", "afterok")
        if dep_type not in ("afterok", "afterany", "afternotok", "after"):
            raise _bad(f"unknown dependency type {dep_type!r}")
        if "group" in dep:
            # A whole --each group or array: the dependency holds for every member.
            group = dep["group"]
            if not isinstance(group, str) or not re.fullmatch(r"grp_[0-9a-f]{16}", group):
                raise _bad(f"dependency group must look like grp_<16 hex>, got {group!r}")
            deps.append({"group": group, "type": dep_type})
        else:
            deps.append({"job": _int(dep.get("job"), "dependency job", minimum=1, maximum=2**62), "type": dep_type})
    array = ctl.get("array")
    if array is not None:
        array = _normalize_array(array)
    control = {
        "priority": _int(ctl.get("priority", 0), "control.priority", minimum=-1000, maximum=1000),
        "hold": bool(ctl.get("hold", False)),
        "retry": _int(ctl.get("retry", 0), "control.retry", minimum=0, maximum=20),
        "retry_on": sorted(set(retry_on) | ({"node_fail"} if ctl.get("retry", 0) else set())),
        "kill_grace_s": _int(ctl.get("kill_grace_s", 60), "control.kill_grace_s", minimum=1, maximum=3600),
        "warn_signal": _warn_signal(ctl.get("warn_signal")),
        "warn_before_s": _int(ctl.get("warn_before_s", 300), "control.warn_before_s", minimum=1, maximum=86400),
        "interactive": bool(ctl.get("interactive", False)),
        "after": deps,
        "array": array,
        "begin": begin,
    }
    if control["interactive"] and (each or array or placement["allow_clusters"] or placement["queue"]
                                   or placement["spill"]):
        # An allocation you shell into lives on one workstation; clusters get sbatch only.
        raise _bad("an interactive allocation is one job on a workstation: no arrays, groups or clusters")
    if control["warn_signal"] and resources["time_s"] and control["warn_before_s"] >= resources["time_s"]:
        raise _bad("control.warn_before_s must be shorter than the job's time limit")
    if array and each:
        raise _bad("use either placement.each or control.array, not both")

    collect_raw = raw.get("collect") or []
    if not isinstance(collect_raw, list) or len(collect_raw) > 64:
        raise _bad("collect must be a list of at most 64 relative paths")
    collect = []
    for item in collect_raw:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict) or "path" not in item:
            raise _bad("collect entries are paths or {path, required}")
        collect.append({"path": _rel_path(item["path"], "collect.path"), "required": bool(item.get("required", True))})

    if collect and control["interactive"]:
        raise _bad("an interactive allocation has no outputs to collect")
    if collect and "in_place" in workdir:
        # An in-place directory may hold unrelated files; collecting from it needs an
        # approved output root, which v1 does not have (§6.4).
        raise _bad("collect is only for snapshot jobs; an in-place job's directory is not an approved output root")

    notify = _str_list(raw.get("notify"), "notify", max_items=4, max_len=16)
    provenance = raw.get("provenance") or {}
    if not isinstance(provenance, dict) or len(canonical_json(provenance)) > 4096:
        raise _bad("provenance must be a small object")

    return {
        "spec_version": SPEC_VERSION,
        "name": name,
        "command": command,
        "workdir": workdir,
        "placement": placement,
        "resources": resources,
        "needs": needs,
        "needs_rw": needs_rw,
        "env": env,
        "setup": setup,
        "control": control,
        "collect": collect,
        "notify": notify,
        "provenance": provenance,
    }


def _normalize_array(array: Any) -> dict[str, Any]:
    if not isinstance(array, dict) or set(array) - {"indices", "throttle"}:
        raise _bad("control.array is {indices: [...], throttle: n}")
    indices = array.get("indices")
    if not isinstance(indices, list) or not indices or len(indices) > 1000:
        raise _bad("control.array.indices must list 1..1000 indices")
    seen = []
    for index in indices:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 10_000_000:
            raise _bad("array indices must be non-negative integers")
        seen.append(index)
    if len(set(seen)) != len(seen):
        raise _bad("array indices must be unique")
    throttle = array.get("throttle")
    if throttle is not None:
        _int(throttle, "control.array.throttle", minimum=1, maximum=1000)
    return {"indices": sorted(seen), "throttle": throttle}


# ---- inventory view used by admission and placement ------------------------------

def enabled_targets(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["id"]: row for row in conn.execute("SELECT * FROM nodes WHERE enabled = 1")}


def parse_destination(text: str) -> tuple[str, str | None]:
    """``kiac:a100`` -> (``kiac``, ``a100``); ``rtx4090`` -> (``rtx4090``, None)."""
    target, _, queue = text.partition(":")
    return target, (queue or None)


def resolve_candidates(
    conn: sqlite3.Connection, spec: dict[str, Any], principal: Principal
) -> list[tuple[str, str | None]]:
    """Turn placement into concrete ``(target, queue)`` candidates, checking authorization.

    Naming a cluster is the job's opt-in; it is never the token's authorization
    (§4.3). Both are required, plus an enabled site (invariant 9).
    """
    targets = enabled_targets(conn)
    known = {row["id"]: row for row in conn.execute("SELECT * FROM nodes")}
    placement = spec["placement"]
    named = placement["on"] or placement["each"]
    candidates: list[tuple[str, str | None]] = []
    if named:
        for dest in named:
            target, queue = parse_destination(dest)
            if target not in known:
                raise FqError("invalid_argument", f"unknown destination {target!r}",
                              hint="list destinations with `fq nodes`")
            if queue is not None and known[target]["backend"] != "slurm":
                raise FqError("invalid_argument", f"{target!r} is not a cluster; it has no queues")
            candidates.append((target, queue))
    else:
        for target_id, row in targets.items():
            if row["backend"] == "slurm" and not placement["allow_clusters"]:
                continue
            candidates.append((target_id, placement["queue"] if row["backend"] == "slurm" else None))
    if placement["queue"]:
        # --queue narrows cluster candidates only; owned nodes are unaffected.
        # A named cluster that disagrees with it is a contradiction (§4.3).
        q_site, q_name = parse_destination(placement["queue"])
        if q_site not in known or known[q_site]["backend"] != "slurm":
            raise FqError("invalid_argument", f"--queue {placement['queue']!r} does not name a cluster site")
        narrowed: list[tuple[str, str | None]] = []
        for target, queue in candidates:
            if known[target]["backend"] != "slurm":
                narrowed.append((target, queue))
            elif target != q_site:
                if named:
                    raise FqError("invalid_argument",
                                  f"--on names cluster {target!r} but --queue names {q_site!r}")
            elif queue is not None and q_name is not None and queue != q_name:
                raise FqError("invalid_argument",
                              f"--on {target}:{queue} conflicts with --queue {placement['queue']}")
            else:
                narrowed.append((target, queue or q_name))
        if not any(t == q_site for t, _ in narrowed):
            if named:
                raise FqError("invalid_argument",
                              f"--queue names cluster {q_site!r}, which --on does not include")
            narrowed.append((q_site, q_name))
        candidates = narrowed
    for dest in (placement.get("spill") or {}).get("to") or []:
        target, queue = parse_destination(dest)
        if target not in known:
            raise FqError("invalid_argument", f"unknown spill destination {target!r}",
                          hint="list destinations with `fq nodes`")
        if queue is not None and known[target]["backend"] != "slurm":
            raise FqError("invalid_argument", f"{target!r} is not a cluster; it has no queues")
        if (target, queue) not in candidates:
            candidates.append((target, queue))
    if spec["control"].get("interactive"):
        if any(known[t]["backend"] == "slurm" for t, _ in candidates):
            raise FqError("invalid_argument", "interactive sessions run on workstations only; "
                                              "clusters are reached with sbatch, never interactively")
    # A spill destination is named, so it is the job's opt-in -- the token must still allow clusters.
    named = named or (placement.get("spill") or {}).get("to")
    clusters = [t for t, _ in candidates if known[t]["backend"] == "slurm"]
    if clusters:
        if not FEATURES["clusters"]:
            raise FqError("feature_unavailable", "cluster execution is not enabled in this release")
        if not principal.allow_clusters:
            raise FqError("cluster_not_allowed", "this token is not authorized for cluster use",
                          hint="ask for a token created with --clusters")
        if not (named or placement["allow_clusters"] or placement["queue"]):
            raise FqError("cluster_not_allowed", "clusters are used only when named or allowed per job")
    if placement["account"] or placement["qos"]:
        if not clusters:
            raise FqError("invalid_argument", "account/qos apply only to cluster destinations")
    return candidates


def permanently_unsuitable(row: sqlite3.Row, spec: dict[str, Any]) -> str | None:
    """A reason this target can *never* run the job, from verified inventory only.

    Unknown capacity returns None: absence of evidence is not proof (§4.3).
    """
    cfg = json.loads(row["config_json"] or "{}")
    res = spec["resources"]
    caps = cfg.get("capacity") or {}
    if res["gpus"] and caps.get("gpu_count") is not None and res["gpus"] > caps["gpu_count"]:
        return f"needs {res['gpus']} GPUs; {row['id']} has {caps['gpu_count']}"
    if res["vram_mb"] and caps.get("max_vram_mb") is not None and res["vram_mb"] > caps["max_vram_mb"]:
        return f"needs {res['vram_mb']} MiB per GPU; {row['id']} maximum is {caps['max_vram_mb']}"
    if res["mem_mb"] and caps.get("ram_budget_mb") is not None and res["mem_mb"] > caps["ram_budget_mb"]:
        return f"needs {res['mem_mb']} MiB RAM; {row['id']} budget is {caps['ram_budget_mb']}"
    if res["cpus"] and caps.get("cpus") is not None and res["cpus"] > caps["cpus"]:
        return f"needs {res['cpus']} CPUs; {row['id']} has {caps['cpus']}"
    if res["time_s"] and caps.get("max_time_s") is not None and res["time_s"] > caps["max_time_s"]:
        return f"needs {res['time_s']} s; {row['id']} allows at most {caps['max_time_s']}"
    if res["gpu_model"] and caps.get("gpu_models") is not None and res["gpu_model"] not in caps["gpu_models"]:
        return f"no {res['gpu_model']} GPU on {row['id']}"
    return None


# ---- quotas --------------------------------------------------------------------

DEFAULT_QUOTAS = {
    "agent": {"active_jobs": 20, "gpus": 4, "submits_per_minute": 10, "group_size": 50,
              "bundle_bytes": 100 * 1024**3},
    "human": {"active_jobs": 500, "gpus": 64, "submits_per_minute": 60, "group_size": 1000,
              "bundle_bytes": 100 * 1024**3},
    "service": {"active_jobs": 0, "gpus": 0, "submits_per_minute": 0, "group_size": 0,
                "bundle_bytes": 0},
}


def effective_quota(principal: Principal) -> dict[str, int]:
    return {**DEFAULT_QUOTAS[principal.kind], **{k: int(v) for k, v in principal.quota.items()}}


def active_job_count(conn: sqlite3.Connection, *, owner: str | None = None, token_id: str | None = None) -> int:
    clause, args = ("owner = ?", (owner,)) if owner is not None else ("token_id = ?", (token_id,))
    return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE phase <> 'TERMINAL' AND {clause}", args).fetchone()[0]


# ---- admission transaction -------------------------------------------------------

_CONTROL_KEYS = {"priority", "hold", "retry", "retry_on", "kill_grace_s", "after", "array", "begin",
                 "warn_signal", "warn_before_s", "interactive"}
# Signals a job may ask for before its walltime. Not TERM/KILL: those are how it is stopped.
WARN_SIGNALS = ("USR1", "USR2", "INT", "HUP")


def _warn_signal(value: Any) -> str | None:
    if value is None:
        return None
    sig = str(value).upper().removeprefix("SIG")
    if sig not in WARN_SIGNALS:
        raise _bad(f"control.warn_signal must be one of {', '.join(WARN_SIGNALS)}")
    return sig


def check_runnable(conn: sqlite3.Connection, spec: dict[str, Any], principal: Principal) -> dict[str, str]:
    """Refuse a spec that no authorized destination could ever run (§4.3); return why the rest can't."""
    candidates = resolve_candidates(conn, spec, principal)
    if not candidates:
        raise FqError("unsatisfiable", "no enabled destination could run this job",
                      hint="enable a node, or name a destination with --on")
    reasons: dict[str, str] = {}
    known = {row["id"]: row for row in conn.execute("SELECT * FROM nodes")}
    for target, _queue in candidates:
        reason = permanently_unsuitable(known[target], spec)
        if reason:
            reasons[target] = reason
    if len(reasons) == len({t for t, _ in candidates}):
        raise FqError("unsatisfiable", "every authorized destination is permanently unsuitable",
                      details={"reasons": reasons})
    return reasons


# What `fq modify` may change on a job that has not started. The command, code,
# dependencies, fan-out and outputs define *what* the job is; changing those is a
# new job. Everything here only changes where, with what, and when.
MODIFIABLE = {
    "name": None,
    "resources": {"gpus", "vram_mb", "gpu_model", "cpus", "mem_mb", "scratch_mb", "time_s"},
    "placement": {"on", "queue", "allow_clusters", "account", "qos"},
    "control": {"priority", "retry", "retry_on", "kill_grace_s", "begin"},
    "env": None,
    "setup": None,
}


def modify_job(conn: sqlite3.Connection, principal: Principal, job_id: int, patch: dict[str, Any], *,
               actor: str, expect_version: int | None = None) -> None:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job["phase"] not in ("PENDING", "HELD", "BLOCKED") or state.active_attempt(conn, job_id):
        raise FqError("not_modifiable", f"job {job_id} is {job['phase']}; only jobs that have not started can change")
    if not isinstance(patch, dict) or not patch:
        raise FqError("invalid_argument", "a modification names at least one field")
    raw = json.loads(job["spec_json"])
    for key, value in patch.items():
        if key not in MODIFIABLE:
            raise FqError("not_modifiable", f"{key!r} cannot change; submit a new job instead")
        allowed = MODIFIABLE[key]
        if allowed is None:
            raw[key] = value
            continue
        if not isinstance(value, dict) or set(value) - allowed:
            raise FqError("not_modifiable", f"only {sorted(allowed)} of {key!r} can change")
        if key == "placement" and "on" in value and job["group_id"] and job["array_index"] is None:
            raise FqError("not_modifiable", "a member of an --each group keeps its destination")
        raw[key] = {**raw[key], **value}
    new = normalize_spec(raw)
    check_runnable(conn, new, principal)
    state.update_job(conn, job_id, event="modified", actor=actor, expect_version=expect_version,
                     detail={"patch": patch}, spec_json=canonical_json(new),
                     spec_digest=digest({"spec": new, "array_index": job["array_index"]}),
                     name=new["name"], priority=new["control"]["priority"], not_before=new["control"]["begin"],
                     reason=None, **({"phase": "PENDING"} if job["phase"] == "BLOCKED"
                                     and dependency_blocked(conn, job_id) is False else {}))


def dependency_blocked(conn: sqlite3.Connection, job_id: int) -> bool:
    """Whether a BLOCKED job is blocked by a dependency that can now never be satisfied."""
    from .controller import dependency_verdict
    return dependency_verdict(conn, job_id) == "never"


def _check_restore_admission(conn: sqlite3.Connection) -> None:
    if fence.restore_discovery_pending(conn):
        raise FqError("not_ready", "new jobs are paused while restored job state is being discovered",
                      retry_after=300)


def requeue_job(conn: sqlite3.Connection, principal: Principal, job_id: int, *, actor: str) -> int:
    """Run a finished job again, as a new job with the same spec (§4.2).

    The original stays TERMINAL: its outcome, waiters and idempotency replay are
    history, not something to rewrite. The copy carries no dependencies (its
    parents are what produced the original), needs the bundle to still exist,
    and counts against quotas like any submission.
    """
    principal.require("submit")
    _check_restore_admission(conn)
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job["phase"] != "TERMINAL":
        raise FqError("not_modifiable", f"job {job_id} is {job['phase']}; requeue applies to finished jobs")
    spec = json.loads(job["spec_json"])
    spec["control"] = {**spec["control"], "after": [], "hold": False, "begin": None}
    spec = normalize_spec(spec)
    check_runnable(conn, spec, principal)
    quota = effective_quota(principal)
    if active_job_count(conn, token_id=principal.token_id) + 1 > quota["active_jobs"]:
        raise FqError("quota_exceeded", f"token active-job limit {quota['active_jobs']} would be exceeded")
    if job["bundle_digest"] and conn.execute("SELECT 1 FROM bundles WHERE digest = ?",
                                             (job["bundle_digest"],)).fetchone() is None:
        raise FqError("bundle_missing", f"job {job_id}'s code snapshot has been garbage-collected",
                      hint="submit it again from the source directory")
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO jobs (owner, token_id, name, group_id, array_index, spec_json, spec_digest, desired_state,"
        " phase, priority, bundle_digest, submitted_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (principal.owner, principal.token_id, spec["name"], None, job["array_index"], canonical_json(spec),
         digest({"spec": spec, "array_index": job["array_index"], "requeue_of": job_id}), "RUN", "PENDING",
         spec["control"]["priority"], job["bundle_digest"], now, now))
    new_id = cur.lastrowid
    if job["bundle_digest"]:
        conn.execute("INSERT INTO bundle_refs (digest, owner, ref_kind, ref_id, created_at) VALUES (?,?,'job',?,?)",
                     (job["bundle_digest"], principal.owner, str(new_id), now))
    state.add_event(conn, "requeued_as", job_id=job_id, actor=actor, detail={"new_job": new_id})
    state.add_event(conn, "requeue_of", job_id=new_id, actor=actor, detail={"original": job_id})
    return new_id


def replay(
    conn: sqlite3.Connection,
    principal: Principal,
    raw_spec: dict[str, Any],
    *,
    idempotency_key: str,
) -> dict[str, Any] | None:
    """Return a matching prior response, or None when this key is new."""
    principal.require("submit")
    if not idempotency_key or len(idempotency_key) > 200:
        raise FqError("invalid_argument", "an Idempotency-Key (1-200 chars) is required for every submission")
    spec = normalize_spec(raw_spec)
    request_hash = digest(spec)

    prior = conn.execute(
        "SELECT request_hash, response_json FROM idempotency WHERE token_id = ? AND key = ?",
        (principal.token_id, idempotency_key),
    ).fetchone()
    if prior is not None:
        if prior["request_hash"] != request_hash:
            raise FqError("idempotency_conflict",
                          "this idempotency key was already used for a different request",
                          hint="use a new key for a different job")
        response = json.loads(prior["response_json"])
        response["idempotent_replay"] = True
        return response
    return None


def admit(
    conn: sqlite3.Connection,
    principal: Principal,
    raw_spec: dict[str, Any],
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Admit a submission; return the response (also replayed for duplicates)."""
    prior = replay(conn, principal, raw_spec, idempotency_key=idempotency_key)
    if prior is not None:
        return prior
    _check_restore_admission(conn)
    accept = conn.execute("SELECT value FROM controller_meta WHERE key='accept'").fetchone()
    if accept is not None and accept["value"] != "on":
        raise FqError("draining", "fleetqd is not accepting new jobs right now", retry_after=300)
    spec = normalize_spec(raw_spec)
    request_hash = digest(spec)

    # Bundle access: knowing a digest grants nothing; the owner must hold a ref.
    if "bundle" in spec["workdir"]:
        bundle_digest = spec["workdir"]["bundle"]
        ref = conn.execute(
            "SELECT 1 FROM bundle_refs WHERE digest = ? AND owner = ? AND released_at IS NULL",
            (bundle_digest, principal.owner),
        ).fetchone()
        if ref is None:
            raise FqError("bundle_missing", f"bundle {bundle_digest} was not uploaded by {principal.owner}",
                          hint="upload it with PUT /api/v1/bundles/{digest} first")
    else:
        bundle_digest = None

    reasons = check_runnable(conn, spec, principal)

    # Dependencies: parents must exist, be visible, and not form a cycle. A new
    # job can't be anyone's parent yet, so a cycle is impossible at insert time
    # except through self-reference, which the schema also forbids.
    edges: list[tuple[int, str]] = []
    for dep in spec["control"]["after"]:
        if "group" in dep:
            members = conn.execute("SELECT id, owner FROM jobs WHERE group_id = ?", (dep["group"],)).fetchall()
            if not members or any(m["owner"] != principal.owner and not principal.has("manage_all") for m in members):
                raise FqError("invalid_argument", f"dependency group {dep['group']} does not exist or is not yours")
            edges += [(m["id"], dep["type"]) for m in members]
            continue
        parent = conn.execute("SELECT owner FROM jobs WHERE id = ?", (dep["job"],)).fetchone()
        if parent is None or (parent["owner"] != principal.owner and not principal.has("manage_all")):
            raise FqError("invalid_argument", f"dependency job {dep['job']} does not exist or is not yours")
        edges.append((dep["job"], dep["type"]))
    if len(edges) > 4096:
        raise FqError("invalid_argument", f"{len(edges)} dependency edges exceed the limit of 4096")

    quota = effective_quota(principal)
    if spec["placement"]["each"]:
        members = [{"target": d, "array_index": None} for d in spec["placement"]["each"]]
        if len({parse_destination(d)[0] for d in spec["placement"]["each"]}) != len(members):
            raise FqError("invalid_argument", "placement.each names the same destination twice")
    elif spec["control"]["array"]:
        members = [{"target": None, "array_index": i} for i in spec["control"]["array"]["indices"]]
    else:
        members = [{"target": None, "array_index": None}]
    if len(members) > 1 and len(members) > quota["group_size"]:
        raise FqError("quota_exceeded", f"a group of {len(members)} exceeds this token's group limit of {quota['group_size']}")
    owner_row = conn.execute("SELECT quota_json FROM principals WHERE name = ?", (principal.owner,)).fetchone()
    owner_limit = int(json.loads(owner_row["quota_json"] if owner_row else "{}").get(
        "active_jobs", DEFAULT_QUOTAS["human"]["active_jobs"]))
    # Both limits apply: many agent tokens can't multiply one owner's capacity.
    for scope, count, limit in (
        ("token", active_job_count(conn, token_id=principal.token_id), quota["active_jobs"]),
        ("owner", active_job_count(conn, owner=principal.owner), owner_limit),
    ):
        if count + len(members) > limit:
            raise FqError("quota_exceeded", f"{scope} active-job limit {limit} would be exceeded",
                          details={"active": count, "requested": len(members)})

    now = utcnow()
    group_id = None
    if len(members) > 1:
        group_id = f"grp_{digest([principal.token_id, idempotency_key])[7:23]}"
        conn.execute(
            "INSERT INTO groups (id, kind, owner, spec_json, created_at) VALUES (?,?,?,?,?)",
            (group_id, "each" if spec["placement"]["each"] else "array", principal.owner, canonical_json(spec), now),
        )
    phase, desired = ("HELD", "HOLD") if spec["control"]["hold"] else ("PENDING", "RUN")
    job_ids = []
    for member in members:
        member_spec = dict(spec)
        if member["target"] is not None:
            member_spec = {**spec, "placement": {**spec["placement"], "on": [member["target"]], "each": None}}
        member_digest = digest({"spec": member_spec, "array_index": member["array_index"]})
        cur = conn.execute(
            "INSERT INTO jobs (owner, token_id, name, group_id, array_index, spec_json, spec_digest, desired_state,"
            " phase, priority, bundle_digest, submitted_at, updated_at, not_before)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (principal.owner, principal.token_id, spec["name"], group_id, member["array_index"],
             canonical_json(member_spec), member_digest, desired, phase, spec["control"]["priority"],
             bundle_digest, now, now, spec["control"]["begin"]),
        )
        job_id = cur.lastrowid
        job_ids.append(job_id)
        for parent_id, dep_type in edges:
            conn.execute("INSERT OR IGNORE INTO deps (job_id, parent_id, type) VALUES (?,?,?)",
                         (job_id, parent_id, dep_type))
        if bundle_digest:
            conn.execute(
                "INSERT INTO bundle_refs (digest, owner, ref_kind, ref_id, created_at) VALUES (?,?,'job',?,?)",
                (bundle_digest, principal.owner, str(job_id), now),
            )
        state.add_event(conn, "submitted", job_id=job_id, job_version=1, actor=principal.label,
                        detail={"token": principal.token_id, "group": group_id})

    response = {
        "schema": "fq.submit/v1",
        "ok": True,
        "jobs": job_ids,
        "group": {"id": group_id, "size": len(job_ids)} if group_id else None,
        "idempotent_replay": False,
        "unsuitable": reasons,
    }
    conn.execute(
        "INSERT INTO idempotency (token_id, key, request_hash, response_json, created_at) VALUES (?,?,?,?,?)",
        (principal.token_id, idempotency_key, request_hash, json.dumps(response, sort_keys=True), now),
    )
    return response
