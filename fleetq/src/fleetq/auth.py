"""Bearer tokens, scopes and ownership (§6.5).

A token is ``fq_<id>_<secret>``. The database stores only a SHA-256 of the
secret, compared in constant time. Scopes restrict *API operations*: submitted
code still runs with the remote Unix account's full authority (§0.3). So a
token is, in effect, the right to run code on every enrolled machine, and is
treated that way.

Ownership: a human token manages its owner's jobs (including jobs that owner's
agents submitted); an agent token manages only jobs that *exact* token
submitted, unless it holds ``manage_all``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .errors import FqError
from .util import new_id, parse_utc, utcnow

# read_all: see everyone's jobs, change nothing -- for dashboards such as fleetmon.
SCOPES = frozenset({"read", "read_all", "logs", "submit", "manage_own", "manage_all", "nodes", "admin", "permits"})
KINDS = frozenset({"human", "agent", "service"})
DEFAULT_SCOPES = {
    "human": ("read", "logs", "submit", "manage_own"),
    "agent": ("read", "logs", "submit", "manage_own"),
    "service": ("read",),
}
_TOKEN_RE = re.compile(r"^fq_([0-9a-f]{16})_([A-Za-z0-9_-]{43})$")
_LAST_USED_GRANULARITY_S = 60


@dataclass(frozen=True)
class Principal:
    token_id: str
    owner: str
    kind: str
    label: str
    scopes: frozenset[str]
    allow_clusters: bool
    quota: dict[str, Any] = field(default_factory=dict)

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise FqError("forbidden", f"this token lacks the {scope!r} scope")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_token(
    conn: sqlite3.Connection,
    *,
    owner: str,
    kind: str,
    label: str,
    scopes: tuple[str, ...] | None = None,
    allow_clusters: bool = False,
    expires_at: str | None = None,
    quota: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Create a token; return ``(token_id, token_string)``. The string is shown once."""
    if kind not in KINDS:
        raise FqError("invalid_argument", f"token kind must be one of {sorted(KINDS)}")
    chosen = tuple(scopes) if scopes else DEFAULT_SCOPES[kind]
    bad = set(chosen) - SCOPES
    if bad:
        raise FqError("invalid_argument", f"unknown scopes: {sorted(bad)}")
    conn.execute(
        "INSERT INTO principals (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
        (owner, utcnow()),
    )
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)[:43].ljust(43, "A")
    conn.execute(
        "INSERT INTO tokens (id, owner, kind, label, secret_sha256, scopes, quota_json, allow_clusters,"
        " created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (token_id, owner, kind, label, _hash(secret), " ".join(sorted(chosen)),
         json.dumps(quota or {}, sort_keys=True), int(allow_clusters), utcnow(), expires_at),
    )
    return token_id, f"fq_{token_id}_{secret}"


def revoke_token(conn: sqlite3.Connection, token_id: str) -> None:
    cur = conn.execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (utcnow(), token_id))
    if not cur.rowcount:
        raise FqError("not_found", f"no active token {token_id}")


def verify(conn: sqlite3.Connection, presented: str | None, *, clock_ok: bool = False) -> Principal:
    """Resolve a presented bearer string to a Principal, or raise ``unauthorized``.

    With an unhealthy clock, a token that carries an expiry can't be checked,
    so it is refused rather than silently accepted (§4.6).
    """
    match = _TOKEN_RE.match(presented or "")
    if not match:
        raise FqError("unauthorized", "missing or malformed bearer token")
    token_id, secret = match.groups()
    row = conn.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)).fetchone()
    # Compare even when the row is missing, so timing reveals nothing about ids.
    expected = row["secret_sha256"] if row else _hash("x" * 43)
    if not hmac.compare_digest(expected, _hash(secret)) or row is None:
        raise FqError("unauthorized", "unknown or invalid token")
    if row["revoked_at"]:
        raise FqError("unauthorized", "token has been revoked")
    if row["expires_at"]:
        if not clock_ok:
            raise FqError("unauthorized", "clock health is uncertain; tokens with an expiry cannot be checked")
        if parse_utc(row["expires_at"]) <= parse_utc(utcnow()):
            raise FqError("unauthorized", "token has expired")
    last = row["last_used_at"]
    if last is None or (parse_utc(utcnow()) - parse_utc(last)).total_seconds() > _LAST_USED_GRANULARITY_S:
        conn.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (utcnow(), token_id))
    # Token-level quota only; owner-level limits are applied separately so
    # that many tokens can't multiply one owner's capacity (§6.5).
    quota = json.loads(row["quota_json"])
    return Principal(
        token_id=token_id,
        owner=row["owner"],
        kind=row["kind"],
        label=row["label"],
        scopes=frozenset(row["scopes"].split()),
        allow_clusters=bool(row["allow_clusters"]),
        quota=quota,
    )


def can_manage(principal: Principal, job: sqlite3.Row) -> bool:
    if principal.has("manage_all"):
        return True
    if not principal.has("manage_own"):
        return False
    if principal.kind == "agent":
        return job["token_id"] == principal.token_id
    return job["owner"] == principal.owner


def can_read(principal: Principal, job: sqlite3.Row) -> bool:
    if principal.has("manage_all") or principal.has("read_all"):
        return True
    if not principal.has("read"):
        return False
    return job["owner"] == principal.owner


def new_request_key() -> str:
    return new_id("idem")
