"""Candidate filtering, resource resolution and scoring (§2.5, §4.3–4.4).

Pure functions over database rows plus an optional capacity view, so they are
deterministic and unit-testable. Placement decides *where* to try; the node's
own launch gate still has the final word on GPUs and physical headroom (§2.3).

Reservations cover every resource, not just GPUs: two attempts must not both
reserve the same apparent RAM or scratch headroom (§2.5).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..util import parse_utc, utcnow
from . import fence
from .admission import parse_destination


class SharedCapacity(Protocol):
    """Answers the conservative shared-node idle question (§2.2). See ``engine.idle``."""

    def placeable_gpus(self, node_id: str, uuids: list[str]) -> tuple[list[str], dict[str, str]]: ...


@dataclass
class Placement:
    target: str
    backend: str
    queue: str | None
    gpus: list[str]
    resources: dict[str, int]
    reservations: list[dict[str, Any]]
    score: tuple


@dataclass
class Rejection:
    target: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def _open_sum(conn: sqlite3.Connection, node_id: str, kind: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM resource_reservations"
        " WHERE node_id = ? AND kind = ? AND released_at IS NULL",
        (node_id, kind),
    ).fetchone()[0]


def _reserved_gpus(conn: sqlite3.Connection, node_id: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT gpu_uuid FROM resource_reservations WHERE node_id = ? AND kind = 'gpu' AND released_at IS NULL",
            (node_id,),
        )
    }


def _owner_gpus_in_use(conn: sqlite3.Connection, owner: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM resource_reservations r JOIN attempts a ON a.id = r.attempt_id"
        " JOIN jobs j ON j.id = a.job_id WHERE j.owner = ? AND r.kind = 'gpu' AND r.released_at IS NULL",
        (owner,),
    ).fetchone()[0]


def _token_gpus_in_use(conn: sqlite3.Connection, token_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM resource_reservations r JOIN attempts a ON a.id = r.attempt_id"
        " JOIN jobs j ON j.id = a.job_id WHERE j.token_id = ? AND r.kind = 'gpu' AND r.released_at IS NULL",
        (token_id,),
    ).fetchone()[0]


def resolve_resources(spec: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, int] | None, str | None]:
    """Fill omitted values from the node's approved profile, or refuse (§4.3).

    There is no universal RAM-per-GPU default: guessing one could make a real
    workstation unsafe. A node without a default and a job without a value means
    the job waits with a reason.
    """
    res = spec["resources"]
    defaults = cfg.get("defaults") or {}
    mem = res["mem_mb"] or defaults.get("mem_mb")
    cpus = res["cpus"] or defaults.get("cpus")
    time_s = res["time_s"] or defaults.get("time_s")
    missing = [name for name, value in (("mem_mb", mem), ("cpus", cpus), ("time_s", time_s)) if not value]
    if missing:
        return None, f"no value and no node default for {', '.join(missing)}"
    return {"gpus": res["gpus"], "mem_mb": int(mem), "cpus": int(cpus), "time_s": int(time_s),
            "scratch_mb": int(res["scratch_mb"])}, None


def candidate_targets(conn: sqlite3.Connection, job: sqlite3.Row, spec: dict[str, Any]) -> list[tuple[str, str | None]]:
    named = spec["placement"]["on"] or spec["placement"]["each"]
    token = conn.execute("SELECT allow_clusters FROM tokens WHERE id = ?", (job["token_id"],)).fetchone()
    allow_clusters = bool(token and token["allow_clusters"])
    rows = {row["id"]: row for row in conn.execute("SELECT * FROM nodes WHERE enabled = 1")}
    out: list[tuple[str, str | None]] = []
    if named:
        for dest in named:
            target, queue = parse_destination(dest)
            if target in rows:
                out.append((target, queue or (spec["placement"]["queue"] and parse_destination(spec["placement"]["queue"])[1])))
    else:
        for target, row in sorted(rows.items()):
            if row["backend"] == "slurm":
                if not (spec["placement"]["allow_clusters"] or spec["placement"]["queue"]):
                    continue
            out.append((target, None))
        if spec["placement"]["queue"]:
            q_site, q_name = parse_destination(spec["placement"]["queue"])
            out = [(t, q_name if t == q_site else q) for t, q in out
                   if rows[t]["backend"] != "slurm" or t == q_site]
    if not allow_clusters:
        out = [(t, q) for t, q in out if rows[t]["backend"] != "slurm"]
    return out


def spill_targets(conn: sqlite3.Connection, job: sqlite3.Row, spill: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Enabled spill destinations this job's token may use (clusters need its permission)."""
    token = conn.execute("SELECT allow_clusters FROM tokens WHERE id = ?", (job["token_id"],)).fetchone()
    rows = {row["id"]: row for row in conn.execute("SELECT id, backend FROM nodes WHERE enabled = 1")}
    out = []
    for dest in spill["to"]:
        target, queue = parse_destination(dest)
        if target in rows and (rows[target]["backend"] != "slurm" or (token and token["allow_clusters"])):
            out.append((target, queue))
    return out


