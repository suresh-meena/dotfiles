"""fleetq.bundles: content-addressed snapshot tarballs for job payloads.

A "bundle" is a gzip-compressed, canonical tar archive of a project
directory.  Its identity (``digest``) is the sha256 of the *uncompressed*
tar stream, computed the same way whether the bundle was just built
(:func:`bundle_build`) or is being checked on a remote node
(:func:`bundle_validate`, :func:`bundle_extract`).

This module is stdlib-only and is concatenated verbatim into two
single-file scripts (a client CLI and a node shim), so it must not import
any other ``fleetq`` module, must not use relative imports, and must not
contain an ``if __name__ == "__main__":`` block.  Every public name is
prefixed ``bundle_``/``Bundle``; every private helper is prefixed
``_bundle_``, including private classes, to avoid collisions when this
file's top-level names share a namespace with other concatenated modules.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import gzip
import hashlib
import io
import os
import pathlib
import shutil
import stat
import tarfile
import tempfile
import json

BUNDLE_FORMAT_VERSION = 1

# Kept byte-for-byte identical to fleetctl's DEFAULT_SYNC_EXCLUDES
# (shared/fleet-dotfiles/bin/fleetctl) on purpose: a test elsewhere compares
# the two lists for equality.
BUNDLE_DEFAULT_EXCLUDES = [
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

BUNDLE_SECRET_EXCLUDES = [
    ".ssh/",
    ".gnupg/",
    ".aws/",
    ".netrc",
    "*.pem",
    "id_rsa*",
    "id_ed25519*",
    ".fleetq-token",
    "*.key",
]

_BUNDLE_CONTROL_CHARS = frozenset(chr(c) for c in list(range(0x20)) + [0x7F])
_BUNDLE_TAR_BLOCKSIZE = 512
_BUNDLE_READ_CHUNK = 65536


class BundleError(Exception):
    """Raised for every expected failure in this module.

    ``code`` is a stable, machine-checkable string (see the module's public
    docs / the task spec for the full list); ``message`` is a free-form,
    human-readable detail.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class BundleLimits:
    max_compressed: int = 256 * 1024 * 1024
    max_file: int = 100 * 1024 * 1024
    max_members: int = 50_000
    max_expanded: int = 1024 * 1024 * 1024
    max_path_len: int = 1024
    max_depth: int = 64
    # API storage policy (not archive-validation limits). Kept beside bundle
    # limits so deployments can tune logical cache quotas in [limits.bundle].
    max_owner_bytes: int = 10 * 1024 * 1024 * 1024
    max_global_bytes: int = 100 * 1024 * 1024 * 1024


@dataclasses.dataclass
class BundleInfo:
    digest: str
    compressed_bytes: int
    expanded_bytes: int
    members: int
    format_version: int
    files: list  # list[tuple[str, int, str]]  (relpath, size, sha256hex)
    skipped: list  # list[tuple[str, str]]  (relpath, reason)


def bundle_manifest_json(info: BundleInfo) -> str:
    """Deterministic JSON rendering of a BundleInfo (a local dry-run manifest)."""
    payload = dataclasses.asdict(info)
    return json.dumps(payload, sort_keys=True, indent=2)


# --------------------------------------------------------------------------
# Excludes
# --------------------------------------------------------------------------


def _bundle_load_fqignore(root: pathlib.Path) -> list:
    ignore_path = root / ".fqignore"
    try:
        text = ignore_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return []
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _bundle_compile_excludes(raw_patterns) -> list:
    """Compile raw fnmatch-style patterns into (core, anchored, dir_only).

    Mirrors rsync's exclude semantics (which is what these patterns are also
    fed to, via fleetctl): a pattern ending in "/" only matches directories
    and prunes them; a pattern containing a "/" (other than a trailing one)
    is anchored to the bundle root and matched against the full relative
    path; a pattern with no interior "/" is matched against the final path
    component (basename) only, so it applies at any depth.
    """
    compiled = []
    for pattern in raw_patterns:
        dir_only = pattern.endswith("/")
        core = pattern[:-1] if dir_only else pattern
        if not core:
            continue
        anchored = "/" in core
        compiled.append((core, anchored, dir_only))
    return compiled


