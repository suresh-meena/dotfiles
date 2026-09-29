"""The remote stage publication boundary is fenced just like submission."""

import subprocess
import json

from fleetq.slurm.render import STAGE_WRAPPER, SUBMIT_WRAPPER


def secure_root(root):
    (root / "attempts").mkdir(parents=True, exist_ok=True)
    (root / "cache").mkdir(exist_ok=True)
    root.chmod(0o700)
    (root / "attempts").chmod(0o700)
    (root / "cache").chmod(0o700)
    for name in ("fence", "controller.lock"):
        path = root / name
        if path.exists():
            path.chmod(0o600)


def test_stale_stage_after_fence_cannot_replace_attempt_or_cache(tmp_path):
    root = tmp_path / "control"
    attempt_id = "att_0123456789abcdef01234567"
    stage_id = "0123456789abcdef0123456789abcdef"
    bundle_sha = "a" * 64
    (root / "attempts" / attempt_id).mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "controller.lock").touch()
    (root / "fence").write_text("fleet-a 2\n")
    (root / "attempts" / attempt_id / "batch.sh").write_text("new batch\n")
    (root / "attempts" / attempt_id / "manifest.json").write_text('{"bundle_digest": "sha256:' + "a" * 64 + '","epoch":2}\n')
    (root / "attempts" / attempt_id / "stage.ready").write_text("ready\n")
    (root / "cache" / f"{bundle_sha}.tar.gz").write_bytes(b"new bundle")
    secure_root(root)

    staged = root / "cache" / f".stage-{stage_id}"
    (staged / "attempt").mkdir(parents=True)
    (staged / "cache").mkdir()
    (staged / "attempt" / "batch.sh").write_text("stale batch\n")
    (staged / "attempt" / "manifest.json").write_text('{"bundle_digest": "sha256:' + bundle_sha + '","epoch":1}\n')
    (staged / "cache" / f"{bundle_sha}.tar.gz").write_bytes(b"stale bundle")

    result = subprocess.run(
        ["bash", "-c", STAGE_WRAPPER, "fleetq", str(root), "fleet-a", "1", attempt_id, stage_id, bundle_sha],
        text=True, capture_output=True, check=True,
    )

    assert '"ok":false' in result.stdout
    assert (root / "attempts" / attempt_id / "batch.sh").read_text() == "new batch\n"
    assert (root / "attempts" / attempt_id / "manifest.json").read_text() == '{"bundle_digest": "sha256:' + "a" * 64 + '","epoch":2}\n'
    assert (root / "attempts" / attempt_id / "stage.ready").read_text() == "ready\n"
    assert (root / "cache" / f"{bundle_sha}.tar.gz").read_bytes() == b"new bundle"


def test_incomplete_stage_cannot_submit(tmp_path):
    root = tmp_path / "control"
    attempt_id = "att_0123456789abcdef01234567"
    attempt = root / "attempts" / attempt_id
    attempt.mkdir(parents=True)
    (root / "fence").write_text("fleet-a 2\n")
    (attempt / "batch.sh").write_text("#!/bin/sh\n")
    secure_root(root)
    result = subprocess.run(
        ["bash", "-c", SUBMIT_WRAPPER, "fleetq", str(root), "fleet-a", "2", attempt_id],
        text=True, capture_output=True, check=True,
    )
    assert '"reason":"not_staged"' in result.stdout
    assert not (attempt / "submit.claim").exists()


def test_stage_cannot_replace_claimed_attempt(tmp_path):
    root = tmp_path / "control"
    attempt_id = "att_0123456789abcdef01234567"
    stage_id = "0123456789abcdef0123456789abcdef"
    attempt = root / "attempts" / attempt_id
    attempt.mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "fence").write_text("fleet-a 2\n")
    (attempt / "batch.sh").write_text("original batch\n")
    (attempt / "manifest.json").write_text('{"bundle_digest": null}\n')
    (attempt / "submit.claim").mkdir()
    staged = root / "cache" / f".stage-{stage_id}" / "attempt"
    staged.mkdir(parents=True)
    (staged / "batch.sh").write_text("replacement batch\n")
    (staged / "manifest.json").write_text('{"bundle_digest": null,"epoch":2}\n')
    secure_root(root)
    result = subprocess.run(
        ["bash", "-c", STAGE_WRAPPER, "fleetq", str(root), "fleet-a", "2", attempt_id, stage_id],
        text=True, capture_output=True, check=True,
    )
    assert '"reason":"already_submitted"' in result.stdout
    assert (attempt / "batch.sh").read_text() == "original batch\n"


def test_stage_publishes_manifest_and_bundle_pin(tmp_path):
    root = tmp_path / "control"
    attempt_id = "att_0123456789abcdef01234567"
    stage_id = "1123456789abcdef0123456789abcdef"
    digest = "b" * 64
    (root / "attempts").mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "controller.lock").touch()
    (root / "fence").write_text("fleet-a 2\n")
    secure_root(root)
    staged = root / "cache" / f".stage-{stage_id}"
    (staged / "attempt").mkdir(parents=True)
    (staged / "cache").mkdir()
    (staged / "attempt" / "batch.sh").write_text("#!/bin/bash\n")
    manifest = {"attempt_id": attempt_id, "bundle_digest": f"sha256:{digest}", "epoch": 2,
                "fleet_id": "fleet-a", "job_id": 9, "request": {}, "spec_digest": "spec"}
    # Original executor manifests had no trailing newline; the wrapper accepts
    # this single JSON record as well as the normalized newline form.
    (staged / "attempt" / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    (staged / "cache" / f"{digest}.tar.gz").write_bytes(b"bundle")

    result = subprocess.run(
        ["bash", "-c", STAGE_WRAPPER, "fleetq", str(root), "fleet-a", "2", attempt_id, stage_id, digest],
        text=True, capture_output=True, check=True,
    )

    assert json.loads(result.stdout) == {"ok": True}
    published = root / "attempts" / attempt_id
    assert json.loads((published / "manifest.json").read_text()) == manifest
    assert (published / "manifest.sha256").read_text().strip()
    assert (published / "stage.ready").exists()
    assert (root / "cache" / f"{digest}.tar.gz").read_bytes() == b"bundle"
    assert (root / "cache" / f"{digest}.tar.gz").stat().st_mode & 0o777 == 0o600
