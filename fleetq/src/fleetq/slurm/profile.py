"""Slurm site profiles (§3.1): normalized, owner-approved request resolution.

A site profile is part of a node's config (``config_json.site``). Queue preset,
partition, account and QOS are separate fields. They are normalized once here,
and the resolved request plus the profile digest travel with the attempt.
Explicit account/QOS overrides are checked; they can't bypass the profile.
"""

from __future__ import annotations

from typing import Any

from ..util import digest


def profile_digest(site: dict[str, Any]) -> str:
    return digest(site)


def resolve_site_request(cfg: dict[str, Any], spec: dict[str, Any], queue: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a job onto one queue of this site, or say why it can't be."""
    site = cfg.get("site")
    if not site:
        return None, "no approved site profile"
    queues = site.get("queues") or {}
    if not queues:
        return None, "site profile defines no queues"
    name = queue or site.get("default_queue")
    if name is None:
        return None, "no queue named and the site has no default queue"
    q = queues.get(name)
    if q is None:
        return None, f"site has no queue {name!r}"
    res = spec["resources"]
    placement = spec["placement"]
    account = placement.get("account") or q.get("account")
    qos = placement.get("qos") or q.get("qos")
    allowed_accounts = q.get("allowed_accounts")
    if q.get("account_required") and not account:
        return None, f"queue {name} requires an account"
    if allowed_accounts is not None and account is not None and account not in allowed_accounts:
        return None, f"account {account} is not permitted on queue {name}"
    if account is not None and account in (q.get("denied_accounts") or []):
        return None, f"account {account} is denied on queue {name}"
    required_qos = q.get("required_qos")
    if required_qos and qos != required_qos:
        if placement.get("qos") and placement["qos"] != required_qos:
            return None, f"queue {name} requires qos {required_qos}"
        qos = required_qos
    time_s = res["time_s"] or q.get("default_time_s")
    if not time_s:
        return None, f"no time requested and queue {name} has no default"
    if q.get("max_time_s") and time_s > q["max_time_s"]:
        return None, f"requested time exceeds queue {name} maximum {q['max_time_s']} s"
    gpus = res["gpus"]
    if q.get("max_gpus") is not None and gpus > q["max_gpus"]:
        return None, f"queue {name} allows at most {q['max_gpus']} GPUs per job"
    if q.get("gpu_only") and gpus == 0:
        return None, f"queue {name} is GPU-only; a CPU-only job is not allowed there"
    mem = res["mem_mb"] or q.get("default_mem_mb")
    cpus = res["cpus"] or q.get("default_cpus")
    if not mem or not cpus:
        return None, f"memory/cpus not requested and queue {name} has no defaults"
    return {
        "queue": name,
        "partition": q.get("partition", name),
        "account": account,
        "qos": qos,
        "gres_type": q.get("gres_type"),
        "gpus": gpus,
        "time_s": int(time_s),
        "mem_mb": int(mem),
        "cpus": int(cpus),
        "profile_digest": profile_digest(site),
    }, None
