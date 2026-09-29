"""The central remote-call budget authority on numpi (§3.5, §8).

Every participating fleetctl, on any machine, asks this authority for a permit
before a managed-cluster control operation; fleetqd's own calls use the same
buckets in-process. Buckets are keyed by the *canonical* cluster, so aliases
and multiple login endpoints share one allowance. Separate classes keep bulk
transfers or slow accounting from taking the only cancellation slot.

Token state is persisted conservatively: a restart computes refill from the
stored time and never refills to a fresh burst; a wall clock that stepped
backwards mints nothing. When the authority can't decide, it denies (§3.5).
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import FqError
from .util import new_id, parse_utc, utcnow

OP_CLASSES = ("monitor", "action", "transfer")
PERMIT_TTL_S = 120


@dataclass(frozen=True)
class BucketPolicy:
    per_minute: float
    burst: float


def policy_for(cfg: dict[str, Any], op_class: str) -> BucketPolicy | None:
    budget = cfg.get("budget") or {}
    rate = budget.get(f"{op_class}_per_minute")
    if rate is None:
        return None
    try:
        per_minute = float(rate)
        burst = float(budget.get("burst", max(1.0, per_minute)))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(per_minute) and math.isfinite(burst) and per_minute > 0 and burst > 0):
        return None
    return BucketPolicy(per_minute=per_minute, burst=burst)


def _canonical(conn: sqlite3.Connection, cluster: str) -> tuple[str, dict[str, Any]]:
    """Resolve an enabled, unambiguous alias to its one canonical cluster."""
    matches = []
    for row in conn.execute("SELECT id, config_json FROM nodes WHERE backend='slurm' AND enabled=1"):
        cfg = json.loads(row["config_json"] or "{}")
        budget = cfg.get("budget") or {}
        raw_aliases = budget.get("aliases") or []
        if not isinstance(raw_aliases, list) or any(not isinstance(alias, str) or not alias for alias in raw_aliases):
            continue
        aliases = set(raw_aliases) | {row["id"]}
        fleetctl_target = cfg.get("fleetctl_target")
        if isinstance(fleetctl_target, str) and fleetctl_target:
            aliases.add(fleetctl_target)
        if cluster in aliases:
            matches.append((row["id"], cfg))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise FqError("forbidden", f"{cluster!r} matches multiple enabled managed clusters")
    raise FqError("forbidden", f"{cluster!r} is not a managed cluster known to this authority")


def _preview_bucket(conn: sqlite3.Connection, table: str, cluster: str, key_name: str, key: str,
                    policy: BucketPolicy, cost: int, now: str) -> dict[str, Any]:
    """Calculate a token-bucket debit without writing, for atomic multi-resource grants."""
    if table == "budget_buckets" and key_name == "op_class":
        row = conn.execute("SELECT tokens, updated_at FROM budget_buckets WHERE cluster=? AND op_class=?",
                           (cluster, key)).fetchone()
    elif table == "budget_dimension_buckets" and key_name == "dimension":
        row = conn.execute("SELECT tokens, updated_at FROM budget_dimension_buckets WHERE cluster=? AND dimension=?",
                           (cluster, key)).fetchone()
    else:
        raise ValueError("unsupported budget bucket")
    tokens = policy.burst
    if row is not None:
        try:
            previous = float(row["tokens"])
        except (TypeError, ValueError, OverflowError):
            previous = 0.0
        if not math.isfinite(previous) or previous < 0:
            previous = 0.0
        try:
            elapsed = (parse_utc(now) - parse_utc(row["updated_at"])).total_seconds()
        except (TypeError, ValueError, OverflowError):
            previous, elapsed = 0.0, 0.0
        tokens = min(policy.burst, previous + max(0.0, elapsed) * policy.per_minute / 60.0)
    if cost > policy.burst:
        return {"granted": False, "retry_after": None, "tokens": tokens, "reason": "burst_too_small"}
    if tokens >= cost:
        return {"granted": True, "retry_after": 0.0, "tokens": tokens - cost}
    retry = (cost - tokens) / (policy.per_minute / 60.0)
    return {"granted": False, "retry_after": retry, "tokens": tokens, "reason": "budget_exhausted"}


def _write_bucket(conn: sqlite3.Connection, table: str, cluster: str, key_name: str,
                  key: str, tokens: float, now: str) -> None:
    if table == "budget_buckets" and key_name == "op_class":
        conn.execute(
            "INSERT INTO budget_buckets (cluster, op_class, tokens, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(cluster, op_class) DO UPDATE SET tokens=excluded.tokens,updated_at=excluded.updated_at",
            (cluster, key, tokens, now),
        )
    elif table == "budget_dimension_buckets" and key_name == "dimension":
        conn.execute(
            "INSERT INTO budget_dimension_buckets (cluster, dimension, tokens, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(cluster, dimension) DO UPDATE SET tokens=excluded.tokens,updated_at=excluded.updated_at",
            (cluster, key, tokens, now),
        )
    else:
        raise ValueError("unsupported budget bucket")


def _required_dimension_policy(budget: dict[str, Any], rate_key: str, burst_key: str) -> BucketPolicy | None:
    """Separate session/byte rates are explicit site policy, never inferred."""
    try:
        rate = float(budget[rate_key])
        burst = float(budget.get(burst_key, max(1.0, rate)))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(rate) and math.isfinite(burst) and rate > 0 and burst > 0):
        return None
    return BucketPolicy(per_minute=rate, burst=burst)


def grant_permit(conn: sqlite3.Connection, *, cluster: str, op_class: str, cost: dict[str, int],
                 caller: str) -> dict[str, Any]:
    if op_class not in OP_CLASSES:
        raise FqError("invalid_argument", f"op_class must be one of {OP_CLASSES}")
    if not isinstance(cost, dict) or any(
        isinstance(cost.get(name, default), bool) or not isinstance(cost.get(name, default), int)
        for name, default in (("rpc", 1), ("sessions", 1), ("bytes", 0))
    ) or set(cost) - {"rpc", "sessions", "bytes"}:
        raise FqError("invalid_argument", "permit costs must be finite integers")
    rpc = cost.get("rpc", 1)
    sessions = cost.get("sessions", 1)
    transfer_bytes = cost.get("bytes", 0)
    if not (0 <= rpc <= 1000 and 1 <= sessions <= 100 and 0 <= transfer_bytes <= 1 << 40):
        raise FqError("invalid_argument", "permit cost exceeds supported bounds")
    canonical, cfg = _canonical(conn, cluster)
    policy = cfg.get("budget") or {}
    session_cap = policy.get("max_sessions_per_operation")
    byte_cap = policy.get("max_bytes_per_transfer")
    if (isinstance(session_cap, bool) or not isinstance(session_cap, int)
            or isinstance(byte_cap, bool) or not isinstance(byte_cap, int)):
        session_cap = byte_cap = None
    if (session_cap is None or byte_cap is None or session_cap <= 0 or byte_cap <= 0
            or sessions > session_cap or transfer_bytes > byte_cap
            or transfer_bytes and op_class != "transfer"):
        reason = "session_cap" if session_cap is None or sessions > int(session_cap) else "transfer_cap"
        return {"schema": "fq.permit/v1", "ok": True, "granted": False,
                "retry_after": 300.0, "cluster": canonical, "reason": reason}
    session_policy = _required_dimension_policy(policy, "sessions_per_minute", "sessions_burst")
    byte_policy = _required_dimension_policy(policy, "bytes_per_minute", "bytes_burst")
    class_policy = policy_for(cfg, op_class)
    if session_policy is None or byte_policy is None or class_policy is None:
        return {"schema": "fq.permit/v1", "ok": True, "granted": False,
                "retry_after": 300.0, "cluster": canonical, "reason": "dimension_policy_missing"}
    if session_policy.burst < session_cap or byte_policy.burst < byte_cap:
        return {"schema": "fq.permit/v1", "ok": True, "granted": False,
                "retry_after": 300.0, "cluster": canonical, "reason": "dimension_burst_too_small"}
    reserve = policy.get("action_session_reserve")
    if (isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < session_cap
            or reserve > session_policy.burst):
        return {"schema": "fq.permit/v1", "ok": True, "granted": False,
                "retry_after": 300.0, "cluster": canonical, "reason": "action_session_reserve_invalid"}

    # Preview every dimension first, then debit all of them in this same
    # connection transaction. A denied session/byte budget never partially
    # spends the RPC bucket.
    now = utcnow()
    plans = [
        ("budget_buckets", "op_class", op_class,
         _preview_bucket(conn, "budget_buckets", canonical, "op_class", op_class,
                         class_policy, max(1, rpc), now)),
        ("budget_dimension_buckets", "dimension", "sessions",
         _preview_bucket(conn, "budget_dimension_buckets", canonical, "dimension", "sessions",
                         session_policy, sessions, now)),
    ]
    # Monitoring and bulk transfer must leave enough session capacity for a
    # single full action (for example, cancellation). Action grants may use
    # the reserve itself.
    session_plan = plans[1][3]
    if op_class != "action" and session_plan["granted"] and session_plan["tokens"] < reserve:
        session_plan["granted"] = False
        session_plan["retry_after"] = (reserve - session_plan["tokens"]) / (session_policy.per_minute / 60.0)
        session_plan["reason"] = "action_session_reserve"
    if transfer_bytes:
        plans.append(("budget_dimension_buckets", "dimension", "bytes",
                      _preview_bucket(conn, "budget_dimension_buckets", canonical, "dimension", "bytes",
                                      byte_policy, transfer_bytes, now)))
    denied = [entry[3] for entry in plans if not entry[3]["granted"]]
    if denied:
        retry_values = [entry["retry_after"] for entry in denied if entry["retry_after"] is not None]
        reason = "budget_exhausted"
        if any(entry.get("reason") == "burst_too_small" for entry in denied):
            reason = "dimension_burst_too_small"
        elif any(entry.get("reason") == "action_session_reserve" for entry in denied):
            reason = "action_session_reserve"
        elif not plans[1][3]["granted"]:
            reason = "session_budget_exhausted"
        elif len(plans) > 2 and not plans[2][3]["granted"]:
            reason = "byte_budget_exhausted"
        now = utcnow()
        conn.execute(
            "INSERT INTO remote_calls (ts,target,op_class,rpc,sessions,bytes,outcome) VALUES (?,?,?,?,?,?,?)",
            (now, canonical, op_class, rpc, sessions, transfer_bytes, reason),
        )
        return {"schema": "fq.permit/v1", "ok": True, "granted": False,
                "retry_after": round(max(retry_values), 1) if retry_values else 300.0,
                "cluster": canonical, "reason": reason}
    for table, key_name, key, plan in plans:
        _write_bucket(conn, table, canonical, key_name, key, plan["tokens"], now)
    permit_id = new_id("pmt")
    now = utcnow()
    expires = (parse_utc(now).timestamp() + PERMIT_TTL_S)
    import datetime as _dt
    expires_at = _dt.datetime.fromtimestamp(expires, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute(
        "INSERT INTO permits (id, cluster, op_class, cost_json, caller, granted_at, expires_at) VALUES (?,?,?,?,?,?,?)",
        (permit_id, canonical, op_class,
         json.dumps({"rpc": rpc, "sessions": sessions, "bytes": transfer_bytes}, sort_keys=True),
         caller[:64], now, expires_at),
    )
    conn.execute(
        "INSERT INTO remote_calls (ts, target, op_class, rpc, sessions, bytes, outcome) VALUES (?,?,?,?,?,?,?)",
        (now, canonical, op_class, rpc, sessions, transfer_bytes, "permitted"),
    )
    return {"schema": "fq.permit/v1", "ok": True, "granted": True, "permit_id": permit_id,
            "cluster": canonical, "expires_at": expires_at}


def redeem_permit(conn: sqlite3.Connection, permit_id: str, *, cluster: str, op_class: str,
                  cost: dict[str, int] | None = None) -> dict[str, Any]:
    """Single-use: a co-resident caller's pre-acquired permit is valid exactly once, and only
    for the bucket it was drawn from. A mismatched redeem still burns it (its cost is spent)."""
    row = conn.execute("SELECT * FROM permits WHERE id = ?", (permit_id,)).fetchone()
    if row is None or row["redeemed_at"] is not None or parse_utc(row["expires_at"]) <= parse_utc(utcnow()):
        return {"schema": "fq.permit/v1", "ok": True, "valid": False}
    conn.execute("UPDATE permits SET redeemed_at = ? WHERE id = ?", (utcnow(), permit_id))
    try:
        canonical, _ = _canonical(conn, cluster)
    except FqError:
        canonical = None
    if (canonical, op_class) != (row["cluster"], row["op_class"]):
        return {"schema": "fq.permit/v1", "ok": True, "valid": False, "reason": "bucket_mismatch"}
    expected = json.loads(row["cost_json"])
    requested = cost if cost is not None else {"rpc": 1, "sessions": 1, "bytes": 0}
    try:
        normalized = {k: int(requested.get(k, default)) for k, default in
                      (("rpc", 1), ("sessions", 1), ("bytes", 0))}
    except (AttributeError, TypeError, ValueError, OverflowError):
        normalized = None
    if normalized != expected:
        return {"schema": "fq.permit/v1", "ok": True, "valid": False, "reason": "cost_mismatch"}
    # Echo the caller's own name for the cluster so it can check the answer without knowing our aliases.
    return {"schema": "fq.permit/v1", "ok": True, "valid": True, "cluster": cluster, "canonical": canonical,
            "op_class": op_class}


def usage(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT target, op_class, COUNT(*) calls, SUM(rpc) rpc, SUM(sessions) sessions, SUM(bytes) bytes"
        " FROM remote_calls WHERE ts >= ? GROUP BY target, op_class ORDER BY target, op_class", (since,))]


def remote_cost_ledger(conn: sqlite3.Connection, since: str, *, now: str | None = None) -> list[dict[str, Any]]:
    """Read-only per-site permit spending and current approved bucket capacity.

    Only permitted rows spent tokens. Denials are reported separately; they
    must not inflate the usage figures shown to operators.
    """
    now = now or utcnow()
    totals = {(r["target"], r["op_class"]): dict(r) for r in conn.execute(
        "SELECT target, op_class, "
        "SUM(CASE WHEN outcome='permitted' THEN 1 ELSE 0 END) calls_today, "
        "SUM(CASE WHEN outcome='permitted' THEN rpc ELSE 0 END) rpc_today, "
        "SUM(CASE WHEN outcome='permitted' THEN sessions ELSE 0 END) sessions_today, "
        "SUM(CASE WHEN outcome='permitted' THEN bytes ELSE 0 END) bytes_today, "
        "SUM(CASE WHEN outcome!='permitted' THEN 1 ELSE 0 END) denied_today "
        "FROM remote_calls WHERE ts >= ? GROUP BY target, op_class", (since,))}

    def available(table: str, site: str, dimension: str, key: str, policy: BucketPolicy | None) -> float | None:
        if policy is None:
            return None
        return round(_preview_bucket(conn, table, site, dimension, key, policy, 0, now)["tokens"], 3)

    ledger = []
    for row in conn.execute("SELECT id, config_json FROM nodes WHERE backend='slurm' AND enabled=1 ORDER BY id"):
        site = row["id"]
        cfg = json.loads(row["config_json"] or "{}")
        limits = cfg.get("budget") or {}
        classes = []
        for op_class in OP_CLASSES:
            policy = policy_for(cfg, op_class)
            used = totals.get((site, op_class), {})
            classes.append({"op_class": op_class,
                            "per_minute": policy.per_minute if policy else None,
                            "burst": policy.burst if policy else None,
                            "available": available("budget_buckets", site, "op_class", op_class, policy),
                            "calls_today": used.get("calls_today", 0),
                            "rpc_today": used.get("rpc_today", 0),
                            "denied_today": used.get("denied_today", 0)})
        dimensions = {}
        for name, rate_key, burst_key, spent_key in (
            ("sessions", "sessions_per_minute", "sessions_burst", "sessions_today"),
            ("bytes", "bytes_per_minute", "bytes_burst", "bytes_today"),
        ):
            policy = _required_dimension_policy(limits, rate_key, burst_key)
            dimensions[name] = {
                "per_minute": policy.per_minute if policy else None,
                "burst": policy.burst if policy else None,
                "available": available("budget_dimension_buckets", site, "dimension", name, policy),
                "used_today": sum(totals.get((site, kind), {}).get(spent_key, 0) for kind in OP_CLASSES),
            }
        ledger.append({"site_id": site, "classes": classes, **dimensions,
                       "action_session_reserve": limits.get("action_session_reserve")})
    return ledger