def _bundle_is_excluded(relpath: str, is_dir: bool, compiled_patterns) -> bool:
    basename = relpath.rsplit("/", 1)[-1]
    for core, anchored, dir_only in compiled_patterns:
        if dir_only and not is_dir:
            continue
        target = relpath if anchored else basename
        if fnmatch.fnmatch(target, core):
            return True
    return False


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


class _bundle_ChangedDuringRead(Exception):
    def __init__(self, relpath: str) -> None:
        super().__init__(relpath)
        self.relpath = relpath


def _bundle_open_and_read(path: pathlib.Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _bundle_read_file_checked(path: pathlib.Path, relpath: str) -> bytes:
    """Read a file's full contents, detecting a change during the read.

    Stats the file immediately before opening it and again immediately
    after finishing the read; if size, mtime_ns, inode or device differ (or
    the number of bytes actually read doesn't match the pre-read size), the
    file changed out from under us and the whole build must be retried.
    """
    pre = os.stat(path, follow_symlinks=False)
    data = _bundle_open_and_read(path)
    post = os.stat(path, follow_symlinks=False)
    if (
        len(data) != pre.st_size
        or pre.st_size != post.st_size
        or pre.st_mtime_ns != post.st_mtime_ns
        or pre.st_ino != post.st_ino
        or pre.st_dev != post.st_dev
    ):
        raise _bundle_ChangedDuringRead(relpath)
    return data


class _bundle_CountingHasher:
    """Writable wrapper: hashes and counts every byte written through it.

    Used to sit between tarfile's writer and the gzip compressor, so the
    running hash is exactly the uncompressed canonical tar stream.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.sha256 = hashlib.sha256()
        self.count = 0

    def write(self, data: bytes) -> int:
        self.sha256.update(data)
        self.count += len(data)
        self._inner.write(data)
        return len(data)

    def flush(self) -> None:
        flush = getattr(self._inner, "flush", None)
        if flush is not None:
            flush()


class _bundle_LimitedWriter:
    """Writable wrapper: enforces a hard cap on bytes written (compressed size)."""

    def __init__(self, inner, limit: int) -> None:
        self._inner = inner
        self._limit = limit
        self.count = 0

    def write(self, data: bytes) -> int:
        self.count += len(data)
        if self.count > self._limit:
            raise BundleError(
                "too_large", f"compressed bundle exceeds {self._limit} bytes"
            )
        self._inner.write(data)
        return len(data)

    def flush(self) -> None:
        flush = getattr(self._inner, "flush", None)
        if flush is not None:
            flush()


def _bundle_gather_tree(root: pathlib.Path, limits: BundleLimits, compiled_excludes):
    """Walk root without following any symlink, applying excludes.

    Returns (dirs, files, skipped, expanded_total) where dirs is a list of
    relpaths, files is a list of (relpath, abspath_str, size, is_exec), and
    skipped is a list of (relpath, reason).
    """
    dirs: list = []
    files: list = []
    skipped: list = []
    member_count = 0
    expanded_total = 0

    stack = [(root, "", 0)]
    while stack:
        current_dir, current_rel, depth = stack.pop()
        try:
            entries = list(os.scandir(current_dir))
        except OSError as exc:
            raise BundleError(
                "bad_format", f"cannot read directory {current_dir}: {exc}"
            ) from exc
        entries.sort(key=lambda e: e.name)
        for entry in entries:
            relpath = f"{current_rel}/{entry.name}" if current_rel else entry.name
            if len(relpath) > limits.max_path_len:
                raise BundleError("path_unsafe", f"path too long: {relpath!r}")
            child_depth = depth + 1
            if child_depth > limits.max_depth:
                raise BundleError("path_unsafe", f"path too deep: {relpath!r}")

            if entry.is_symlink():
                skipped.append((relpath, "symlink"))
                continue

            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                skipped.append((relpath, f"stat_error:{exc.errno}"))
                continue

            mode = st.st_mode
            if stat.S_ISDIR(mode):
                if _bundle_is_excluded(relpath, True, compiled_excludes):
                    continue
                member_count += 1
                if member_count > limits.max_members:
                    raise BundleError(
                        "too_many_members", f"more than {limits.max_members} members"
                    )
                dirs.append(relpath)
                stack.append((entry.path, relpath, child_depth))
            elif stat.S_ISREG(mode):
                if _bundle_is_excluded(relpath, False, compiled_excludes):
                    continue
                if st.st_size > limits.max_file:
                    raise BundleError(
                        "file_too_large",
                        f"{relpath} ({st.st_size} bytes) exceeds {limits.max_file} bytes",
                    )
                member_count += 1
                if member_count > limits.max_members:
                    raise BundleError(
                        "too_many_members", f"more than {limits.max_members} members"
                    )
                expanded_total += st.st_size
                if expanded_total > limits.max_expanded:
                    raise BundleError(
                        "limits_expanded",
                        f"expanded size exceeds {limits.max_expanded} bytes",
                    )
                is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
                files.append((relpath, entry.path, st.st_size, is_exec))
            elif stat.S_ISFIFO(mode):
                skipped.append((relpath, "fifo"))
            elif stat.S_ISSOCK(mode):
                skipped.append((relpath, "socket"))
            elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                skipped.append((relpath, "device"))
            else:
                skipped.append((relpath, "special"))

    return dirs, files, skipped, expanded_total


def _bundle_build_once(
    root: pathlib.Path,
    out_path: pathlib.Path,
    compiled_excludes,
    limits: BundleLimits,
) -> BundleInfo:
    dirs, files, skipped, expanded_total = _bundle_gather_tree(
        root, limits, compiled_excludes
    )

    entries = [(d, "dir", None) for d in dirs] + [(f[0], "file", f) for f in files]
    entries.sort(key=lambda e: e[0])

    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out_dir), prefix=".bundle-", suffix=".tmp")
    tmp_path = pathlib.Path(tmp_name)

    try:
        file_records: list = []
        with os.fdopen(fd, "wb") as raw_out:
            limited = _bundle_LimitedWriter(raw_out, limits.max_compressed)
            # gz is wrapped in its own `with` so it is always closed (and its
            # trailer flushed) deterministically, even if an exception is
            # raised while writing tar entries -- otherwise, on the error
            # path, gz would only get closed later by the garbage collector,
            # by which point raw_out may already be closed.
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=limited, mtime=0, compresslevel=1
            ) as gz:
                counting = _bundle_CountingHasher(gz)
                with tarfile.open(
                    mode="w|", fileobj=counting, format=tarfile.PAX_FORMAT
                ) as tar:
                    for relpath, kind, meta in entries:
                        if kind == "dir":
                            info = tarfile.TarInfo(name=relpath)
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            info.uid = 0
                            info.gid = 0
                            info.uname = ""
                            info.gname = ""
                            info.mtime = 0
                            info.size = 0
                            tar.addfile(info)
                        else:
                            _, abspath, _size, is_exec = meta
                            data = _bundle_read_file_checked(pathlib.Path(abspath), relpath)
                            info = tarfile.TarInfo(name=relpath)
                            info.type = tarfile.REGTYPE
                            info.mode = 0o755 if is_exec else 0o644
                            info.uid = 0
                            info.gid = 0
                            info.uname = ""
                            info.gname = ""
                            info.mtime = 0
                            info.size = len(data)
                            tar.addfile(info, io.BytesIO(data))
                            file_records.append(
                                (relpath, len(data), hashlib.sha256(data).hexdigest())
                            )
            raw_out.flush()
            os.fsync(raw_out.fileno())

        compressed_bytes = os.path.getsize(tmp_path)
        if compressed_bytes > limits.max_compressed:
            raise BundleError(
                "too_large", f"compressed bundle exceeds {limits.max_compressed} bytes"
            )

        os.rename(tmp_path, out_path)
        dir_fd = os.open(str(out_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        digest = "sha256:" + counting.sha256.hexdigest()
        return BundleInfo(
            digest=digest,
            compressed_bytes=compressed_bytes,
            expanded_bytes=expanded_total,
            members=len(entries),
            format_version=BUNDLE_FORMAT_VERSION,
            files=file_records,
            skipped=skipped,
        )
    except BaseException:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def bundle_build(
    root,
    out_path,
    *,
    extra_excludes=(),
    limits: BundleLimits = BundleLimits(),
    max_retries: int = 2,
) -> BundleInfo:
    root = pathlib.Path(root)
    out_path = pathlib.Path(out_path)
    if not root.is_dir():
        raise BundleError("bad_format", f"{root} is not a directory")

    raw_excludes = (
        list(BUNDLE_DEFAULT_EXCLUDES)
        + list(BUNDLE_SECRET_EXCLUDES)
        + _bundle_load_fqignore(root)
        + list(extra_excludes)
    )
    compiled_excludes = _bundle_compile_excludes(raw_excludes)

    attempt = 0
    while True:
        try:
            return _bundle_build_once(root, out_path, compiled_excludes, limits)
        except _bundle_ChangedDuringRead:
            attempt += 1
            if attempt > max_retries:
                raise BundleError(
                    "changed_during_read",
                    f"source tree changed during read after {max_retries} retries",
                )
            continue


# --------------------------------------------------------------------------
# validate / extract (shared streaming core)
# --------------------------------------------------------------------------


class _bundle_CountingReader:
    """Readable wrapper: counts bytes actually read and enforces a cap."""

    def __init__(self, inner, limit: int) -> None:
        self._inner = inner
        self._limit = limit
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._inner.read(size)
        self.count += len(chunk)
        if self.count > self._limit:
            raise BundleError(
                "too_large", f"compressed size exceeds {self._limit} bytes"
            )
        return chunk


class _bundle_ExpandGuard:
    """Readable wrapper around the gzip stream: hashes every decompressed
    byte (so the digest matches the build side exactly, including trailing
    tar padding) and aborts early once a generous expanded-byte budget is
    exceeded, so a gzip bomb fails fast."""

    def __init__(self, inner, hasher, budget: int) -> None:
        self._inner = inner
        self._hasher = hasher
        self._budget = budget
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._inner.read(size)
        if chunk:
            self._hasher.update(chunk)
            self.count += len(chunk)
            if self.count > self._budget:
                raise BundleError(
                    "limits_expanded",
                    f"decompressed stream exceeds {self._budget} bytes",
                )
        return chunk


def _bundle_validate_name(name: str, limits: BundleLimits) -> None:
    if not name:
        raise BundleError("path_unsafe", "empty member name")
    if "\\" in name:
        raise BundleError("path_unsafe", f"backslash in path: {name!r}")
    if "\x00" in name:
        raise BundleError("path_unsafe", f"NUL byte in path: {name!r}")
    if any(ch in _BUNDLE_CONTROL_CHARS for ch in name):
        raise BundleError("path_unsafe", f"control character in path: {name!r}")
    if name.startswith("/"):
        raise BundleError("path_unsafe", f"absolute path: {name!r}")
    parts = name.split("/")
    if any(part == "" for part in parts):
        raise BundleError("path_unsafe", f"empty path component: {name!r}")
    if any(part == "." for part in parts):
        raise BundleError("path_unsafe", f"'.' path component: {name!r}")
    if any(part == ".." for part in parts):
        raise BundleError("path_unsafe", f"'..' path component: {name!r}")
    if len(name) > limits.max_path_len:
        raise BundleError("path_unsafe", f"path too long: {name!r}")
    if len(parts) > limits.max_depth:
        raise BundleError("path_unsafe", f"path too deep: {name!r}")


def _bundle_validate_type(tarinfo: tarfile.TarInfo) -> None:
    if tarinfo.issym():
        raise BundleError("link_rejected", f"symlink member: {tarinfo.name!r}")
    if tarinfo.islnk():
        raise BundleError("link_rejected", f"hardlink member: {tarinfo.name!r}")
    if tarinfo.issparse():
        raise BundleError("special_file_rejected", f"sparse member: {tarinfo.name!r}")
    if tarinfo.isdev():
        raise BundleError(
            "special_file_rejected", f"device or fifo member: {tarinfo.name!r}"
        )
    if not (tarinfo.isreg() or tarinfo.isdir()):
        raise BundleError(
            "special_file_rejected", f"unsupported member type: {tarinfo.name!r}"
        )


def _bundle_validate_metadata(tarinfo: tarfile.TarInfo) -> None:
    if tarinfo.uid != 0 or tarinfo.gid != 0:
        raise BundleError(
            "forbidden_metadata", f"non-zero uid/gid on {tarinfo.name!r}"
        )
    if tarinfo.uname or tarinfo.gname:
        raise BundleError(
            "forbidden_metadata", f"non-empty uname/gname on {tarinfo.name!r}"
        )
    if tarinfo.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise BundleError(
            "forbidden_metadata",
            f"setuid/setgid/sticky bit set on {tarinfo.name!r}",
        )


def _bundle_write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _bundle_extract_ensure_parents(extract_root: pathlib.Path, relpath: str) -> None:
    parts = relpath.split("/")[:-1]
    current = extract_root
    for part in parts:
        current = current / part
        try:
            os.mkdir(current, 0o755)
        except FileExistsError:
            pass
        os.chmod(current, 0o755)


def _bundle_extract_dir(extract_root: pathlib.Path, relpath: str) -> None:
    _bundle_extract_ensure_parents(extract_root, relpath)
    full = extract_root / relpath
    try:
        os.mkdir(full, 0o755)
    except FileExistsError:
        pass
    os.chmod(full, 0o755)


def _bundle_extract_open(extract_root: pathlib.Path, relpath: str, mode: int) -> int:
    _bundle_extract_ensure_parents(extract_root, relpath)
    full = extract_root / relpath
    is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    file_mode = 0o755 if is_exec else 0o644
    fd = os.open(
        str(full), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, file_mode
    )
    os.fchmod(fd, file_mode)
    return fd


def _bundle_run_stream(
    path, limits: BundleLimits, extract_root: pathlib.Path | None = None
) -> BundleInfo:
    """Stream-validate a bundle file, optionally extracting it as it goes.

    Never reads the whole (compressed or decompressed) file into memory:
    everything is processed in bounded chunks. Never trusts client-claimed
    sizes -- every limit is enforced against bytes actually observed on the
    wire.
    """
    raw_in = open(path, "rb")
    try:
        counted_in = _bundle_CountingReader(raw_in, limits.max_compressed)
        try:
            gz = gzip.GzipFile(fileobj=counted_in, mode="rb")
        except OSError as exc:
            raise BundleError("bad_format", f"not a valid gzip stream: {exc}") from exc

        hasher = hashlib.sha256()
        # Generous per-member header/padding overhead budget on top of the
        # real expanded-bytes cap, so a gzip bomb is rejected long before it
        # is fully decompressed, without being so tight it misfires on a
        # legitimately large, well-formed archive.
        budget = limits.max_expanded + (limits.max_members * _BUNDLE_TAR_BLOCKSIZE) + 10240
        guard = _bundle_ExpandGuard(gz, hasher, budget)

        member_count = 0
        expanded_total = 0
        all_paths: set = set()
        file_paths: set = set()
        file_records: list = []

        # Opening the tar stream, iterating its members, and draining any
        # trailing padding are all wrapped in one try/except: a truncated or
        # otherwise corrupt gzip/tar stream can raise EOFError/OSError/
        # tarfile.TarError from any of tarfile.open() itself (which primes
        # its first header read immediately), from member iteration, or
        # from a later read -- all map to the same "bad_format" outcome.
        # gz is closed deterministically via `with`, even on the error path,
        # so it never gets finalized later against an already-closed
        # underlying file.
        try:
            with gz:
                tar = tarfile.open(mode="r|", fileobj=guard)
                with tar:
                    for tarinfo in tar:
                        member_count += 1
                        if member_count > limits.max_members:
                            raise BundleError(
                                "too_many_members",
                                f"more than {limits.max_members} members",
                            )

                        name = tarinfo.name
                        _bundle_validate_name(name, limits)
                        _bundle_validate_type(tarinfo)
                        _bundle_validate_metadata(tarinfo)

                        if name in all_paths:
                            raise BundleError(
                                "duplicate_path", f"duplicate member: {name!r}"
                            )
                        ancestor = ""
                        for part in name.split("/")[:-1]:
                            ancestor = f"{ancestor}/{part}" if ancestor else part
                            if ancestor in file_paths:
                                raise BundleError(
                                    "duplicate_path",
                                    f"{name!r} conflicts with file {ancestor!r}",
                                )
                        all_paths.add(name)

                        if tarinfo.isdir():
                            if extract_root is not None:
                                _bundle_extract_dir(extract_root, name)
                            continue

                        file_paths.add(name)
                        if tarinfo.size > limits.max_file:
                            raise BundleError(
                                "file_too_large",
                                f"{name} ({tarinfo.size} bytes) exceeds {limits.max_file} bytes",
                            )

                        src = tar.extractfile(tarinfo)
                        file_hash = hashlib.sha256()
                        dest_fd = None
                        if extract_root is not None:
                            dest_fd = _bundle_extract_open(extract_root, name, tarinfo.mode)
                        try:
                            remaining = tarinfo.size
                            while remaining > 0:
                                chunk = src.read(min(_BUNDLE_READ_CHUNK, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                                file_hash.update(chunk)
                                expanded_total += len(chunk)
                                if expanded_total > limits.max_expanded:
                                    raise BundleError(
                                        "limits_expanded",
                                        f"expanded size exceeds {limits.max_expanded} bytes",
                                    )
                                if dest_fd is not None:
                                    _bundle_write_all(dest_fd, chunk)
                        finally:
                            if dest_fd is not None:
                                os.fsync(dest_fd)
                                os.close(dest_fd)
                        if remaining > 0:
                            raise BundleError(
                                "bad_format", f"truncated member data: {name!r}"
                            )
                        file_records.append((name, tarinfo.size, file_hash.hexdigest()))

                    # Drain any bytes tarfile's own member iteration never asked
                    # for (the RECORDSIZE padding tarfile.close() writes at
                    # build time), so the digest is over exactly the same
                    # uncompressed byte stream the client hashed.
                    while True:
                        chunk = guard.read(_BUNDLE_READ_CHUNK)
                        if not chunk:
                            break
        except tarfile.TarError as exc:
            raise BundleError("bad_format", f"corrupt tar stream: {exc}") from exc
        except EOFError as exc:
            raise BundleError("bad_format", f"truncated gzip stream: {exc}") from exc
        except OSError as exc:
            raise BundleError("bad_format", f"corrupt archive: {exc}") from exc

        digest = "sha256:" + hasher.hexdigest()
        return BundleInfo(
            digest=digest,
            compressed_bytes=counted_in.count,
            expanded_bytes=expanded_total,
            members=member_count,
            format_version=BUNDLE_FORMAT_VERSION,
            files=file_records,
            skipped=[],
        )
    finally:
        raw_in.close()


def bundle_validate(
    path, *, expected_digest: str | None = None, limits: BundleLimits = BundleLimits()
) -> BundleInfo:
    info = _bundle_run_stream(path, limits, extract_root=None)
    if expected_digest is not None and info.digest != expected_digest:
        raise BundleError(
            "digest_mismatch", f"expected {expected_digest}, got {info.digest}"
        )
    return info


def bundle_extract(
    path,
    dest_dir,
    *,
    expected_digest: str,
    limits: BundleLimits = BundleLimits(),
) -> BundleInfo:
    dest_dir = pathlib.Path(dest_dir)
    if os.path.lexists(dest_dir):
        raise BundleError("dest_exists", f"destination already exists: {dest_dir}")

    parent = dest_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = pathlib.Path(
        tempfile.mkdtemp(dir=str(parent), prefix=".bundle-extract-")
    )
    try:
        os.chmod(tmp_dir, 0o700)
        info = _bundle_run_stream(path, limits, extract_root=tmp_dir)
        if info.digest != expected_digest:
            raise BundleError(
                "digest_mismatch", f"expected {expected_digest}, got {info.digest}"
            )
        os.rename(str(tmp_dir), str(dest_dir))
        return info
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
