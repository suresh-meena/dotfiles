"""Artifact collection helpers and the node-side staging rules (§6.4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fleetq.engine import artifacts

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "build" / "fq-node"
FLEET = "fleet_test"


@pytest.fixture(scope="module", autouse=True)
def built():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py")], check=True, capture_output=True)


@pytest.mark.parametrize("rel,ok", [
    ("out/model.pt", "out/model.pt"), ("a/./b", "a/b"), ("a/../b", "b"),
    ("../x", None), ("a/../../x", None), ("/etc/passwd", None), ("", None), (".", None), ("a\x00b", None),
])
def test_safe_relpath(rel, ok):
    assert artifacts.safe_relpath(rel) == ok


def test_a_required_directory_is_covered_by_any_file_under_it():
    files = [{"relpath": "out/a.txt"}, {"relpath": "outside.txt"}]
    assert artifacts.covered("out", files) and artifacts.covered("out/", files)
    assert not artifacts.covered("ou", files) and not artifacts.covered("runs", files)


def test_verify_checks_presence_size_and_hash(tmp_path):
    (tmp_path / "000000").write_bytes(b"abc")
    (tmp_path / "000001").write_bytes(b"xyz")
    good = artifacts.sha256_file(tmp_path / "000000")
    files = [{"slot": "000000", "relpath": "a", "size": 3, "sha256": good},
             {"slot": "000001", "relpath": "b", "size": 3, "sha256": "0" * 64},
             {"slot": "000002", "relpath": "c", "size": 1}]
    problems = artifacts.verify(tmp_path, files)
    assert problems == ["b: hash mismatch", "c: not transferred"]
    assert files[0]["local_sha256"] == good


def test_publish_reuses_only_an_identical_tree(tmp_path):
    partial = tmp_path / ".partial" / "att_1"
    partial.mkdir(parents=True)
    (partial / "000000").write_text("m")
    (partial / "000001").write_text("n")
    final = tmp_path / "7" / "1"
    files = [{"slot": "000000", "relpath": "out/m.pt"},
             {"slot": "000001", "relpath": "runs/deep/n.json"}]
    artifacts.publish(partial, final, files)
    assert (final / "out" / "m.pt").read_text() == "m" and (final / "runs" / "deep" / "n.json").read_text() == "n"
    assert not partial.exists()
    partial.mkdir()
    (partial / "000000").write_text("m")
    (partial / "000001").write_text("n")
    artifacts.publish(partial, final, files)  # crash after publish, before DB mark
    assert not partial.exists()
    partial.mkdir()
    (partial / "000000").write_text("different")
    (partial / "000001").write_text("n")
    with pytest.raises(FileExistsError):
        artifacts.publish(partial, final, files)
    assert (final / "out" / "m.pt").read_text() == "m"


def test_verify_rejects_a_transferred_symlink(tmp_path):
    target = tmp_path / "secret"
    target.write_bytes(b"secret")
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "000000").symlink_to(target)
    files = [{"slot": "000000", "relpath": "out/secret", "size": 6}]
    assert artifacts.verify(partial, files) == ["out/secret: not transferred"]
    with pytest.raises(ValueError):
        artifacts.publish(partial, tmp_path / "final", files)


def test_publish_refuses_symlinked_staging_or_final_parent(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "important"
    outside.mkdir()
    stolen = outside / "att_1"
    stolen.mkdir()
    (stolen / "000000").write_text("keep")
    (root / ".partial").symlink_to(outside, target_is_directory=True)
    partial = root / ".partial" / "att_1"
    with pytest.raises(ValueError, match="unsafe artifact directory"):
        artifacts.publish(partial, root / "7" / "1", [{"slot": "000000", "relpath": "out/file"}])
    assert (stolen / "000000").read_text() == "keep"

    (root / ".partial").unlink()
    partial.mkdir(parents=True)
    (partial / "000000").write_text("new")
    (root / "7").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe artifact directory"):
        artifacts.publish(partial, root / "7" / "1", [{"slot": "000000", "relpath": "out/file"}])
    assert (partial / "000000").read_text() == "new"


def test_publish_rejects_output_traversal_even_if_called_directly(tmp_path):
    partial = tmp_path / ".partial" / "att_1"
    partial.mkdir(parents=True)
    (partial / "000000").write_text("new")
    with pytest.raises(ValueError, match="unsafe artifact publish path"):
        artifacts.publish(partial, tmp_path / "7" / "1",
                          [{"slot": "000000", "relpath": "../../important"}])
    assert (partial / "000000").read_text() == "new"


def test_staging_cleanup_refuses_parent_traversal(tmp_path):
    root = tmp_path / "artifacts"
    (root / ".partial").mkdir(parents=True)
    important = tmp_path / "important"
    important.mkdir()
    protected = important / "att_1"
    protected.mkdir()
    (protected / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="unsafe artifact directory"):
        artifacts.remove_staging(root / ".partial" / ".." / ".." / "important" / "att_1")
    with pytest.raises(ValueError, match="unsafe artifact staging entry"):
        artifacts.remove_staging(root / ".partial" / ".")
    assert (protected / "keep.txt").read_text() == "keep"


# ---- the shim's collect-stage ---------------------------------------------------------


class Node:
    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "root"
        self.env = {**os.environ, "FQ_NODE_BOOT_ID": "b"}
        self.call("enroll", "--fleet-id", FLEET, "--node-id", "n")
        self.call("fence", "--fleet-id", FLEET, "--epoch", "1")

    def call(self, *args):
        proc = subprocess.run([sys.executable, str(SHIM), "--root", str(self.root), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def attempt(self, name: str, **manifest) -> Path:
        adir = self.root / "attempts" / name
        (adir / "code").mkdir(parents=True)
        (adir / "manifest.json").write_text(json.dumps({"attempt_id": name, "epoch": 1, **manifest}))
        return adir

    def stage(self, attempt, *paths, max_bytes=10 ** 9):
        args = ["collect-stage", "--fleet-id", FLEET, "--epoch", "1", attempt, "--max-bytes", str(max_bytes)]
        for p in paths:
            args += ["--path", p]
        return self.call(*args)


def test_shim_stages_regular_files_only_and_never_follows_links(tmp_path):
    node = Node(tmp_path)
    adir = node.attempt("att_a")
    code = adir / "code"
    (code / "out" / "deep").mkdir(parents=True)
    (code / "out" / "model.pt").write_bytes(b"weights")
    (code / "out" / "deep" / "odd name\n.txt").write_text("x")
    (code / "out" / "pw").symlink_to("/etc/passwd")
    (tmp_path / "secret").mkdir()
    (code / "out" / "escape").symlink_to(tmp_path / "secret")
    (code / "linked").symlink_to(tmp_path / "secret")
    body = node.stage("att_a", "out", "linked", "absent")
    assert body["ok"], body
    assert sorted(f["relpath"] for f in body["files"]) == ["out/deep/odd name\n.txt", "out/model.pt"]
    assert {r["path"]: r["reason"] for r in body["refused"]} == {
        "out/pw": "not_a_regular_file", "out/escape": "symlink", "linked": "escapes_workdir"}
    assert body["missing"] == ["absent"]
    outbox = adir / "outbox"
    slots = sorted(p.name for p in outbox.iterdir() if p.name.isdigit())
    assert slots == ["000000", "000001"]
    model = next(f for f in body["files"] if f["relpath"] == "out/model.pt")
    assert os.stat(outbox / model["slot"]).st_ino == os.stat(code / "out" / "model.pt").st_ino, "a hard link"
    again = node.stage("att_a", "out")
    assert again["already"] is True and again["files"] == body["files"]
    assert node.call("collect-clean", "--fleet-id", FLEET, "--epoch", "1", "att_a")["ok"] and not outbox.exists()
    assert (code / "out" / "model.pt").read_bytes() == b"weights", "cleaning removes links, not outputs"


def test_shim_refuses_over_the_allowance_and_in_place_jobs(tmp_path):
    node = Node(tmp_path)
    adir = node.attempt("att_b")
    (adir / "code" / "big").write_bytes(b"0" * 1000)
    body = node.stage("att_b", "big", max_bytes=999)
    assert body["ok"] is False and body["error"]["code"] == "too_large"
    assert not (adir / "outbox").exists()
    node.attempt("att_c", in_place="/data/proj")
    assert node.stage("att_c", "x")["error"]["code"] == "in_place_collect"
