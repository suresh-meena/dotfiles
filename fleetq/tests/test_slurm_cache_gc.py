"""Fenced, operator-driven retention for shared Slurm bundle caches."""

import hashlib
import json
import os
import subprocess
import time

from fleetq.slurm.render import CACHE_GC_SCRIPT, CACHE_PIN_RELEASE_SCRIPT

GRACE = 7 * 24 * 60 * 60
FLEET = "fleet-a"
EPOCH = "4"


def setup_root(tmp_path):
    root = tmp_path / "control"
    (root / "attempts").mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "fence").write_text(f"{FLEET} {EPOCH}\n")
    (root / "controller.lock").touch()
    root.chmod(0o700)
    (root / "attempts").chmod(0o700)
    (root / "cache").chmod(0o700)
    (root / "fence").chmod(0o600)
    (root / "controller.lock").chmod(0o600)
    return root


def attempt(root, number, digest, *, released=None):
    aid = f"att_{number:024x}"
    directory = root / "attempts" / aid
    directory.mkdir(mode=0o700)
    data = {"attempt_id": aid, "bundle_digest": f"sha256:{digest}" if digest else None}
    (directory / "manifest.json").write_text(json.dumps(data, sort_keys=True) + "\n")
    (directory / "manifest.json").chmod(0o600)
    digest_bytes = (directory / "manifest.json").read_bytes()
    (directory / "manifest.sha256").write_text(hashlib.sha256(digest_bytes).hexdigest() + "\n")
    (directory / "manifest.sha256").chmod(0o600)
    if released is not None:
        (directory / "cache.released").write_text(f"{released}\n")
        (directory / "cache.released").chmod(0o600)
    return aid


def cache_file(root, digest, *, age=GRACE + 10):
    path = root / "cache" / f"{digest}.tar.gz"
    path.write_bytes(b"bundle")
    path.chmod(0o600)
    timestamp = int(time.time()) - age
    os.utime(path, (timestamp, timestamp))
    return path


def invoke(script, root, *args, epoch=EPOCH):
    proc = subprocess.run(["bash", "-c", script, "fq-test", str(root), FLEET, epoch, *args],
                          text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)


def test_inspect_pins_unreleased_and_reports_old_released_digest(tmp_path):
    root = setup_root(tmp_path)
    old = "a" * 64
    live = "b" * 64
    attempt(root, 1, old, released=int(time.time()) - GRACE - 20)
    attempt(root, 2, live)
    cache_file(root, old)
    cache_file(root, live)
    # Active transfer directories are ignored and retained, never swept.
    stage = root / "cache" / ".stage-inflight"
    stage.mkdir()
    (stage / "partial").write_bytes(b"in-flight")

    result = invoke(CACHE_GC_SCRIPT, root, "inspect", "")

    assert result["ok"] is True
    assert result["eligible"] == [f"sha256:{old}"]
    assert result["blocked"] == []
    assert stage.exists() and (stage / "partial").exists()


def test_inspect_fails_closed_on_corrupt_attempt_reference(tmp_path):
    root = setup_root(tmp_path)
    digest = "c" * 64
    cache_file(root, digest)
    bad = root / "attempts" / "att_000000000000000000000001"
    bad.mkdir(mode=0o700)
    (bad / "manifest.json").write_text("not json\n")
    (bad / "manifest.json").chmod(0o600)
    (bad / "manifest.sha256").write_text(hashlib.sha256(b"not json\n").hexdigest() + "\n")
    (bad / "manifest.sha256").chmod(0o600)

    result = invoke(CACHE_GC_SCRIPT, root, "inspect", "")

    assert result["ok"] is True
    assert result["eligible"] == []
    assert "attempt_manifest_invalid" in result["blocked"]


def test_release_marker_requires_exact_fence_and_is_idempotent(tmp_path):
    root = setup_root(tmp_path)
    digest = "d" * 64
    aid = attempt(root, 3, digest)

    released = invoke(CACHE_PIN_RELEASE_SCRIPT, root, aid)
    assert released["ok"] is True and released["released"] is True
    marker = root / "attempts" / aid / "cache.released"
    first = marker.read_text()
    again = invoke(CACHE_PIN_RELEASE_SCRIPT, root, aid)
    assert again["already"] is True and marker.read_text() == first
    rejected = invoke(CACHE_PIN_RELEASE_SCRIPT, root, aid, epoch=str(int(EPOCH) + 1))
    assert rejected == {"ok": False, "reason": "stale_or_unfenced_epoch"}
    assert marker.read_text() == first and first.strip().isdigit()


