"""State reducer and schema-level invariants (§1.2, §5)."""

from __future__ import annotations

import sqlite3

import pytest

from fleetq.db.store import Store, check_sqlite_gate, sqlite_has_wal_reset_fix
from fleetq.engine import state
from fleetq.errors import FqError, InvariantViolation
from fleetq.util import utcnow


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "fq.db")
    s.open()
    s.run_sync(_seed)
    yield s
    s.close()


def _seed(conn: sqlite3.Connection) -> None:
    now = utcnow()
    conn.execute("INSERT INTO principals (name, created_at) VALUES ('suresh', ?)", (now,))
    conn.execute(
        "INSERT INTO tokens (id, owner, kind, label, secret_sha256, scopes, created_at)"
        " VALUES ('tok1','suresh','human','laptop','x','submit read',?)",
        (now,),
    )


def _job(conn: sqlite3.Connection, phase: str = "PENDING") -> int:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO jobs (owner, token_id, name, spec_json, spec_digest, desired_state, phase,"
        " submitted_at, updated_at) VALUES ('suresh','tok1','j','{}','sha256:x','RUN',?,?,?)",
        (phase, now, now),
    )
    return cur.lastrowid


def _gpu(node: str, uuid: str) -> dict:
    return {"node_id": node, "kind": "gpu", "gpu_uuid": uuid, "amount": 1}


def test_sqlite_gate_ranges():
    assert sqlite_has_wal_reset_fix("3.51.3")
    assert sqlite_has_wal_reset_fix("3.50.7")
    assert sqlite_has_wal_reset_fix("3.44.6")
    assert not sqlite_has_wal_reset_fix("3.51.2")
    assert not sqlite_has_wal_reset_fix("3.50.6")
    assert not sqlite_has_wal_reset_fix("3.45.1")
    ok, msg = check_sqlite_gate(attestation="fedora advisory FEDORA-2026-x")
    assert ok and "attestation" in msg or "includes" in msg


def test_create_attempt_moves_job_and_bumps_version(store):
    def run(conn):
        jid = _job(conn)
        before = state.get_job(conn, jid)["version"]
        att = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1,
                                   reservations=[_gpu("n1", "GPU-a")])
        job = state.get_job(conn, jid)
        events = [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE job_id=?", (jid,))]
        return before, job["version"], job["phase"], att["state"], att["remote_may_be_live"], events

    before, after, phase, astate, live, events = store.run_sync(run)
    assert after == before + 1
    assert phase == "DISPATCHING"
    assert astate == "PLANNED" and live == 0
    assert "attempt_planned" in events and "dispatching" in events


def test_same_gpu_cannot_be_reserved_twice(store):
    def run(conn):
        j1, j2 = _job(conn), _job(conn)
        state.create_attempt(conn, j1, backend="fake", target="n1", epoch=1, reservations=[_gpu("n1", "GPU-a")])
        state.create_attempt(conn, j2, backend="fake", target="n1", epoch=1, reservations=[_gpu("n1", "GPU-a")])

    with pytest.raises(sqlite3.IntegrityError):
        store.run_sync(run)
    # The whole transaction rolled back: nothing half-planned remains.
    assert store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]) == 0


def test_released_gpu_can_be_reserved_again(store):
    def run(conn):
        j1, j2 = _job(conn), _job(conn)
        a = state.create_attempt(conn, j1, backend="fake", target="n1", epoch=1, reservations=[_gpu("n1", "GPU-a")])
        state.update_attempt(conn, a["id"], state="STAGING", event="staging")
        state.update_attempt(conn, a["id"], state="NEVER_STARTED", event="never_started")
        state.release_reservations(conn, a["id"], reason="test")
        state.create_attempt(conn, j2, backend="fake", target="n1", epoch=1, reservations=[_gpu("n1", "GPU-a")])
        return state.invariant_violations(conn)

    assert store.run_sync(run) == []


