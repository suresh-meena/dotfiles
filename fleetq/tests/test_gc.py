"""Safety tests for the deliberately narrow local GC command."""
from __future__ import annotations

import sqlite3
import os
from pathlib import Path

import pytest

from fleetq import gc
from fleetq.db import schema
from fleetq.engine.fence import ControllerLock, ControllerLockHeld


@pytest.fixture

def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(schema.DDL)
    conn.execute("INSERT INTO principals(name,created_at) VALUES('alice','2020-01-01T00:00:00Z')")
    conn.execute("INSERT INTO tokens(id,owner,kind,label,secret_sha256,scopes,created_at)"
                 " VALUES('tok','alice','human','test','x','[]','2020-01-01T00:00:00Z')")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture

def bundle_root(tmp_path):
    root = tmp_path / "bundles"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def put_bundle(conn, root: Path, digest: str, *, created="2020-01-01T00:00:00Z", refs=()):
    sha = digest * 64
    name = f"sha256:{sha}"
    path = root / f"{sha}.tar.gz"
    path.write_bytes(b"bundle")
    conn.execute("INSERT INTO bundles(digest,compressed_bytes,expanded_bytes,members,format_version,path,created_at)"
                 " VALUES(?,?,0,0,1,?,?)", (name, path.stat().st_size, str(path), created))
    for ref_kind, ref_id, released in refs:
        conn.execute("INSERT INTO bundle_refs(digest,owner,ref_kind,ref_id,created_at,released_at)"
                     " VALUES(?,'alice',?,?,?,?)", (name, ref_kind, ref_id, created, released))
    return name, path


def put_job_pin(conn, digest):
    conn.execute("INSERT INTO jobs(owner,token_id,name,spec_json,spec_digest,desired_state,phase,execution_outcome,"
                 "submitted_at,updated_at,bundle_digest) VALUES('alice','tok','j','{}','s','RUN','TERMINAL','COMPLETED',"
                 "'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z',?)", (digest,))


def put_artifact_job(conn, root: Path, *, job_state="COMPLETE", attempt_state="RELEASED", success=1):
    ended = "2020-01-01T00:00:00Z"
    cur = conn.execute("INSERT INTO jobs(owner,token_id,name,spec_json,spec_digest,desired_state,phase,execution_outcome,"
                       "success,artifacts_state,submitted_at,updated_at,ended_at) VALUES('alice','tok','artifact','{}','s',"
                       "'RUN','TERMINAL','COMPLETED',?,?,?, ?,?)", (success, job_state, ended, ended, ended))
    jid = cur.lastrowid
    conn.execute("INSERT INTO attempts(id,job_id,n,backend,target,epoch,state,remote_may_be_live,launch_op_id,spec_digest,"
                 "created_at,updated_at) VALUES(?, ?,1,'slurm','host',1,?,0,?,'s',?,?)",
                 (f"attempt-{jid}", jid, attempt_state, f"op-{jid}", ended, ended))
    attempt_dir = root / str(jid) / "1"
    attempt_dir.mkdir(parents=True, mode=0o700)
    attempt_dir.chmod(0o700)
    (root / str(jid)).chmod(0o700)
    path = attempt_dir / "result.txt"
    path.write_text("collected")
    conn.execute("INSERT INTO artifacts(job_id,attempt_id,relpath,state,size,sha256,local_path,updated_at)"
                 " VALUES(?,?,?,'COMPLETE',?, 'sha',?,?)",
                 (jid, f"attempt-{jid}", "result.txt", path.stat().st_size, str(path), ended))
    return jid, path


