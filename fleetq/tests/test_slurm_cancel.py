import json
import os
import subprocess
from pathlib import Path

import pytest

from fleetq.slurm.render import (CANCEL_SCRIPT, COLLECT_CLEAN_SCRIPT, COLLECT_STAGE_SCRIPT, CONTROL_ROOT_CHECK_SCRIPT,
                                FENCE_SCRIPT, STAGE_WRAPPER,
                                SUBMIT_WRAPPER)


def secure_control_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "attempts").mkdir(exist_ok=True)
    (root / "cache").mkdir(exist_ok=True)
    (root / "attempts").chmod(0o700)
    (root / "cache").chmod(0o700)
    root.chmod(0o700)


def run_cancel(tmp_path: Path, epoch: int, *, fence: str = "fleet-a 2\n", job_id: str = "123",
               sync_fails: bool = False) -> dict:
    root = tmp_path / "control"
    secure_control_root(root)
    (root / "fence").write_text(fence)
    (root / "fence").chmod(0o600)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    called = tmp_path / "scancel-called"
    scancel = bindir / "scancel"
    scancel.write_text(f"#!/bin/sh\nprintf '%s' \"$1\" > {called}\n")
    scancel.chmod(0o755)
    if sync_fails:
        sync = bindir / "sync"
        sync.write_text("#!/bin/sh\nexit 1\n")
        sync.chmod(0o755)
    attempt = "att_0123456789abcdef01234567"
    proc = subprocess.run(
        ["bash", "-c", CANCEL_SCRIPT, "fq-cancel", str(root), "fleet-a", str(epoch), attempt, job_id],
        text=True, capture_output=True, check=True, env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    return json.loads(proc.stdout), root, called, attempt


def test_stale_slurm_cancel_does_not_tombstone_or_scancel(tmp_path):
    result, root, called, attempt = run_cancel(tmp_path, 1)
    assert result["reason"] == "stale_epoch"
    assert not (root / "attempts" / attempt / "cancel").exists()
    assert not called.exists()


def test_future_epoch_cannot_mutate_before_fence(tmp_path):
    result, root, called, attempt = run_cancel(tmp_path, 3)
    assert result["reason"] == "unfenced_epoch"
    assert not called.exists() and not (root / "attempts" / attempt / "cancel").exists()
    submit = subprocess.run(
        ["bash", "-c", SUBMIT_WRAPPER, "fq-submit", str(root), "fleet-a", "3", attempt],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(submit.stdout) == {"result": "never_started", "reason": "unfenced_epoch"}
    assert not (root / "attempts" / attempt / "submit.claim").exists()


def test_submit_refuses_when_slurm_controller_is_unavailable(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    (root / "fence").write_text("fleet-a 2\n")
    (root / "fence").chmod(0o600)
    attempt = "att_0123456789abcdef01234567"
    adir = root / "attempts" / attempt
    adir.mkdir(parents=True)
    (adir / "batch.sh").write_text("#!/bin/bash\ntrue\n")
    (adir / "stage.ready").touch()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    called = tmp_path / "sbatch-called"
    sbatch = bindir / "sbatch"
    sbatch.write_text(f"#!/bin/sh\ntouch {called}\necho 123\n")
    sbatch.chmod(0o755)

    proc = subprocess.run(
        ["bash", "-c", SUBMIT_WRAPPER, "fq-submit", str(root), "fleet-a", "2", attempt],
        text=True, capture_output=True, check=True,
        env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"},
    )

    assert json.loads(proc.stdout) == {"result": "never_started", "reason": "slurm_unavailable"}
    assert not called.exists()
    assert not (adir / "submit.claim").exists()


def test_submit_rejects_path_shaped_attempt_id(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    (root / "fence").write_text("fleet-a 2\n")
    (root / "fence").chmod(0o600)
    proc = subprocess.run(
        ["bash", "-c", SUBMIT_WRAPPER, "fq-submit", str(root), "fleet-a", "2", "att_../../outside"],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(proc.stdout) == {"result": "never_started", "reason": "invalid_attempt_id"}
    assert not (tmp_path / "outside" / "submit.claim").exists()


def test_stage_wrapper_rejects_path_shaped_ids_before_mutation(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    proc = subprocess.run(
        ["bash", "-c", STAGE_WRAPPER, "fq-stage", str(root), "fleet-a", "2",
         "att_../../outside", "0123456789abcdef0123456789abcdef"],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(proc.stdout) == {"ok": False, "reason": "invalid_attempt_id"}
    assert not (tmp_path / "outside").exists()


def test_stage_wrapper_refuses_symlinked_stage_source(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    (root / "fence").write_text("fleet-a 2\n")
    (root / "fence").chmod(0o600)
    outside = tmp_path / "other-stage"
    (outside / "attempt").mkdir(parents=True)
    marker = outside / "attempt" / "batch.sh"
    marker.write_text("keep\n")
    stage = "0123456789abcdef0123456789abcdef"
    (root / "cache" / f".stage-{stage}").symlink_to(outside, target_is_directory=True)

    proc = subprocess.run(
        ["bash", "-c", STAGE_WRAPPER, "fq-stage", str(root), "fleet-a", "2",
         "att_0123456789abcdef01234567", stage],
        text=True, capture_output=True, check=True,
    )

    assert json.loads(proc.stdout) == {"ok": False, "reason": "stage_missing"}
    assert marker.read_text() == "keep\n"
    assert not (root / "attempts" / "att_0123456789abcdef01234567" / "batch.sh").exists()


def test_collect_clean_rejects_path_shaped_attempt_id(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    outside = tmp_path / "outside" / "outbox"
    outside.mkdir(parents=True)
    marker = outside / "keep"
    marker.write_text("safe")
    root_secure = root / "fence"
    root_secure.write_text("fleet-a 2\n")
    root_secure.chmod(0o600)
    proc = subprocess.run(
        ["bash", "-c", COLLECT_CLEAN_SCRIPT, "fq-clean", str(root), "../../outside"],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(proc.stdout) == {"cleaned": False, "reason": "invalid_attempt_id"}
    assert marker.read_text() == "safe"


def test_collect_clean_rejects_symlink_attempt_directory(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    outside = tmp_path / "outside"
    outbox = outside / "outbox"
    outbox.mkdir(parents=True)
    marker = outbox / "keep"
    marker.write_text("safe")
    att = "att_0123456789abcdef01234567"
    (root / "attempts" / att).symlink_to(outside, target_is_directory=True)

    proc = subprocess.run(
        ["bash", "-c", COLLECT_CLEAN_SCRIPT, "fq-clean", str(root), att],
        text=True, capture_output=True, check=True,
    )

    assert json.loads(proc.stdout) == {"cleaned": False, "reason": "unsafe_attempt_dir"}
    assert marker.read_text() == "safe"


def test_collect_stage_refuses_symlink_attempt_directory_before_cleanup(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    att = "att_0123456789abcdef01234567"
    (root / "attempts" / att).symlink_to(outside, target_is_directory=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc = subprocess.run(
        ["bash", "-c", COLLECT_STAGE_SCRIPT, "fq-stage", str(root), att,
         str(workdir), "10", "100"],
        text=True, capture_output=True, check=True,
    )

    assert proc.stdout.strip() == "E unsafe_attempt_dir"
    assert list(outside.iterdir()) == []


def test_collect_stage_refuses_symlink_outbox_before_publish(tmp_path):
    root = tmp_path / "control"
    secure_control_root(root)
    att = "att_0123456789abcdef01234567"
    adir = root / "attempts" / att
    adir.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("safe")
    (adir / "outbox").symlink_to(outside, target_is_directory=True)
    workdir = tmp_path / "work"
    workdir.mkdir()

    proc = subprocess.run(
        ["bash", "-c", COLLECT_STAGE_SCRIPT, "fq-stage", str(root), att,
         str(workdir), "10", "100"],
        text=True, capture_output=True, check=True,
    )

    assert proc.stdout.strip() == "E unsafe_outbox"
    assert marker.read_text() == "safe"
    assert not (outside / ".outbox").exists()


def test_cancel_refuses_home_as_control_root_before_creating_files(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run(
        ["bash", "-c", CANCEL_SCRIPT, "fq-cancel", str(home), "fleet-a", "2",
         "att_0123456789abcdef01234567", "123"],
        text=True, capture_output=True, check=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert json.loads(proc.stdout) == {"tombstone": False, "reason": "unsafe_control_root"}
    assert list(home.iterdir()) == []


def test_collect_clean_refuses_symlink_control_root(tmp_path):
    real = tmp_path / "real-control"
    outbox = real / "attempts" / "att_0123456789abcdef01234567" / "outbox"
    outbox.mkdir(parents=True)
    marker = outbox / "keep"
    marker.write_text("safe")
    alias = tmp_path / "control-alias"
    alias.symlink_to(real, target_is_directory=True)
    proc = subprocess.run(
        ["bash", "-c", COLLECT_CLEAN_SCRIPT, "fq-clean", str(alias), "att_0123456789abcdef01234567"],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(proc.stdout) == {"cleaned": False, "reason": "unsafe_control_root"}
    assert marker.read_text() == "safe"


def test_dedicated_home_subdirectory_is_an_eligible_control_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "fleetq-control"
    secure_control_root(root)
    proc = subprocess.run(
        ["sh", "-c", CONTROL_ROOT_CHECK_SCRIPT, "fq-root-check", str(root)],
        text=True, capture_output=True, check=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert json.loads(proc.stdout) == {"ok": True}


def test_root_guard_refuses_root_and_home_ancestor(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for root in ("/", str(tmp_path)):
        proc = subprocess.run(
            ["sh", "-c", CONTROL_ROOT_CHECK_SCRIPT, "fq-root-check", root],
            text=True, capture_output=True, check=True,
            env={**os.environ, "HOME": str(home)},
        )
        assert json.loads(proc.stdout) == {"ok": False}


def test_root_precheck_refuses_unrelated_layout_without_touching_sentinel(tmp_path):
    home = tmp_path / "home"
    unrelated = home / ".ssh" / "cache"
    unrelated.mkdir(parents=True)
    sentinel = unrelated / "authorized_keys"
    sentinel.write_text("keep me\n")
    proc = subprocess.run(
        ["sh", "-c", CONTROL_ROOT_CHECK_SCRIPT, "fq-root-check", str(unrelated)],
        text=True, capture_output=True, check=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert json.loads(proc.stdout) == {"ok": False}
    assert sentinel.read_text() == "keep me\n"
    assert sorted(p.name for p in unrelated.iterdir()) == ["authorized_keys"]


@pytest.mark.parametrize("component", [".ssh", ".gnupg", ".config", ".local", ".cache"])
def test_important_home_directory_components_are_never_control_roots(tmp_path, component):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = home / component
    root.mkdir(mode=0o700)
    env = {**os.environ, "HOME": str(home)}

    check = subprocess.run(
        ["sh", "-c", CONTROL_ROOT_CHECK_SCRIPT, "fq-root-check", str(root)],
        text=True, capture_output=True, check=True, env=env,
    )
    fence = subprocess.run(
        ["bash", "-c", FENCE_SCRIPT, "fq-fence", str(root), "fleet-a", "1"],
        text=True, capture_output=True, check=True, env=env,
    )

    assert json.loads(check.stdout) == {"ok": False}
    assert json.loads(fence.stdout) == {"accepted": False, "reason": "unsafe_control_root"}
    assert list(root.iterdir()) == []


def test_protected_component_is_rejected_even_below_dedicated_home_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = home / ".config" / "fleetq-control"
    root.mkdir(parents=True, mode=0o700)
    proc = subprocess.run(
        ["sh", "-c", CONTROL_ROOT_CHECK_SCRIPT, "fq-root-check", str(root)],
        text=True, capture_output=True, check=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert json.loads(proc.stdout) == {"ok": False}
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("root", ["/bin/fleetq", "/sbin/fleetq", "/lib/fleetq",
                                  "/lib64/fleetq", "/opt/fleetq", "/run/fleetq"])
def test_system_directories_are_never_slurm_control_roots(root):
    proc = subprocess.run(
        ["sh", "-c", CONTROL_ROOT_CHECK_SCRIPT, "fq-root-check", root],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(proc.stdout) == {"ok": False}


@pytest.mark.parametrize("precreate", [False, True])
def test_fence_initializes_or_upgrades_only_private_control_layout(tmp_path, precreate):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = home / "fleetq-control"
    if precreate:
        root.mkdir(mode=0o755)
    proc = subprocess.run(
        ["bash", "-c", FENCE_SCRIPT, "fq-fence", str(root), "fleet-a", "1"],
        text=True, capture_output=True, check=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert json.loads(proc.stdout)["accepted"] is True
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "attempts").stat().st_mode & 0o777 == 0o700
    assert (root / "cache").stat().st_mode & 0o777 == 0o700
    assert (root / "fence").stat().st_mode & 0o777 == 0o600
    assert json.loads(subprocess.run(
        ["sh", "-c", CONTROL_ROOT_CHECK_SCRIPT, "fq-check", str(root)],
        text=True, capture_output=True, check=True,
        env={**os.environ, "HOME": str(home)},
    ).stdout) == {"ok": True}


def test_current_slurm_cancel_tombstones_and_scancels_exact_id(tmp_path):
    result, root, called, attempt = run_cancel(tmp_path, 2)
    assert result["tombstone"] is True and result["scancel_rc"] == 0
    assert (root / "attempts" / attempt / "cancel").exists()
    assert called.read_text() == "123"


def test_corrupt_fence_blocks_every_remote_mutation(tmp_path):
    result, root, called, attempt = run_cancel(tmp_path, 2, fence="fleet-a broken\n")
    assert result["reason"] == "invalid_fence"
    assert not called.exists() and not (root / "attempts" / attempt / "cancel").exists()

    submit = subprocess.run(
        ["bash", "-c", SUBMIT_WRAPPER, "fq-submit", str(root), "fleet-a", "2", attempt],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(submit.stdout) == {"result": "never_started", "reason": "invalid_fence"}
    assert not (root / "attempts" / attempt / "submit.claim").exists()

    fence = subprocess.run(
        ["bash", "-c", FENCE_SCRIPT, "fq-fence", str(root), "fleet-a", "3"],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(fence.stdout)["reason"] == "invalid_fence"
    assert (root / "fence").read_text() == "fleet-a broken\n"


def test_invalid_slurm_id_writes_tombstone_without_scancel(tmp_path):
    result, root, called, attempt = run_cancel(tmp_path, 2, job_id="--clusters=elsewhere")
    assert result["reason"] == "invalid_id" and result["tombstone"] is True
    assert (root / "attempts" / attempt / "cancel").exists()
    assert not called.exists()


def test_cancel_never_scancels_without_durable_tombstone(tmp_path):
    result, root, called, attempt = run_cancel(tmp_path, 2, sync_fails=True)
    assert result["reason"] == "tombstone_failed"
    assert not (root / "attempts" / attempt / "cancel").exists()
    assert not called.exists()
