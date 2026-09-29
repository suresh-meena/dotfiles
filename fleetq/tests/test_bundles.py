"""Tests for fleetq.bundles: content-addressed snapshot tarballs."""

from __future__ import annotations

import gzip
import io
import json
import os
import pathlib
import resource
import shutil
import tarfile
import time

import pytest

from fleetq import bundles


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _populate_tree(base: pathlib.Path) -> None:
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "file1.txt").write_text("hello")
    (base / "file2.py").write_text("print(1)\n")
    script = base / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)


class _ZeroSource:
    """A read()-only source of N zero bytes, generated on demand (never
    materialized as a single giant bytes object)."""

    def __init__(self, total: int) -> None:
        self.remaining = total

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.remaining
        n = min(size, self.remaining)
        self.remaining -= n
        return b"\x00" * n


def _write_bundle(path, members, fmt=tarfile.PAX_FORMAT, compresslevel=6) -> None:
    """Build a raw (possibly malicious) gzip+tar file directly with
    tarfile/gzip, bypassing bundle_build entirely, so tests can craft
    archives bundle_build itself would never produce."""
    buf = io.BytesIO()
    with tarfile.open(mode="w", fileobj=buf, format=fmt) as tar:
        for m in members:
            info = tarfile.TarInfo(name=m.get("name", "placeholder"))
            info.type = m.get("type", tarfile.REGTYPE)
            info.mode = m.get("mode", 0o644)
            info.uid = m.get("uid", 0)
            info.gid = m.get("gid", 0)
            info.uname = m.get("uname", "")
            info.gname = m.get("gname", "")
            info.mtime = 0
            if "linkname" in m:
                info.linkname = m["linkname"]
            if "devmajor" in m:
                info.devmajor = m["devmajor"]
                info.devminor = m.get("devminor", 0)
            if "sparse" in m:
                info.sparse = m["sparse"]
            if "pax_name" in m:
                # Carries an exact byte sequence (e.g. embedding a NUL or a
                # control character) through as a PAX extended header value,
                # which is length-prefixed rather than NUL-terminated, so it
                # survives a round trip intact where a plain USTAR name field
                # would truncate at the first NUL.
                info.pax_headers = {"path": m["pax_name"]}
            data = m.get("data")
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif "fileobj" in m:
                info.size = m["size"]
                tar.addfile(info, m["fileobj"])
            else:
                info.size = 0
                tar.addfile(info)
    tar_bytes = buf.getvalue()
    with open(path, "wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=compresslevel
        ) as gz:
            gz.write(tar_bytes)