@pytest.fixture
def artifact_root(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    state_root.chmod(0o700)
    root = state_root / "artifacts"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def test_preview_and_apply_remove_only_old_unreferenced_bundle(db, bundle_root):
    old_digest, old_path = put_bundle(db, bundle_root, "a")
    recent_digest, recent_path = put_bundle(db, bundle_root, "b", created="2099-01-01T00:00:00Z")
    db.commit()
    report = gc.preview(db, bundle_root, now=1_800_000_000)
    assert [x["digest"] for x in report["eligible"]] == [old_digest]
    assert recent_path.exists()

    db.execute("BEGIN IMMEDIATE")
    report = gc.apply(db, bundle_root, now=1_800_000_000)
    db.commit()
    assert [x["digest"] for x in report["removed"]] == [old_digest]
    assert not old_path.exists()
    assert db.execute("SELECT 1 FROM bundles WHERE digest=?", (old_digest,)).fetchone() is None
    assert recent_path.exists()
    assert db.execute("SELECT 1 FROM bundles WHERE digest=?", (recent_digest,)).fetchone() is not None


@pytest.mark.parametrize("kind,released", [("upload", None), ("job", None), ("upload", "2020-02-01T00:00:00Z")])
def test_any_reference_row_pins_bundle_even_if_released(db, bundle_root, kind, released):
    digest, path = put_bundle(db, bundle_root, "c", refs=[(kind, "pin", released)])
    assert gc.preview(db, bundle_root, now=1_800_000_000)["eligible"] == []
    assert gc.apply(db, bundle_root, now=1_800_000_000)["removed"] == []
    assert path.exists()
    assert db.execute("SELECT 1 FROM bundles WHERE digest=?", (digest,)).fetchone()


def test_job_digest_pins_bundle_even_without_bundle_ref(db, bundle_root):
    digest, path = put_bundle(db, bundle_root, "d")
    put_job_pin(db, digest)
    assert gc.preview(db, bundle_root, now=1_800_000_000)["eligible"] == []
    assert path.exists()


def test_reference_added_after_preview_prevents_deletion(db, bundle_root):
    digest, path = put_bundle(db, bundle_root, "7")
    assert [item["digest"] for item in gc.preview(db, bundle_root, now=1_800_000_000)["eligible"]] == [digest]
    db.execute("INSERT INTO bundle_refs(digest,owner,ref_kind,ref_id,created_at) "
               "VALUES(?,'alice','job','late','2020-01-01T00:00:00Z')", (digest,))
    report = gc.apply(db, bundle_root, now=1_800_000_000)
    assert report["eligible"] == [] and report["removed"] == []
    assert path.exists()
    assert db.execute("SELECT 1 FROM bundles WHERE digest=?", (digest,)).fetchone()


def test_missing_file_and_symlink_are_blocked(db, bundle_root, tmp_path):
    digest, path = put_bundle(db, bundle_root, "e")
    path.unlink()
    report = gc.preview(db, bundle_root, now=1_800_000_000)
    assert report["eligible"] == []
    assert report["blocked"][0]["digest"] == digest

    _, symlink = put_bundle(db, bundle_root, "f")
    symlink.unlink()
    target = tmp_path / "outside"
    target.write_text("keep")
    symlink.symlink_to(target)
    report = gc.preview(db, bundle_root, now=1_800_000_000)
    assert not any(x["digest"].endswith("f" * 64) for x in report["eligible"])
    assert any(x["digest"].endswith("f" * 64) for x in report["blocked"])
    assert target.read_text() == "keep"


def test_noncanonical_path_and_insecure_root_are_blocked(db, bundle_root, tmp_path):
    digest, path = put_bundle(db, bundle_root, "1")
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"outside")
    db.execute("UPDATE bundles SET path=? WHERE digest=?", (str(outside), digest))
    report = gc.preview(db, bundle_root, now=1_800_000_000)
    assert report["eligible"] == [] and report["blocked"]
    assert outside.exists()

    bundle_root.chmod(0o755)
    report = gc.preview(db, bundle_root, now=1_800_000_000)
    assert report["eligible"] == []
    assert report["blocked"][0]["reason"] == "bundle_root_not_private_owned_directory"
    assert path.exists()