def place_job(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    *,
    busy_targets: set[str],
    quota: dict[str, int],
    shared: SharedCapacity | None = None,
    now: str | None = None,
    held_for: dict[str, int] | None = None,
) -> tuple[Placement | None, list[Rejection]]:
    """Choose one concrete placement for ``job``, or explain why none fits now.

    ``held_for`` maps a target to the starving job it is being drained for; no
    other job is placed there until that job starts (§4.4).
    """
    spec = json.loads(job["spec_json"])
    now = now or utcnow()
    rejections: list[Rejection] = []
    placements: list[Placement] = []
    want_gpus = spec["resources"]["gpus"]

    if want_gpus:
        used = _token_gpus_in_use(conn, job["token_id"])
        if used + want_gpus > quota.get("gpus", 0):
            return None, [Rejection("*", "gpu_quota", {"in_use": used, "limit": quota.get("gpus", 0)})]

    candidates = candidate_targets(conn, job, spec)
    # After TIMEOUT or preemption the job's checkpoint is on the machine it ran on;
    # prefer going back there (a soft preference: --on pins it hard).
    last = conn.execute("SELECT target, outcome FROM attempts WHERE job_id = ? ORDER BY n DESC LIMIT 1",
                        (job["id"],)).fetchone()
    resume_on = last["target"] if last and last["outcome"] in ("TIMEOUT", "PREEMPTED") else None
    spilled: set[str] = set()
    spill = spec["placement"].get("spill")
    if spill:
        waited = (parse_utc(now) - parse_utc(job["submitted_at"])).total_seconds()
        if waited >= spill["after_s"]:
            extra = [c for c in spill_targets(conn, job, spill) if c not in candidates]
            spilled = {t for t, _ in extra}
            candidates += extra
        else:
            rejections.append(Rejection("spill", "spill_not_yet",
                                        {"in_s": int(spill["after_s"] - waited), "to": spill["to"]}))
    for target, queue in candidates:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (target,)).fetchone()
        cfg = json.loads(row["config_json"] or "{}")
        if row["drain_kind"]:
            rejections.append(Rejection(target, "drained", {"reason": row["drain_reason"]}))
            continue
        if not fence.target_dispatch_ready(conn, target):
            rejections.append(Rejection(target, "not_reconciled"))
            continue
        if target in busy_targets:
            rejections.append(Rejection(target, "dispatch_in_flight"))
            continue
        if held_for and held_for.get(target, job["id"]) != job["id"]:
            rejections.append(Rejection(target, "held_for_starving_job", {"job": held_for[target]}))
            continue
        if row["backend"] == "slurm":
            placement = _place_slurm(conn, job, spec, row, cfg, queue, rejections)
            if placement:
                placements.append(placement)
            continue
        if row["backend"] == "bare":
            # Enabled in config is not enough: the node's own evidence must say it can
            # hold a job (linger, user manager, writable root, GPU inventory) (§10).
            probe = json.loads(row["last_probe_json"]) if row["last_probe_json"] else None
            if probe is None:
                rejections.append(Rejection(target, "not_probed", {"hint": f"fleetqd node probe {target}"}))
                continue
            if not probe["verdict"]["fit"]:
                rejections.append(Rejection(target, "probe_blockers", {"blockers": probe["verdict"]["blockers"]}))
                continue
        resources, why = resolve_resources(spec, cfg)
        if resources is None:
            rejections.append(Rejection(target, "resource_unspecified", {"why": why}))
            continue
        caps = cfg.get("capacity") or {}
        if spec["workdir"].get("in_place"):
            allowed = cfg.get("in_place_roots") or []
            path = spec["workdir"]["in_place"]
            if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in allowed):
                rejections.append(Rejection(target, "in_place_not_allowed", {"path": path}))
                continue
        # Policy reservations (§2.5): committed sums + new must fit the budget.
        checks = (
            ("mem_mb", "ram", caps.get("ram_budget_mb")),
            ("cpus", "cpu", caps.get("cpus")),
            ("scratch_mb", "scratch", caps.get("scratch_budget_mb")),
        )
        short = None
        for key, kind, budget in checks:
            if budget is None:
                short = Rejection(target, "capacity_unknown", {"resource": kind})
                break
            if _open_sum(conn, target, kind) + resources[key] > budget:
                short = Rejection(target, "insufficient_" + kind,
                                  {"committed": _open_sum(conn, target, kind), "requested": resources[key], "budget": budget})
                break
        if short:
            rejections.append(short)
            continue
        slots = caps.get("job_slots", 1)
        if _open_sum(conn, target, "slot") + 1 > slots:
            rejections.append(Rejection(target, "no_job_slot", {"slots": slots}))
            continue

        chosen: list[str] = []
        if want_gpus:
            chosen, gpu_reasons = _choose_gpus(conn, target, row["mode"], spec, want_gpus, now, shared)
            if not chosen:
                rejections.append(Rejection(target, "no_placeable_gpus", gpu_reasons))
                continue
        reservations = [
            {"node_id": target, "kind": "ram", "amount": resources["mem_mb"]},
            {"node_id": target, "kind": "cpu", "amount": resources["cpus"]},
            {"node_id": target, "kind": "scratch", "amount": resources["scratch_mb"]},
            {"node_id": target, "kind": "slot", "amount": 1},
            *({"node_id": target, "kind": "gpu", "gpu_uuid": uuid, "amount": 1} for uuid in chosen),
        ]
        vram_surplus = 0
        if chosen:
            vrams = [conn.execute("SELECT vram_total FROM node_gpus WHERE node_id=? AND uuid=?", (target, u)).fetchone()[0] or 0
                     for u in chosen]
            vram_surplus = sum(v - (spec["resources"]["vram_mb"] or 0) for v in vrams)
        failures = _recent_failures(conn, job["id"], target)
        score = (
            failures,                                   # anti-thrash: fewer failures of this job here
            0 if row["mode"] != "shared" else 1,        # exclusive before shared (spare labmates)
            vram_surplus,                               # best-fit VRAM
            target,                                     # deterministic tie-break
        )
        placements.append(Placement(target, row["backend"], None, chosen, resources, reservations, score))

    if not placements:
        return None, rejections
    # A spill destination widens the choice; it never outranks where the job asked to run.
    placements.sort(key=lambda p: (p.target in spilled, p.target != resume_on if resume_on else False,
                                   p.score))
    return placements[0], rejections


