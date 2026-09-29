"""Typed errors with stable codes.

A code is the contract: clients and agents branch on it, so it never changes
meaning within API v1.  The HTTP status and CLI exit class are derived from the
code, not chosen at each raise site, so one failure can't be reported two ways.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# code -> (http status, cli exit code, retryable)
ERROR_CODES: dict[str, tuple[int, int, bool]] = {
    "invalid_argument": (400, 2, False),
    "unauthorized": (401, 6, False),
    "forbidden": (403, 2, False),
    "not_found": (404, 3, False),
    "conflict": (409, 2, False),
    "version_conflict": (409, 2, False),
    "idempotency_conflict": (409, 2, False),
    "not_modifiable": (409, 2, False),
    "not_running": (409, 2, False),
    "unsatisfiable": (422, 2, False),
    "preflight_refused": (422, 2, False),
    "feature_unavailable": (422, 2, False),
    "cluster_not_allowed": (403, 2, False),
    "quota_exceeded": (429, 2, False),
    "rate_limited": (429, 8, True),
    "bundle_too_large": (413, 2, False),
    "bundle_invalid": (400, 2, False),
    "bundle_missing": (404, 3, False),
    "insufficient_storage": (507, 5, True),
    "unsafe_storage": (500, 7, False),
    "draining": (503, 5, True),
    "not_ready": (503, 5, True),
    "version_mismatch": (400, 7, False),
    "internal": (500, 7, True),
}


@dataclass
class FqError(Exception):
    """A user-facing failure with a stable code."""

    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    retry_after: float | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:  # pragma: no cover - programming error
            raise ValueError(f"unregistered error code {self.code!r}")
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return ERROR_CODES[self.code][0]

    @property
    def exit_code(self) -> int:
        return ERROR_CODES[self.code][1]

    @property
    def retryable(self) -> bool:
        return ERROR_CODES[self.code][2]

    def envelope(self) -> dict[str, Any]:
        return {
            "schema": "fq.error/v1",
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "retry_after": self.retry_after,
                "hint": self.hint,
                "details": self.details,
            },
        }


class InvariantViolation(RuntimeError):
    """A state change that would break a §1.2 invariant.

    Never caught to "recover": it means a bug, and the transaction that raised
    it is rolled back so the database never records the broken state.
    """