def test_controller_lock_prevents_concurrent_gc_owner(tmp_path):
    first = ControllerLock(tmp_path)
    second = ControllerLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(ControllerLockHeld):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_controller_lock_refuses_symlink_without_truncating_target(tmp_path):
    important = tmp_path / "important"
    important.write_text("keep this data")
    (tmp_path / "controller.lock").symlink_to(important)
    with pytest.raises(OSError):
        ControllerLock(tmp_path).acquire()
    assert important.read_text() == "keep this data"


def test_controller_lock_refuses_hardlink_without_truncating_target(tmp_path):
    important = tmp_path / "important"
    important.write_text("keep this data")
    important.chmod(0o600)
    (tmp_path / "controller.lock").hardlink_to(important)
    with pytest.raises(RuntimeError, match="unsafe controller lock"):
        ControllerLock(tmp_path).acquire()
    assert important.read_text() == "keep this data"


def test_artifacts_expire_then_purge_without_changing_execution_outcome(db, tmp_path, artifact_root):
    jid, path = put_artifact_job(db, artifact_root)
    now = 1_800_000_000
    reserved = db.execute("SELECT SUM(size) FROM artifacts").fetchone()[0]
    report = gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)
    assert [x["job_id"] for x in report["eligible_artifacts"]] == [jid]
    assert report["purgeable_artifacts"] == []
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    first = gc.apply(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)
    db.commit()
    assert [x["job_id"] for x in first["expired_artifacts"]] == [jid]
    assert path.exists()
    row = db.execute("SELECT phase,artifacts_state,execution_outcome,success,version FROM jobs WHERE id=?", (jid,)).fetchone()
    assert tuple(row[:4]) == ("TERMINAL", "EXPIRED", "COMPLETED", 1)
    assert row["version"] == 2
    assert db.execute("SELECT state,local_path FROM artifacts WHERE job_id=?", (jid,)).fetchone()["state"] == "EXPIRED"
    assert db.execute("SELECT SUM(size) FROM artifacts").fetchone()[0] == reserved

    assert [x["job_id"] for x in gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)["purgeable_artifacts"]] == [jid]
    db.execute("BEGIN IMMEDIATE")
    second = gc.apply(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)
    db.commit()
    assert [x["job_id"] for x in second["removed_artifacts"]] == [jid]
    assert not path.exists()
    row = db.execute("SELECT phase,artifacts_state,execution_outcome,success FROM jobs WHERE id=?", (jid,)).fetchone()
    assert tuple(row) == ("TERMINAL", "EXPIRED", "COMPLETED", 1)
    purged = db.execute("SELECT local_path,size FROM artifacts WHERE job_id=?", (jid,)).fetchone()
    assert purged["local_path"] is None and purged["size"] is None
    assert db.execute("SELECT COALESCE(SUM(size),0) FROM artifacts").fetchone()[0] == 0


def test_artifact_reference_race_is_rechecked_before_expiry(db, tmp_path, artifact_root):
    jid, path = put_artifact_job(db, artifact_root)
    assert gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)["eligible_artifacts"]
    # A new nonterminal dependent pins the parent's collected data.
    cur = db.execute("INSERT INTO jobs(owner,token_id,name,spec_json,spec_digest,desired_state,phase,submitted_at,updated_at)"
                     " VALUES('alice','tok','child','{}','child','RUN','PENDING','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
    db.execute("INSERT INTO deps(job_id,parent_id,type) VALUES(?,?,'after')", (cur.lastrowid, jid))
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    report = gc.apply(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)
    db.commit()
    assert not report["expired_artifacts"]
    assert any(x["job_id"] == jid and x["reason"] == "active_dependent" for x in report["blocked_artifacts"])
    assert path.exists()
    assert db.execute("SELECT artifacts_state FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "COMPLETE"


def test_artifact_unsafe_paths_and_uncertain_attempts_are_blocked(db, tmp_path, artifact_root):
    jid, path = put_artifact_job(db, artifact_root)
    external = tmp_path / "external"
    external.write_text("keep")
    path.unlink()
    path.symlink_to(external)
    report = gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)
    assert not report["eligible_artifacts"]
    assert any(x["job_id"] == jid for x in report["blocked_artifacts"])
    assert external.read_text() == "keep"

    path.unlink()
    path.write_text("collected")
    db.execute("UPDATE attempts SET state='SUBMITTING',remote_may_be_live=1 WHERE job_id=?", (jid,))
    report = gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)
    assert not report["eligible_artifacts"]
    assert any(x["job_id"] == jid and x["reason"] == "attempt_live_or_unreleased" for x in report["blocked_artifacts"])
    assert path.exists()


