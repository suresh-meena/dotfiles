"""Time-limited credentials fail closed while the host clock is uncertain."""

from __future__ import annotations

import datetime as dt

import pytest

from fleetq import auth
from fleetq.errors import FqError

from harness import Harness


def test_expiring_token_needs_healthy_clock_but_recovery_token_does_not(tmp_path):
    h = Harness(tmp_path)
    try:
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _, limited = h.store.run_sync(lambda conn: auth.create_token(
            conn, owner="suresh", kind="human", label="limited", expires_at=expiry))
        with pytest.raises(FqError, match="clock health is uncertain"):
            h.store.run_sync(lambda conn: auth.verify(conn, limited))
        assert h.store.run_sync(lambda conn: auth.verify(conn, limited, clock_ok=True)).owner == "suresh"
        assert h.store.run_sync(lambda conn: auth.verify(conn, h.token)).owner == "suresh"
    finally:
        h.close()