def _assert_no_leftover_tmp_dirs(parent: pathlib.Path) -> None:
    leftovers = [p.name for p in parent.iterdir() if p.name.startswith(".bundle-extract-")]
    assert leftovers == []


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_determinism_identical_build_gives_identical_digest_and_bytes(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    _populate_tree(src)
    out1 = tmp_path / "b1.tar.gz"
    out2 = tmp_path / "b2.tar.gz"
    info1 = bundles.bundle_build(src, out1)
    info2 = bundles.bundle_build(src, out2)
    assert info1.digest == info2.digest
    assert info1.compressed_bytes == info2.compressed_bytes
    assert out1.read_bytes() == out2.read_bytes()


def test_determinism_survives_mtime_touch(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    _populate_tree(src)
    out1 = tmp_path / "b1.tar.gz"
    info1 = bundles.bundle_build(src, out1)

    future = time.time() + 100_000
    for p in src.rglob("*"):
        if p.is_file():
            os.utime(p, (future, future))

    out2 = tmp_path / "b2.tar.gz"
    info2 = bundles.bundle_build(src, out2)
    assert info1.digest == info2.digest


def test_determinism_same_exec_bit_different_exact_mode(tmp_path):
    src1 = tmp_path / "proj1"
    src1.mkdir()
    _populate_tree(src1)
    src2 = tmp_path / "proj2"
    shutil.copytree(src1, src2)

    # Different exact permission bits, same execute-bit status as src1.
    (src2 / "sub" / "file1.txt").chmod(0o640)  # non-exec, like src1's 0o644
    (src2 / "file2.py").chmod(0o600)  # non-exec, like src1's 0o644
    (src2 / "run.sh").chmod(0o750)  # exec, like src1's 0o755

    out1 = tmp_path / "b1.tar.gz"
    out2 = tmp_path / "b2.tar.gz"
    info1 = bundles.bundle_build(src1, out1)
    info2 = bundles.bundle_build(src2, out2)
    assert info1.digest == info2.digest


def test_determinism_one_byte_diff_changes_digest(tmp_path):
    src1 = tmp_path / "proj1"
    src1.mkdir()
    (src1 / "a.txt").write_text("hello")
    src2 = tmp_path / "proj2"
    src2.mkdir()
    (src2 / "a.txt").write_text("hellp")

    out1 = tmp_path / "b1.tar.gz"
    out2 = tmp_path / "b2.tar.gz"
    info1 = bundles.bundle_build(src1, out1)
    info2 = bundles.bundle_build(src2, out2)
    assert info1.digest != info2.digest


# --------------------------------------------------------------------------
# excludes
# --------------------------------------------------------------------------


def test_bundle_default_excludes_matches_spec_list():
    expected = [
        ".direnv/",
        ".env",
        ".env.local",
        ".git/",
        ".mypy_cache/",
        ".nox/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".venv/",
        "__pycache__/",
        "artifacts/",
        "build/",
        "dist/",
        "log/",
        "logs/",
        "remote-downloads/",
        "runs/",
        "*.pyc",
    ]
    assert bundles.BUNDLE_DEFAULT_EXCLUDES == expected


def test_exclude_defaults_prune_directory(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    gitdir = src / ".git"
    gitdir.mkdir()
    (gitdir / "config").write_text("should-not-appear")
    (gitdir / "objects").mkdir()
    (gitdir / "objects" / "pack").write_text("should-not-appear-either")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    names = [f[0] for f in info.files]
    assert "keep.txt" in names
    assert not any(n == ".git" or n.startswith(".git/") for n in names)


def test_exclude_secret_patterns(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    ssh = src / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("SECRET")
    (src / "server.pem").write_text("SECRET")
    (src / "keep.txt").write_text("keep")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    names = [f[0] for f in info.files]
    assert "keep.txt" in names
    assert not any(n.startswith(".ssh/") for n in names)
    assert "server.pem" not in names


def test_exclude_fqignore_file(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "secretdata.bin").write_text("data")
    (src / ".fqignore").write_text("# a comment\nsecretdata.bin\n\n")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    names = [f[0] for f in info.files]
    assert "keep.txt" in names
    assert "secretdata.bin" not in names


def test_exclude_extra_excludes_param(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "custom.dat").write_text("data")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out, extra_excludes=["custom.dat"])
    names = [f[0] for f in info.files]
    assert "keep.txt" in names
    assert "custom.dat" not in names


# --------------------------------------------------------------------------
# links and special files
# --------------------------------------------------------------------------


def test_symlink_to_file_skipped_and_reported(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "real.txt").write_text("data")
    link = src / "link.txt"
    link.symlink_to(src / "real.txt")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    names = [f[0] for f in info.files]
    assert "real.txt" in names
    assert "link.txt" not in names
    assert ("link.txt", "symlink") in info.skipped


def test_symlink_to_dir_outside_root_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")

    src = tmp_path / "proj"
    src.mkdir()
    link = src / "escape"
    link.symlink_to(outside, target_is_directory=True)

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    names = [f[0] for f in info.files]
    assert not any(n.startswith("escape") for n in names)
    assert ("escape", "symlink") in info.skipped


def test_dangling_symlink_skipped(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    link = src / "dangling"
    link.symlink_to(src / "does_not_exist")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    assert ("dangling", "symlink") in info.skipped


def test_fifo_skipped(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    os.mkfifo(src / "myfifo")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    assert ("myfifo", "fifo") in info.skipped
    names = [f[0] for f in info.files]
    assert "myfifo" not in names


# --------------------------------------------------------------------------
# limits (build side)
# --------------------------------------------------------------------------


def test_limit_file_too_large(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "big.bin").write_bytes(b"x" * 2048)

    out = tmp_path / "b.tar.gz"
    limits = bundles.BundleLimits(max_file=1024)
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_build(src, out, limits=limits)
    assert excinfo.value.code == "file_too_large"


def test_limit_too_many_members(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    for i in range(10):
        (src / f"f{i}.txt").write_text("x")

    out = tmp_path / "b.tar.gz"
    limits = bundles.BundleLimits(max_members=5)
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_build(src, out, limits=limits)
    assert excinfo.value.code == "too_many_members"


def test_limit_expanded_total(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    for i in range(5):
        (src / f"f{i}.bin").write_bytes(b"x" * 1000)

    out = tmp_path / "b.tar.gz"
    limits = bundles.BundleLimits(max_expanded=2000, max_file=10_000)
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_build(src, out, limits=limits)
    assert excinfo.value.code == "limits_expanded"


def test_limit_path_too_long(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    # Build a relpath that exceeds max_path_len in aggregate without any
    # single component exceeding the real filesystem's NAME_MAX.
    cur = src
    for i in range(6):
        cur = cur / f"directory_{i:02d}"
        cur.mkdir()
    (cur / "file.txt").write_text("x")

    out = tmp_path / "b.tar.gz"
    limits = bundles.BundleLimits(max_path_len=50)
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_build(src, out, limits=limits)
    assert excinfo.value.code == "path_unsafe"


def test_limit_depth_too_deep(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    cur = src
    for i in range(10):
        cur = cur / f"d{i}"
        cur.mkdir()
    (cur / "deepfile.txt").write_text("x")

    out = tmp_path / "b.tar.gz"
    limits = bundles.BundleLimits(max_depth=5)
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_build(src, out, limits=limits)
    assert excinfo.value.code == "path_unsafe"


# --------------------------------------------------------------------------
# changed during read
# --------------------------------------------------------------------------


def test_changed_during_read_retries_then_succeeds(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "flaky.txt").write_text("original content")

    state = {"calls": 0}
    original = bundles._bundle_open_and_read

    def flaky(path):
        state["calls"] += 1
        data = original(path)
        if state["calls"] == 1:
            with open(path, "ab") as fh:
                fh.write(b"x")
        return data

    monkeypatch.setattr(bundles, "_bundle_open_and_read", flaky)

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out, max_retries=2)
    assert state["calls"] == 2
    assert out.exists()
    assert info.digest.startswith("sha256:")


def test_changed_during_read_exhausts_retries_and_raises(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "flaky.txt").write_text("original content")

    original = bundles._bundle_open_and_read

    def always_flaky(path):
        data = original(path)
        with open(path, "ab") as fh:
            fh.write(b"x")
        return data

    monkeypatch.setattr(bundles, "_bundle_open_and_read", always_flaky)

    out = tmp_path / "b.tar.gz"
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_build(src, out, max_retries=2)
    assert excinfo.value.code == "changed_during_read"
    assert not out.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".bundle-")]
    assert leftovers == []


# --------------------------------------------------------------------------
# malicious archives
# --------------------------------------------------------------------------


def _mk_absolute():
    return [{"name": "/etc/passwd", "data": b"x"}]


def _mk_dotdot():
    return [{"name": "../escape", "data": b"x"}]


def _mk_dotdot_nested():
    return [{"name": "a/../../x", "data": b"x"}]


def _mk_nul():
    return [{"name": "placeholder", "pax_name": "foo\x00bar", "data": b"x"}]


def _mk_ctrl():
    return [{"name": "placeholder", "pax_name": "foo\x01bar", "data": b"x"}]


def _mk_symlink():
    return [{"name": "link1", "type": tarfile.SYMTYPE, "linkname": "target"}]


def _mk_hardlink():
    return [
        {"name": "a", "data": b"xxx"},
        {"name": "hardlink_b", "type": tarfile.LNKTYPE, "linkname": "a"},
    ]


def _mk_chardev():
    return [{"name": "dev1", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3}]


def _mk_blockdev():
    return [{"name": "dev2", "type": tarfile.BLKTYPE, "devmajor": 1, "devminor": 3}]


def _mk_fifo():
    return [{"name": "fifo1", "type": tarfile.FIFOTYPE}]


def _mk_duplicate():
    return [{"name": "dup", "data": b"a"}, {"name": "dup", "data": b"b"}]


def _mk_conflict():
    return [{"name": "a", "data": b"x"}, {"name": "a/b", "data": b"y"}]


def _mk_setuid():
    return [{"name": "suid", "mode": 0o4755, "data": b"x"}]


def _mk_nonzero_uid():
    return [{"name": "uidfile", "uid": 1000, "data": b"x"}]


_MALICIOUS_CASES = [
    ("absolute_path", _mk_absolute, "path_unsafe"),
    ("dotdot", _mk_dotdot, "path_unsafe"),
    ("dotdot_nested", _mk_dotdot_nested, "path_unsafe"),
    ("nul_in_name", _mk_nul, "path_unsafe"),
    ("control_char_in_name", _mk_ctrl, "path_unsafe"),
    ("symlink_member", _mk_symlink, "link_rejected"),
    ("hardlink_member", _mk_hardlink, "link_rejected"),
    ("chardev_member", _mk_chardev, "special_file_rejected"),
    ("blockdev_member", _mk_blockdev, "special_file_rejected"),
    ("fifo_member", _mk_fifo, "special_file_rejected"),
    ("duplicate_names", _mk_duplicate, "duplicate_path"),
    ("file_dir_conflict", _mk_conflict, "duplicate_path"),
    ("setuid_bit", _mk_setuid, "forbidden_metadata"),
    ("nonzero_uid", _mk_nonzero_uid, "forbidden_metadata"),
]


@pytest.mark.parametrize(
    "case_name, builder, code", _MALICIOUS_CASES, ids=[c[0] for c in _MALICIOUS_CASES]
)
def test_malicious_archive_rejected(tmp_path, case_name, builder, code):
    archive = tmp_path / f"{case_name}.tar.gz"
    _write_bundle(archive, builder())

    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_validate(archive)
    assert excinfo.value.code == code

    dest = tmp_path / f"{case_name}_dest"
    with pytest.raises(bundles.BundleError) as excinfo2:
        bundles.bundle_extract(archive, dest, expected_digest="sha256:" + "0" * 64)
    assert excinfo2.value.code == code
    assert not dest.exists()
    _assert_no_leftover_tmp_dirs(tmp_path)


def test_gnu_sparse_member_rejected(tmp_path):
    archive = tmp_path / "sparse.tar.gz"
    members = [
        {
            "name": "sparsefile",
            "type": tarfile.GNUTYPE_SPARSE,
            "data": b"x" * 10,
            "sparse": [(0, 5), (100, 5)],
        }
    ]
    _write_bundle(archive, members, fmt=tarfile.GNU_FORMAT)

    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_validate(archive)
    assert excinfo.value.code == "special_file_rejected"

    dest = tmp_path / "sparse_dest"
    with pytest.raises(bundles.BundleError) as excinfo2:
        bundles.bundle_extract(archive, dest, expected_digest="sha256:" + "0" * 64)
    assert excinfo2.value.code == "special_file_rejected"
    assert not dest.exists()
    _assert_no_leftover_tmp_dirs(tmp_path)


def test_gzip_bomb_rejected_quickly(tmp_path):
    archive = tmp_path / "bomb.tar.gz"
    total = 2 * 1024**3  # 2 GiB claimed, never actually materialized at once
    members = [{"name": "bomb.bin", "fileobj": _ZeroSource(total), "size": total}]
    _write_bundle(archive, members, compresslevel=9)
    assert archive.stat().st_size < 5 * 1024 * 1024

    limits = bundles.BundleLimits(max_file=4 * 1024**3, max_expanded=1_000_000)

    start = time.monotonic()
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_validate(archive, limits=limits)
    elapsed = time.monotonic() - start
    assert excinfo.value.code == "limits_expanded"
    assert elapsed < 10.0

    dest = tmp_path / "bomb_dest"
    with pytest.raises(bundles.BundleError) as excinfo2:
        bundles.bundle_extract(archive, dest, expected_digest="sha256:" + "0" * 64, limits=limits)
    assert excinfo2.value.code == "limits_expanded"
    assert not dest.exists()
    _assert_no_leftover_tmp_dirs(tmp_path)


def test_truncated_gzip_rejected(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "file.txt").write_text("hello world" * 100)
    good = tmp_path / "good.tar.gz"
    bundles.bundle_build(src, good)

    data = good.read_bytes()
    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(data[: len(data) // 2])

    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_validate(truncated)
    assert excinfo.value.code == "bad_format"

    dest = tmp_path / "trunc_dest"
    with pytest.raises(bundles.BundleError) as excinfo2:
        bundles.bundle_extract(truncated, dest, expected_digest="sha256:" + "0" * 64)
    assert excinfo2.value.code == "bad_format"
    assert not dest.exists()
    _assert_no_leftover_tmp_dirs(tmp_path)


# --------------------------------------------------------------------------
# digest mismatch
# --------------------------------------------------------------------------


def test_digest_mismatch_rejected(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    out = tmp_path / "b.tar.gz"
    bundles.bundle_build(src, out)

    wrong = "sha256:" + "0" * 64
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_validate(out, expected_digest=wrong)
    assert excinfo.value.code == "digest_mismatch"

    dest = tmp_path / "dest"
    with pytest.raises(bundles.BundleError) as excinfo2:
        bundles.bundle_extract(out, dest, expected_digest=wrong)
    assert excinfo2.value.code == "digest_mismatch"
    assert not dest.exists()
    _assert_no_leftover_tmp_dirs(tmp_path)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def test_extract_reproduces_bytes_and_modes(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "plain.txt").write_text("hello world")
    script = src / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o750)
    (src / "sub").mkdir()
    (src / "sub" / "nested.txt").write_text("nested content")

    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)

    dest = tmp_path / "extracted"
    bundles.bundle_extract(out, dest, expected_digest=info.digest)

    assert (dest / "plain.txt").read_text() == "hello world"
    assert (dest / "sub" / "nested.txt").read_text() == "nested content"
    assert (dest / "run.sh").read_bytes() == script.read_bytes()

    assert (os.stat(dest / "plain.txt").st_mode & 0o777) == 0o644
    assert (os.stat(dest / "run.sh").st_mode & 0o777) == 0o755
    assert (os.stat(dest / "sub").st_mode & 0o777) == 0o755


def test_extract_dest_exists_raises(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("x")
    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.bundle_extract(out, dest, expected_digest=info.digest)
    assert excinfo.value.code == "dest_exists"


def test_extract_atomic_publish_failure_midway(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    for i in range(5):
        (src / f"f{i}.txt").write_text(f"content {i}" * 10)
    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)

    state = {"n": 0}
    original = bundles._bundle_write_all

    def flaky_write(fd, data):
        state["n"] += 1
        if state["n"] == 3:
            raise RuntimeError("simulated failure mid-extract")
        return original(fd, data)

    monkeypatch.setattr(bundles, "_bundle_write_all", flaky_write)

    dest = tmp_path / "extracted"
    with pytest.raises(RuntimeError):
        bundles.bundle_extract(out, dest, expected_digest=info.digest)

    assert not dest.exists()
    _assert_no_leftover_tmp_dirs(tmp_path)


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def test_manifest_json_deterministic(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    (src / "b.txt").write_text("world")
    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)

    j1 = bundles.bundle_manifest_json(info)
    j2 = bundles.bundle_manifest_json(info)
    assert j1 == j2

    parsed = json.loads(j1)
    assert parsed["digest"] == info.digest
    assert parsed["members"] == info.members
    assert parsed["format_version"] == bundles.BUNDLE_FORMAT_VERSION


# --------------------------------------------------------------------------
# streaming (bounded memory)
# --------------------------------------------------------------------------


def test_streaming_validate_bounded_memory(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "big.bin").write_bytes(b"\x00" * (50 * 1024 * 1024))
    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    bundles.bundle_validate(out, expected_digest=info.digest)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    growth_kib = after - before
    # ru_maxrss is a KiB high-water mark on Linux; a validate() that
    # buffered the whole 50 MiB file (or its fully decompressed form) would
    # show growth on that order. Streaming should stay far below it.
    assert growth_kib < 20 * 1024