def test_artifact_gc_rejects_corrupt_attempt_number_traversal(db, tmp_path, artifact_root):
    jid, path = put_artifact_job(db, artifact_root, job_state="EXPIRED")
    state_root = artifact_root.parent
    sentinel = state_root / "keep.txt"
    sentinel.write_text("state data")
    # SQLite can retain nonnumeric text in an INTEGER-affinity column despite
    # the schema's n >= 1 check. A path component like this must never reach rmtree.
    db.execute("UPDATE attempts SET n='../..' WHERE job_id=?", (jid,))
    db.execute("UPDATE artifacts SET state='EXPIRED', local_path=? WHERE job_id=?", (str(state_root / "keep.txt"), jid))

    report = gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)
    assert report["purgeable_artifacts"] == []
    assert any(item.get("job_id") == jid and item["reason"] == "unsafe_artifact_attempt_number"
               for item in report["blocked_artifacts"])

    db.commit()
    db.execute("BEGIN IMMEDIATE")
    applied = gc.apply(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)
    db.commit()
    assert not applied["removed_artifacts"]
    assert sentinel.read_text() == "state data"
    assert path.exists()


def test_failed_artifact_is_never_collected(db, tmp_path, artifact_root):
    jid, path = put_artifact_job(db, artifact_root)
    db.execute("UPDATE artifacts SET state='FAILED' WHERE job_id=?", (jid,))
    report = gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)
    assert not report["eligible_artifacts"]
    assert path.exists()
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    applied = gc.apply(db, tmp_path / "bundles", artifact_dir=artifact_root, now=1_800_000_000)
    db.commit()
    assert not applied["expired_artifacts"] and not applied["removed_artifacts"]
    assert path.exists()
    assert db.execute("SELECT artifacts_state FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "COMPLETE"


def test_old_generated_upload_parts_are_previewed_and_removed(db, bundle_root, tmp_path):
    temp_dir = bundle_root / "tmp"
    temp_dir.mkdir(mode=0o700)
    temp_dir.chmod(0o700)
    now = 1_800_000_000
    old = temp_dir / ("a" * 24 + ".part")
    old.write_bytes(b"stale")
    os.utime(old, (now - gc.BUNDLE_GRACE_SECONDS - 1, now - gc.BUNDLE_GRACE_SECONDS - 1))
    recent = temp_dir / ("b" * 24 + ".part")
    recent.write_bytes(b"live")
    os.utime(recent, (now - 100, now - 100))
    malformed = temp_dir / ("c" * 23 + ".part")
    malformed.write_bytes(b"keep")

    report = gc.preview(db, bundle_root, now=now)
    assert [item["path"] for item in report["eligible_temp"]] == [str(old)]
    assert recent.exists() and malformed.exists()
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    applied = gc.apply(db, bundle_root, now=now)
    db.commit()
    assert [item["path"] for item in applied["removed_temp"]] == [str(old)]
    assert not old.exists() and recent.exists() and malformed.exists()


def test_upload_part_symlink_and_unsafe_temp_directory_are_blocked(db, bundle_root, tmp_path):
    temp_dir = bundle_root / "tmp"
    temp_dir.mkdir(mode=0o700)
    temp_dir.chmod(0o700)
    target = tmp_path / "outside"
    target.write_text("keep")
    link = temp_dir / ("d" * 24 + ".part")
    link.symlink_to(target)
    report = gc.preview(db, bundle_root, now=1_800_000_000)
    assert not report["eligible_temp"]
    assert any(item["path"] == str(link) for item in report["blocked_temp"])
    assert target.read_text() == "keep"
    link.unlink()
    candidate = temp_dir / ("e" * 24 + ".part")
    candidate.write_text("stale")
    os.utime(candidate, (1_700_000_000, 1_700_000_000))
    temp_dir.chmod(0o755)
    report = gc.preview(db, bundle_root, now=1_800_000_000)
    assert not report["eligible_temp"]
    assert any(item["reason"] == "bundle_tmp_not_private_owned_directory" for item in report["blocked_temp"])
    assert candidate.exists()


def _put_event(db, job_id, ts="2020-01-01T00:00:00Z", detail='{"private":"payload"}'):
    cur = db.execute("INSERT INTO events(ts,job_id,kind,job_version,actor,detail_json) VALUES(?,?, 'job_changed',3,'alice',?)",
                     (ts, job_id, detail))
    return cur.lastrowid


def test_old_terminal_event_detail_is_redacted_but_identity_is_retained(db, tmp_path, artifact_root):
    jid, _path = put_artifact_job(db, artifact_root)
    event_id = _put_event(db, jid)
    recent_id = _put_event(db, jid, ts="2026-12-01T00:00:00Z", detail='{"recent":"keep"}')
    global_id = _put_event(db, None)
    db.commit()
    now = 1_800_000_000
    preview = gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)
    assert [event["event_id"] for event in preview["eligible_events"]] == [event_id]
    db.execute("BEGIN IMMEDIATE")
    applied = gc.apply(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)
    db.commit()
    assert [event["event_id"] for event in applied["redacted_events"]] == [event_id]
    row = db.execute("SELECT id,ts,job_id,kind,job_version,actor,detail_json FROM events WHERE id=?", (event_id,)).fetchone()
    assert tuple(row) == (event_id, "2020-01-01T00:00:00Z", jid, "job_changed", 3, "alice", "{}")
    assert db.execute("SELECT detail_json FROM events WHERE id=?", (recent_id,)).fetchone()[0] == '{"recent":"keep"}'
    assert db.execute("SELECT detail_json FROM events WHERE id=?", (global_id,)).fetchone()[0] != "{}"