def test_possibly_live_set_on_launch_and_blocks_second_attempt(store):
    def run(conn):
        jid = _job(conn)
        a = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[])
        state.update_attempt(conn, a["id"], state="STAGING", event="staging")
        a = state.update_attempt(conn, a["id"], state="LAUNCHING", event="launching")
        assert a["remote_may_be_live"] == 1
        # An uncertain launch keeps it live; the job can't be retried or finished.
        a = state.update_attempt(conn, a["id"], state="START_UNKNOWN", event="start_unknown")
        assert a["remote_may_be_live"] == 1
        state.update_job(conn, jid, event="x", actor="t", phase="RECONCILING")
        with pytest.raises(InvariantViolation):
            state.update_job(conn, jid, event="x", actor="t", phase="TERMINAL", execution_outcome="FAILED")

    store.run_sync(run)


def test_illegal_transitions_are_rejected(store):
    def run(conn):
        jid = _job(conn)
        a = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[])
        state.update_attempt(conn, a["id"], state="RUNNING", event="bad")  # PLANNED -> RUNNING skips the send

    with pytest.raises(InvariantViolation):
        store.run_sync(run)


def test_reservations_held_until_stop_is_proven(store):
    def run(conn):
        jid = _job(conn)
        a = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[_gpu("n1", "GPU-a")])
        state.update_attempt(conn, a["id"], state="STAGING", event="s")
        state.update_attempt(conn, a["id"], state="LAUNCHING", event="l")
        state.update_attempt(conn, a["id"], state="RUNNING", event="r")
        state.release_reservations(conn, a["id"], reason="premature")

    with pytest.raises(InvariantViolation):
        store.run_sync(run)


def test_version_conflict(store):
    def run(conn):
        jid = _job(conn)
        state.update_job(conn, jid, event="hold", actor="t", expect_version=99, phase="HELD", desired_state="HOLD")

    with pytest.raises(FqError) as exc:
        store.run_sync(run)
    assert exc.value.code == "version_conflict"


def test_held_phase_cannot_hide_an_active_attempt(store):
    def run(conn):
        jid = _job(conn)
        state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[])
        state.update_job(conn, jid, event="hold", actor="t", desired_state="HOLD", phase="HELD")

    with pytest.raises(InvariantViolation, match="active"):
        store.run_sync(run)


def test_terminal_requires_outcome(store):
    def run(conn):
        jid = _job(conn)
        state.update_job(conn, jid, event="x", actor="t", phase="TERMINAL")

    with pytest.raises(sqlite3.IntegrityError):
        store.run_sync(run)


def test_schema_refuses_newer_database(tmp_path):
    db = tmp_path / "new.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    s = Store(db)
    with pytest.raises(RuntimeError, match="newer"):
        s.open()


def test_online_backup_is_consistent(store, tmp_path):
    store.run_sync(lambda c: _job(c))
    dest = tmp_path / "backup.db"
    store.backup(dest)
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_online_backup_refuses_existing_destination_without_clobbering(store, tmp_path):
    from fleetq.errors import FqError

    dest = tmp_path / "backup.db"
    dest.write_bytes(b"keep this file")
    with pytest.raises(FqError, match="already exists"):
        store.backup(dest)
    assert dest.read_bytes() == b"keep this file"


def test_online_backup_refuses_symlink_destination_without_following_it(store, tmp_path):
    from fleetq.errors import FqError

    important = tmp_path / "important.db"
    important.write_bytes(b"keep this target")
    dest = tmp_path / "backup.db"
    dest.symlink_to(important)
    with pytest.raises(FqError, match="already exists"):
        store.backup(dest)
    assert dest.is_symlink()
    assert important.read_bytes() == b"keep this target"


def test_online_backup_refuses_live_database_alias(store):
    from fleetq.errors import FqError

    with pytest.raises(FqError, match="aliases the live database"):
        store.backup(store.path)