def _choose_gpus(conn, node_id, mode, spec, want, now, shared) -> tuple[list[str], dict[str, Any]]:
    res = spec["resources"]
    reserved = _reserved_gpus(conn, node_id)
    reasons: dict[str, str] = {}
    usable: list[tuple[str, int]] = []
    for g in conn.execute("SELECT * FROM node_gpus WHERE node_id = ? ORDER BY uuid", (node_id,)):
        uuid = g["uuid"]
        if uuid in reserved:
            reasons[uuid] = "reserved_by_fleetq"
        elif g["reserved"]:
            reasons[uuid] = "owner_reserved"
        elif g["drained"]:
            reasons[uuid] = f"drained:{g['drain_reason']}"
        elif g["cooldown_until"] and parse_utc(g["cooldown_until"]) > parse_utc(now):
            reasons[uuid] = "cooldown"
        elif res["vram_mb"] and (g["vram_total"] or 0) < res["vram_mb"]:
            reasons[uuid] = "vram_too_small"
        elif res["gpu_model"] and (g["model"] or "") != res["gpu_model"]:
            reasons[uuid] = "model_mismatch"
        else:
            usable.append((uuid, g["vram_total"] or 0))
    if mode == "shared":
        if shared is None:
            return [], {**reasons, "_node": "shared_capacity_unavailable"}
        ok, shared_reasons = shared.placeable_gpus(node_id, [u for u, _ in usable])
        reasons.update(shared_reasons)
        usable = [(u, v) for u, v in usable if u in ok]
    if len(usable) < want:
        return [], reasons
    # Prefer a homogeneous set of the smallest sufficient GPUs.
    usable.sort(key=lambda item: (item[1], item[0]))
    by_model: dict[str, list[str]] = {}
    for uuid, _ in usable:
        model = conn.execute("SELECT model FROM node_gpus WHERE node_id=? AND uuid=?", (node_id, uuid)).fetchone()[0] or ""
        by_model.setdefault(model, []).append(uuid)
    for model_uuids in sorted(by_model.values(), key=len, reverse=True):
        if len(model_uuids) >= want:
            return model_uuids[:want], reasons
    return [u for u, _ in usable[:want]], reasons