def test_event_detail_compaction_skips_uncertain_attempts_and_rechecks_dependents(db, tmp_path, artifact_root):
    jid, _path = put_artifact_job(db, artifact_root)
    uncertain_id = _put_event(db, jid)
    db.execute("UPDATE attempts SET state='SUBMITTING',remote_may_be_live=1 WHERE job_id=?", (jid,))
    db.commit()
    now = 1_800_000_000
    report = gc.preview(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)
    assert not report["eligible_events"]
    assert any(event.get("event_id") == uncertain_id and event["reason"] == "attempt_live_or_unreleased"
               for event in report["blocked_events"])

    db.execute("UPDATE attempts SET state='RELEASED',remote_may_be_live=0 WHERE job_id=?", (jid,))
    event_id = _put_event(db, jid, ts="2020-01-02T00:00:00Z")
    assert any(event["event_id"] == event_id for event in gc.preview(
        db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)["eligible_events"])
    child = db.execute("INSERT INTO jobs(owner,token_id,name,spec_json,spec_digest,desired_state,phase,submitted_at,updated_at)"
                       " VALUES('alice','tok','child','{}','child','RUN','PENDING','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')").lastrowid
    db.execute("INSERT INTO deps(job_id,parent_id,type) VALUES(?,?,'after')", (child, jid))
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    applied = gc.apply(db, tmp_path / "bundles", artifact_dir=artifact_root, now=now)
    db.commit()
    assert not any(event["event_id"] == event_id for event in applied["redacted_events"])
    assert any(event.get("event_id") == event_id and event["reason"] == "active_dependent"
               for event in applied["blocked_events"])
    assert db.execute("SELECT detail_json FROM events WHERE id=?", (event_id,)).fetchone()[0] != "{}"