def test_release_marker_refuses_live_or_uncertain_submission(tmp_path):
    root = setup_root(tmp_path)
    digest = "2" * 64
    aid = attempt(root, 8, digest)
    adir = root / "attempts" / aid
    (adir / "submit.claim").mkdir(mode=0o700)
    (adir / "receipt").write_text("12345\n")
    (adir / "receipt").chmod(0o600)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    squeue = fakebin / "squeue"
    squeue.write_text("#!/bin/sh\nprintf '12345\\n'\n")
    squeue.chmod(0o700)
    env = {**os.environ, "PATH": str(fakebin) + ":" + os.environ["PATH"]}
    proc = subprocess.run(["bash", "-c", CACHE_PIN_RELEASE_SCRIPT, "fq-test", str(root), FLEET, EPOCH, aid],
                          text=True, capture_output=True, check=True, env=env)
    result = json.loads(proc.stdout)
    assert result == {"ok": False, "reason": "slurm_job_still_visible"}
    assert not (adir / "cache.released").exists()

    # A claim without a receipt is ambiguous even when queue lookup could not
    # find a job; absence is not proof that sbatch did not accept it.
    (adir / "receipt").unlink()
    proc = subprocess.run(["bash", "-c", CACHE_PIN_RELEASE_SCRIPT, "fq-test", str(root), FLEET, EPOCH, aid],
                          text=True, capture_output=True, check=True, env=env)
    assert json.loads(proc.stdout) == {"ok": False, "reason": "submission_unresolved"}


def test_release_marker_accepts_exact_id_missing_from_scheduler_queue(tmp_path):
    root = setup_root(tmp_path)
    digest = "4" * 64
    aid = attempt(root, 10, digest)
    adir = root / "attempts" / aid
    (adir / "submit.claim").mkdir(mode=0o700)
    (adir / "receipt").write_text("12346\n")
    (adir / "receipt").chmod(0o600)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    squeue = fakebin / "squeue"
    squeue.write_text("#!/bin/sh\necho 'slurm_load_jobs error: Invalid job id specified'\nexit 1\n")
    squeue.chmod(0o700)
    env = {**os.environ, "PATH": str(fakebin) + ":" + os.environ["PATH"]}

    proc = subprocess.run(["bash", "-c", CACHE_PIN_RELEASE_SCRIPT, "fq-test", str(root), FLEET, EPOCH, aid],
                          text=True, capture_output=True, check=True, env=env)

    result = json.loads(proc.stdout)
    assert result["ok"] is True and result["released"] is True


def test_gc_blocks_claim_without_receipt_even_if_release_marker_exists(tmp_path):
    root = setup_root(tmp_path)
    digest = "3" * 64
    aid = attempt(root, 9, digest, released=int(time.time()) - GRACE - 20)
    adir = root / "attempts" / aid
    (adir / "submit.claim").mkdir(mode=0o700)
    cache_file(root, digest)

    result = invoke(CACHE_GC_SCRIPT, root, "inspect", "")

    assert result["eligible"] == []
    assert "submission_unresolved" in result["blocked"]


def test_purge_rechecks_current_references_under_lock(tmp_path):
    root = setup_root(tmp_path)
    digest = "e" * 64
    attempt(root, 4, digest, released=int(time.time()) - GRACE - 20)
    path = cache_file(root, digest)
    inspected = invoke(CACHE_GC_SCRIPT, root, "inspect", "")
    assert inspected["eligible"] == [f"sha256:{digest}"]

    # A newly staged attempt published after preview becomes an active pin.
    attempt(root, 5, digest)
    purged = invoke(CACHE_GC_SCRIPT, root, "purge", f"sha256:{digest}")
    assert purged["ok"] is False
    assert purged["reason"] == "not_currently_eligible"
    assert path.exists()


def test_exact_fence_required_and_only_canonical_owned_files_are_purged(tmp_path):
    root = setup_root(tmp_path)
    digest = "f" * 64
    attempt(root, 6, digest, released=int(time.time()) - GRACE - 20)
    path = cache_file(root, digest)

    stale = invoke(CACHE_GC_SCRIPT, root, "purge", f"sha256:{digest}", epoch=str(int(EPOCH) + 1))
    assert stale["ok"] is False and stale["reason"] == "stale_or_unfenced_epoch"
    assert path.exists()
    # Correct epoch removes only the reviewed canonical digest.
    (root / "fence").write_text(f"{FLEET} {EPOCH}\n")
    result = invoke(CACHE_GC_SCRIPT, root, "purge", f"sha256:{digest}")
    assert result["ok"] is True and result["removed"] == [f"sha256:{digest}"]
    assert not path.exists()


def test_young_released_cache_remains_ineligible(tmp_path):
    root = setup_root(tmp_path)
    digest = "1" * 64
    attempt(root, 7, digest, released=1_800_000_000 - 20)
    cache_file(root, digest, age=20)

    result = invoke(CACHE_GC_SCRIPT, root, "inspect", "")

    assert result["eligible"] == []