def could_ever_fit(conn: sqlite3.Connection, spec: dict[str, Any], target: str) -> bool:
    """Would ``spec`` fit on ``target`` once everything fleetq placed there has finished?

    Static capability only (budgets, slots, GPUs that aren't owner-reserved or
    drained and match VRAM/model); current use is ignored. Clusters run their own
    queue, so fleetq never drains one.
    """
    row = conn.execute("SELECT backend, config_json FROM nodes WHERE id = ? AND enabled = 1", (target,)).fetchone()
    if row is None or row["backend"] == "slurm":
        return False
    cfg = json.loads(row["config_json"] or "{}")
    resources, _ = resolve_resources(spec, cfg)
    if resources is None:
        return False
    caps = cfg.get("capacity") or {}
    for key, budget in (("mem_mb", caps.get("ram_budget_mb")), ("cpus", caps.get("cpus")),
                        ("scratch_mb", caps.get("scratch_budget_mb"))):
        if budget is None or resources[key] > budget:
            return False
    res = spec["resources"]
    usable = [g for g in conn.execute("SELECT * FROM node_gpus WHERE node_id = ?", (target,))
              if not g["reserved"] and not g["drained"]
              and not (res["vram_mb"] and (g["vram_total"] or 0) < res["vram_mb"])
              and not (res["gpu_model"] and (g["model"] or "") != res["gpu_model"])]
    return len(usable) >= res["gpus"]


def gpus_in_use(conn: sqlite3.Connection, target: str) -> int:
    return len(_reserved_gpus(conn, target))


def _recent_failures(conn: sqlite3.Connection, job_id: int, target: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE job_id = ? AND target = ? AND outcome IS NOT NULL AND outcome <> 'COMPLETED'",
        (job_id, target),
    ).fetchone()[0]


def _place_slurm(conn, job, spec, row, cfg, queue, rejections) -> Placement | None:
    """Placement onto a cluster: a site profile queue plus local commitment caps (§3.7)."""
    from ..slurm.profile import resolve_site_request  # local import: slurm is P2

    request, why = resolve_site_request(cfg, spec, queue)
    if request is None:
        rejections.append(Rejection(row["id"], "site_request_invalid", {"why": why}))
        return None
    cap_gpus = (cfg.get("caps") or {}).get("gpus")
    cap_jobs = (cfg.get("caps") or {}).get("jobs")
    used_gpus = _open_sum(conn, row["id"], "cluster_gpus")
    used_jobs = _open_sum(conn, row["id"], "cluster_jobs")
    if cap_jobs is not None and used_jobs + 1 > cap_jobs:
        rejections.append(Rejection(row["id"], "cluster_job_cap", {"committed": used_jobs, "cap": cap_jobs}))
        return None
    if cap_gpus is not None and used_gpus + request["gpus"] > cap_gpus:
        rejections.append(Rejection(row["id"], "cluster_gpu_cap", {"committed": used_gpus, "cap": cap_gpus}))
        return None
    reservations = [
        {"node_id": row["id"], "kind": "cluster_jobs", "amount": 1},
        {"node_id": row["id"], "kind": "cluster_gpus", "amount": request["gpus"]},
    ]
    return Placement(row["id"], "slurm", request["queue"], [], request, reservations,
                     (_recent_failures(conn, job["id"], row["id"]), 2, 0, row["id"]))
