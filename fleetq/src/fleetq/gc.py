"""Conservative local GC for fleetqd-owned bundles and collected artifacts.

Artifact expiry is deliberately two-pass: first persist EXPIRED state and its
job event; a later apply removes only the now-unavailable artifact files.
"""
from __future__ import annotations

import datetime
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any
from .engine import state

BUNDLE_GRACE_SECONDS = 7 * 24 * 60 * 60
ARTIFACT_RETENTION_SECONDS = 180 * 24 * 60 * 60
EVENT_DETAIL_RETENTION_SECONDS = 90 * 24 * 60 * 60
PART_NAME = re.compile(r"[0-9a-f]{24}\.part\Z")


def _safe_root(root: Path) -> bool:
    try:
        st = root.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not (st.st_mode & 0o077))


def _candidate_rows(conn: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT digest,path,compressed_bytes,created_at FROM bundles ORDER BY digest").fetchall()
    result = []
    for row in rows:
        # Any ref row is a pin, including released refs: release/retention semantics
        # are not sufficiently complete to infer that old job or upload history is disposable.
        if conn.execute("SELECT 1 FROM bundle_refs WHERE digest=? LIMIT 1", (row["digest"],)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM jobs WHERE bundle_digest=? LIMIT 1", (row["digest"],)).fetchone():
            continue
        try:
            age = now - datetime.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, OverflowError):
            continue
        if age < BUNDLE_GRACE_SECONDS:
            continue
        result.append({"digest": row["digest"], "path": row["path"], "bytes": row["compressed_bytes"], "age_seconds": int(age)})
    return result


def _part_files(bundle_dir: Path, now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find only aged generated upload parts in the private bundle tmp directory."""
    tmp_dir = bundle_dir / "tmp"
    try:
        tmp_dir.lstat()
    except FileNotFoundError:
        return [], []
    if not _safe_root(bundle_dir) or not _safe_root(tmp_dir) or tmp_dir.resolve().parent != bundle_dir.resolve():
        return [], [{"path": str(tmp_dir), "reason": "bundle_tmp_not_private_owned_directory"}]
    candidates, blocked = [], []
    try:
        entries = list(tmp_dir.iterdir())
    except OSError as exc:
        return [], [{"path": str(tmp_dir), "reason": str(exc)}]
    for path in entries:
        if not PART_NAME.fullmatch(path.name):
            continue
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise ValueError("bundle temp is not an owned regular file")
            age = now - st.st_mtime
            if age < BUNDLE_GRACE_SECONDS:
                continue
            candidates.append({"path": str(path), "bytes": st.st_size, "age_seconds": int(age)})
        except (OSError, ValueError) as exc:
            blocked.append({"path": str(path), "reason": str(exc)})
    return candidates, blocked


def _age_seconds(value: str | None, now: float) -> int | None:
    try:
        return int(now - datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (AttributeError, ValueError, TypeError, OverflowError):
        return None


def _safe_artifact_root(root: Path) -> bool:
    """The state directory is private; the artifacts child must be owned and non-writable by others."""
    try:
        parent = root.parent.lstat()
        st = root.lstat()
        return (_safe_root(root.parent) and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
                and not (st.st_mode & 0o022) and root.resolve().parent == root.parent.resolve()
                and stat.S_ISDIR(parent.st_mode))
    except OSError:
        return False


def _safe_owned_dir(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not (st.st_mode & 0o022)


def _artifact_group_paths(root: Path, job_id: int, rows: list[sqlite3.Row], *, allow_missing: bool = False) -> tuple[list[Path] | None, str | None]:
    """Validate every expected file and reject symlinks, escapes, and unmanifested files."""
    expected_by_dir: dict[Path, set[Path]] = {}
    dirs: set[Path] = set()
    for row in rows:
        # SQLite's INTEGER affinity and CHECK constraints do not guarantee that
        # a corrupt row contains an integer: nonnumeric text can satisfy n>=1.
        # Treat the attempt number as a single canonical positive path segment.
        attempt_number = row["n"]
        if (not isinstance(attempt_number, int) or isinstance(attempt_number, bool)
                or attempt_number < 1):
            return None, "unsafe_artifact_attempt_number"
        attempt_dir = root / str(job_id) / str(row["n"])
        rel = row["relpath"]
        norm = posixpath.normpath(rel)
        if (not rel or rel.startswith("/") or "\x00" in rel or norm in ("", ".", "..")
                or norm.startswith("../") or norm != rel):
            return None, "unsafe_artifact_relpath"
        if row["local_path"] != str(attempt_dir / rel):
            return None, "artifact_path_mismatch"
        path = attempt_dir / rel
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current = current / part
            if not _safe_owned_dir(current):
                if allow_missing and not current.exists() and not current.is_symlink():
                    continue
                return None, "artifact_directory_missing_or_unsafe"
        try:
            st = path.lstat()
        except OSError:
            if allow_missing and not path.exists() and not path.is_symlink():
                dirs.add(attempt_dir)
                continue
            return None, "artifact_file_missing"
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size != row["size"]:
            return None, "artifact_file_missing_or_unsafe"
        expected_by_dir.setdefault(attempt_dir, set()).add(path)
        dirs.add(attempt_dir)

    for attempt_dir, expected in expected_by_dir.items():
        if allow_missing and not attempt_dir.exists() and not attempt_dir.is_symlink():
            continue
        found: set[Path] = set()
        for current, dirnames, filenames in os.walk(attempt_dir, followlinks=False):
            base = Path(current)
            for name in list(dirnames):
                child = base / name
                if not _safe_owned_dir(child):
                    return None, "unsafe_artifact_directory"
            for name in filenames:
                path = base / name
                try:
                    st = path.lstat()
                except OSError:
                    return None, "artifact_file_missing"
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                    return None, "unsafe_artifact_file"
                found.add(path)
        if (not found.issubset(expected) if allow_missing else found != expected):
            return None, "unmanifested_artifact_file"
    return sorted(dirs), None


def _artifact_candidate(conn: sqlite3.Connection, job_id: int, artifact_root: Path, now: float,
                        *, expired: bool) -> tuple[dict[str, Any] | None, str | None]:
    job = conn.execute("SELECT id, phase, artifacts_state, ended_at, execution_outcome, success FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    if job is None or job["phase"] != "TERMINAL" or job["artifacts_state"] != ("EXPIRED" if expired else "COMPLETE"):
        return None, "job_no_longer_eligible"
    age = _age_seconds(job["ended_at"], now)
    if age is None or age < ARTIFACT_RETENTION_SECONDS:
        return None, "job_not_old_enough"
    if conn.execute("SELECT 1 FROM attempts WHERE job_id=? AND (remote_may_be_live=1 OR"
                    " state NOT IN ('REFUSED','NEVER_STARTED','RELEASED')) LIMIT 1", (job_id,)).fetchone():
        return None, "attempt_live_or_unreleased"
    if conn.execute("SELECT 1 FROM deps d JOIN jobs child ON child.id=d.job_id"
                    " WHERE d.parent_id=? AND child.phase<>'TERMINAL' LIMIT 1", (job_id,)).fetchone():
        return None, "active_dependent"
    rows = conn.execute("SELECT a.*, t.n, t.job_id AS attempt_job_id FROM artifacts a LEFT JOIN attempts t ON t.id=a.attempt_id"
                        " WHERE a.job_id=? ORDER BY t.n,a.relpath", (job_id,)).fetchall()
    if not rows or any(row["attempt_job_id"] != job_id or row["state"] != ("EXPIRED" if expired else "COMPLETE") for row in rows):
        return None, "artifacts_not_complete"
    if any(row["local_path"] is None for row in rows):
        return None, "artifact_path_missing"
    if not _safe_artifact_root(artifact_root):
        return None, "artifact_root_not_private_owned_directory"
    dirs, error = _artifact_group_paths(artifact_root, job_id, rows, allow_missing=expired)
    if error:
        return None, error
    return {"job_id": job_id, "bytes": sum(int(row["size"] or 0) for row in rows),
            "files": len(rows), "age_seconds": age, "paths": [str(path) for path in dirs],
            "execution_outcome": job["execution_outcome"], "success": job["success"]}, None


def _artifact_jobs(conn: sqlite3.Connection, artifact_root: Path, now: float, *, expired: bool):
    wanted = "EXPIRED" if expired else "COMPLETE"
    ids = conn.execute("SELECT id FROM jobs WHERE phase='TERMINAL' AND artifacts_state=? ORDER BY id", (wanted,))
    candidates, blocked = [], []
    for row in ids:
        candidate, reason = _artifact_candidate(conn, row["id"], artifact_root, now, expired=expired)
        if candidate:
            candidates.append(candidate)
        elif reason not in ("job_not_old_enough", "artifacts_not_complete", "artifact_path_missing"):
            blocked.append({"job_id": row["id"], "reason": reason})
    return candidates, blocked


def _event_candidate(conn: sqlite3.Connection, event_id: int, now: float):
    event = conn.execute("SELECT id,ts,job_id,kind,job_version,detail_json FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None or event["detail_json"] == "{}":
        return None, "event_detail_already_compacted"
    age = _age_seconds(event["ts"], now)
    if age is None or age < EVENT_DETAIL_RETENTION_SECONDS:
        return None, "event_not_old_enough"
    job_id = event["job_id"]
    job = conn.execute("SELECT phase FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id is not None else None
    if job is None or job["phase"] != "TERMINAL":
        return None, "parent_not_terminal"
    if conn.execute("SELECT 1 FROM attempts WHERE job_id=? AND (remote_may_be_live=1 OR"
                    " state NOT IN ('REFUSED','NEVER_STARTED','RELEASED')) LIMIT 1", (job_id,)).fetchone():
        return None, "attempt_live_or_unreleased"
    if conn.execute("SELECT 1 FROM deps d JOIN jobs child ON child.id=d.job_id"
                    " WHERE d.parent_id=? AND child.phase<>'TERMINAL' LIMIT 1", (job_id,)).fetchone():
        return None, "active_dependent"
    return {"event_id": event["id"], "job_id": job_id, "ts": event["ts"], "kind": event["kind"],
            "job_version": event["job_version"], "age_seconds": age}, None


def _event_details(conn: sqlite3.Connection, now: float):
    eligible, blocked = [], []
    # Global controller events have no terminal job to prove safe for redaction.
    for row in conn.execute("SELECT id,ts FROM events WHERE job_id IS NOT NULL AND detail_json<>'{}' ORDER BY id"):
        age = _age_seconds(row["ts"], now)
        if age is None or age < EVENT_DETAIL_RETENTION_SECONDS:
            continue
        item, reason = _event_candidate(conn, row["id"], now)
        if item:
            eligible.append(item)
        elif reason not in ("event_not_old_enough", "event_detail_already_compacted"):
            blocked.append({"event_id": row["id"], "reason": reason})
    return eligible, blocked


def preview(conn: sqlite3.Connection, bundle_dir: Path, *, artifact_dir: Path | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Return deletions only when the root, DB row, references, and file are provable."""
    now = time.time() if now is None else now
    artifact_dir = artifact_dir or (bundle_dir.parent / "artifacts")
    report: dict[str, Any] = {"schema": "fq.gc/v1", "mode": "preview", "eligible": [],
                              "removed": [], "blocked": [], "eligible_temp": [], "removed_temp": [],
                              "blocked_temp": [], "eligible_events": [], "redacted_events": [],
                              "blocked_events": [], "eligible_artifacts": [],
                              "purgeable_artifacts": [], "expired_artifacts": [],
                              "removed_artifacts": [], "blocked_artifacts": [],
                              "limitations": ["artifact apply first marks COMPLETE artifacts EXPIRED; a later apply purges files",
                                              "event rows and timeline metadata remain; safe event details older than 90 days are compacted",
                                              "bundle references and job/event identity metadata are retained indefinitely",
                                              "only old generated upload .part files are collected; other incomplete files are retained"]}
    if _safe_root(bundle_dir):
        try:
            candidates = _candidate_rows(conn, now)
        except sqlite3.Error as exc:
            report["blocked"].append({"reason": "database_reference_query_failed", "detail": str(exc)})
            candidates = []
        root = bundle_dir.resolve()
        for item in candidates:
            path = Path(item["path"])
            try:
                if path.parent.resolve() != root or path.name != item["digest"].split(":", 1)[1] + ".tar.gz":
                    raise ValueError("path does not match content-addressed bundle location")
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                    raise ValueError("bundle is not an owned regular file")
            except (OSError, ValueError) as exc:
                report["blocked"].append({"digest": item["digest"], "reason": str(exc)})
                continue
            report["eligible"].append(item)
    else:
        report["blocked"].append({"reason": "bundle_root_not_private_owned_directory", "path": str(bundle_dir)})
    report["eligible_temp"], report["blocked_temp"] = _part_files(bundle_dir, now)
    try:
        report["eligible_artifacts"], report["blocked_artifacts"] = _artifact_jobs(conn, artifact_dir, now, expired=False)
        report["purgeable_artifacts"], expired_blocked = _artifact_jobs(conn, artifact_dir, now, expired=True)
        report["blocked_artifacts"].extend(expired_blocked)
    except sqlite3.Error as exc:
        report["blocked_artifacts"].append({"reason": "database_artifact_query_failed", "detail": str(exc)})
    try:
        report["eligible_events"], report["blocked_events"] = _event_details(conn, now)
    except sqlite3.Error as exc:
        report["blocked_events"].append({"reason": "database_event_query_failed", "detail": str(exc)})
    return report


def apply(conn: sqlite3.Connection, bundle_dir: Path, *, artifact_dir: Path | None = None,
          now: float | None = None) -> dict[str, Any]:
    """Apply local GC inside the caller's SQLite owner transaction."""
    now = time.time() if now is None else now
    artifact_dir = artifact_dir or (bundle_dir.parent / "artifacts")
    report = preview(conn, bundle_dir, artifact_dir=artifact_dir, now=now)
    report["mode"] = "apply"
    for item in report["eligible_temp"]:
        path = Path(item["path"])
        current, blocked = _part_files(bundle_dir, now)
        fresh = next((entry for entry in current if entry["path"] == item["path"]), None)
        if fresh is None:
            reason = next((entry["reason"] for entry in blocked if entry["path"] == item["path"]), "eligibility_changed")
            report["blocked_temp"].append({"path": item["path"], "reason": reason})
            continue
        try:
            # lstat again immediately before unlink; never follow or remove a replacement symlink.
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or now - st.st_mtime < BUNDLE_GRACE_SECONDS:
                raise ValueError("bundle temp changed since preview")
            path.unlink()
            report["removed_temp"].append(fresh)
        except (OSError, ValueError) as exc:
            report["blocked_temp"].append({"path": item["path"], "reason": str(exc)})
    for item in report["eligible"]:
        try:
            fresh = preview(conn, bundle_dir, now=now)
            match = next((x for x in fresh["eligible"] if x["digest"] == item["digest"]), None)
            if match is None:
                report["blocked"].append({"digest": item["digest"], "reason": "eligibility_changed"})
                continue
            # Revalidate metadata and references in the transaction before unlink.
            row = conn.execute("SELECT path FROM bundles WHERE digest=?", (item["digest"],)).fetchone()
            if row is None or conn.execute("SELECT 1 FROM bundle_refs WHERE digest=? LIMIT 1", (item["digest"],)).fetchone() or conn.execute(
                    "SELECT 1 FROM jobs WHERE bundle_digest=? LIMIT 1", (item["digest"],)).fetchone():
                report["blocked"].append({"digest": item["digest"], "reason": "reference_appeared"})
                continue
            conn.execute("DELETE FROM bundles WHERE digest=?", (item["digest"],))
            Path(row["path"]).unlink()
            report["removed"].append(item)
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError(f"GC failed for {item['digest']}: {exc}") from exc
    for item in report["eligible_artifacts"]:
        jid = item["job_id"]
        current, why = _artifact_candidate(conn, jid, artifact_dir, now, expired=False)
        if current is None:
            report["blocked_artifacts"].append({"job_id": jid, "reason": why or "eligibility_changed"})
            continue
        state.update_job(conn, jid, event="artifact_retention_expired", actor="gc",
                         detail={"files": current["files"], "bytes": current["bytes"]}, artifacts_state="EXPIRED")
        conn.execute("UPDATE artifacts SET state='EXPIRED', updated_at=? WHERE job_id=? AND state='COMPLETE'",
                     (datetime.datetime.now(datetime.timezone.utc).isoformat(), jid))
        report["expired_artifacts"].append(item)
    for item in report["eligible_events"]:
        fresh, why = _event_candidate(conn, item["event_id"], now)
        if fresh is None:
            report["blocked_events"].append({"event_id": item["event_id"], "reason": why or "eligibility_changed"})
            continue
        changed = conn.execute("UPDATE events SET detail_json='{}' WHERE id=? AND detail_json<>'{}'",
                               (item["event_id"],)).rowcount
        if changed:
            report["redacted_events"].append(item)
        else:
            report["blocked_events"].append({"event_id": item["event_id"], "reason": "event_changed"})
    for item in report["purgeable_artifacts"]:
        jid = item["job_id"]
        current, why = _artifact_candidate(conn, jid, artifact_dir, now, expired=True)
        if current is None:
            report["blocked_artifacts"].append({"job_id": jid, "reason": why or "eligibility_changed"})
            continue
        try:
            for path_s in current["paths"]:
                path = Path(path_s)
                if path.exists():
                    shutil.rmtree(path)
            conn.execute("UPDATE artifacts SET local_path=NULL, size=NULL, updated_at=? WHERE job_id=? AND state='EXPIRED'",
                         (datetime.datetime.now(datetime.timezone.utc).isoformat(), jid))
            report["removed_artifacts"].append(item)
        except OSError as exc:
            report["blocked_artifacts"].append({"job_id": jid, "reason": str(exc)})
    return report
