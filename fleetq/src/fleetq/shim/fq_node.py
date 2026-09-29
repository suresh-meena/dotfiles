"""fq-node: the one-shot node shim (§2.3, §2.6–2.8).

It is not a daemon. fleetqd invokes it through ``fleetctl exec``; it does one
bounded operation, prints one JSON object, and exits. The only long-lived
process is each job's own systemd user service, which exists for that job.

Built into the single file ``fq-node`` by concatenating ``fleetq/bundles.py``
and this file (``scripts/build``), so it depends on the stdlib only.

Control root layout (canonical, on local healthy disk, pinned at enrollment)::

    <root>/enrollment.json         fleet id, node id, pinned root
    <root>/fence.json              highest controller epoch accepted
    <root>/.lock                   node-wide flock (bounded)
    <root>/alloc/<gpu-uuid>.json   allocation markers
    <root>/cache/<sha>.tar.gz      verified bundles
    <root>/inbox/<attempt>/        staging drop (manifest.json, bundle)
    <root>/attempts/<attempt>/
        manifest.json              immutable spec for this attempt
        code/                      extracted snapshot (never the cache itself)
        facts/                     one file per fact, atomically written:
            staged start_requested runner_entered payload_started
            cancel stopped released
        result.json                payload exit evidence (bound to nonce)
        service_result.json        systemd's view, from ExecStopPost
        replay/                    replayed runners/finalizers: observed, never overwriting
        stdout.log stderr.log

A payload is entered at most once per attempt: ``facts/runner_entered`` is
created with O_EXCL under the node lock, and a runner that finds it already
present records a replay observation and exits without running anything.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:  # concatenated build: bundles.py precedes this file
    bundle_extract  # type: ignore[used-before-def]  # noqa: B018
except NameError:  # pragma: no cover - import path in tests and development
    from fleetq.bundles import BundleError, BundleLimits, bundle_extract  # type: ignore

SHIM_VERSION = "1"
LOCK_TIMEOUT_S = 20
NVSMI_TIMEOUT_S = float(os.environ.get("FQ_NODE_NVSMI_TIMEOUT", "10"))
SYSTEMCTL_TIMEOUT_S = 20
PROBE_TIMEOUT_S = 5
MAX_OUTSTANDING_PROBES = 4
MAX_GPU_PROCESS_ROWS = 128
REMOTE_CACHE_GRACE_S = 7 * 24 * 60 * 60
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_PROC_STAT_BYTES = 8192
MAX_PROC_CGROUP_BYTES = 2048
GATE_SAMPLES = 3
GATE_INTERVAL_S = 1.0
DEFAULT_GPU_IDLE = {"max_mem_mib": 0, "max_util": 5}
RESERVED_ENV = ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "NVIDIA_VISIBLE_DEVICES")


class ShimError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


_HOME_CONFIG_ROOTS = {".ssh", ".gnupg", ".config", ".local", ".cache"}
_PROTECTED_SYSTEM_ROOTS = ("/etc", "/usr", "/var", "/root", "/boot", "/proc", "/sys", "/dev",
                           "/bin", "/sbin", "/lib", "/lib64", "/opt", "/run")


def _is_protected_system_root(path: str | Path) -> bool:
    """Whether a canonical control root is a system-owned directory tree."""
    real = os.path.realpath(path)
    return any(real == prefix or real.startswith(prefix + "/") for prefix in _PROTECTED_SYSTEM_ROOTS)


def _is_home_config_root(path: str | Path, home: str | Path) -> bool:
    """Whether path is one of HOME's config roots or anything inside one."""
    try:
        relative = Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(home)))
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] in _HOME_CONFIG_ROOTS)


# ---- durable file helpers ---------------------------------------------------------

def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: dict[str, Any] | str) -> None:
    """Write temp, fsync, rename, fsync the parent (§2.6)."""
    body = data if isinstance(data, str) else json.dumps(data, sort_keys=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def create_exclusive(path: Path, data: dict[str, Any]) -> bool:
    """Create ``path`` only if absent (the claim primitive). True if we created it."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(tmp, path)          # atomic create-if-absent that also survives a crash
    except FileExistsError:
        os.unlink(tmp)
        return False
    os.unlink(tmp)
    _fsync_dir(path.parent)
    return True


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        return {"_unreadable": str(exc)}


def boot_id() -> str:
    override = os.environ.get("FQ_NODE_BOOT_ID")      # tests only
    if override:
        return override
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def gpu_process_identity(pid: int, *, current_boot_id: str, root: ControlRoot | None = None,
                        fleet_id: str | None = None) -> dict[str, Any]:
    """Capture a bounded PID identity suitable for later attribution.

    A process is never called fleetq-owned from its PID alone. The host start
    tick, boot ID, and cgroup path travel together; unreadable or changing
    procfs evidence remains ownership_unknown.
    """
    unknown = {"pid": pid, "boot_id": current_boot_id, "process_start_ticks": None,
               "cgroup_path": None, "fleetq_attempt_id": None,
               "attribution": "ownership_unknown"}
    if current_boot_id in ("", "unknown"):
        return {**unknown, "reason": "boot_id_unavailable"}
    if pid <= 0:
        return {**unknown, "reason": "invalid_pid"}
    proc = Path("/proc") / str(pid)
    try:
        with (proc / "stat").open("rb") as handle:
            before = handle.read(MAX_PROC_STAT_BYTES + 1)
        if len(before) > MAX_PROC_STAT_BYTES:
            return {**unknown, "reason": "stat_too_large"}
        # comm is parenthesized and may itself contain ')'; parse fields after
        # the final delimiter. starttime is field 22 (index 19 after state).
        close = before.rfind(b")")
        fields = before[close + 1:].split() if close >= 0 else []
        start_ticks = int(fields[19])
        if start_ticks <= 0:
            return {**unknown, "reason": "invalid_start_time"}
        with (proc / "cgroup").open("rb") as handle:
            cgroup_raw = handle.read(MAX_PROC_CGROUP_BYTES + 1)
        if len(cgroup_raw) > MAX_PROC_CGROUP_BYTES:
            return {**unknown, "reason": "cgroup_too_large"}
        with (proc / "stat").open("rb") as handle:
            after = handle.read(MAX_PROC_STAT_BYTES + 1)
        if len(after) > MAX_PROC_STAT_BYTES:
            return {**unknown, "reason": "stat_too_large"}
        after_close = after.rfind(b")")
        after_fields = after[after_close + 1:].split() if after_close >= 0 else []
        if int(after_fields[19]) != start_ticks:
            return {**unknown, "reason": "pid_reused"}
        cgroups = cgroup_raw.decode("utf-8", "strict").splitlines()
        paths = []
        for line in cgroups:
            parts = line.split(":", 2)
            if len(parts) != 3 or not parts[2].startswith("/"):
                return {**unknown, "reason": "invalid_cgroup_record"}
            paths.append(parts[2])
        if not paths:
            return {**unknown, "reason": "cgroup_unavailable"}
        cgroup_path = ";".join(paths)
        attempt_id = None
        has_fq_attempt_marker = False
        for path in paths:
            for component in path.split("/"):
                if component.startswith("fq-att_") and component.endswith(".service"):
                    has_fq_attempt_marker = True
                    candidate = component[3:-8]
                    if (candidate.startswith("att_") and len(candidate) == 28
                            and all(ch in "0123456789abcdef" for ch in candidate[4:])):
                        if attempt_id is not None and attempt_id != candidate:
                            return {**unknown, "reason": "multiple_fleetq_cgroups"}
                        attempt_id = candidate
        if attempt_id is not None:
            attempt_dir = root.attempt_dir(attempt_id) if root is not None else None
            manifest = read_json(attempt_dir / "manifest.json") if attempt_dir is not None else None
            started = fact(attempt_dir, "start_requested") if attempt_dir is not None else None
            if (manifest is None or "_unreadable" in manifest or manifest.get("attempt_id") != attempt_id
                    or manifest.get("fleet_id") != fleet_id or started is None
                    or started.get("boot_id") != current_boot_id):
                return {**unknown, "cgroup_path": cgroup_path, "reason": "unknown_fleetq_attempt"}
        elif has_fq_attempt_marker:
            return {**unknown, "cgroup_path": cgroup_path, "reason": "malformed_fleetq_cgroup"}
        return {"pid": pid, "boot_id": current_boot_id, "process_start_ticks": start_ticks,
                "cgroup_path": cgroup_path, "fleetq_attempt_id": attempt_id,
                "attribution": "fleetq_attempt" if attempt_id else "foreign"}
    except FileNotFoundError:
        return {**unknown, "reason": "process_exited"}
    except (OSError, UnicodeError, ValueError, IndexError):
        return {**unknown, "reason": "proc_identity_unavailable"}


# ---- bounded subprocesses -----------------------------------------------------------

def run_bounded(argv: list[str], *, timeout: float, env: dict[str, str] | None = None,
                cwd: str | None = None) -> tuple[int | None, str, str]:
    """Run a helper command with a hard deadline; never wait past it.

    On timeout, kill it and stop waiting. A process stuck in uninterruptible
    I/O may outlive the kill (§2.7), so the shim never blocks on reaping it.
    Returns ``(rc, stdout, stderr)``, with rc None on timeout.
    """
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                env=env, cwd=cwd, start_new_session=True, text=True)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return None, "", f"{argv[0]}: timed out after {timeout}s"


# ---- control root, enrollment, lock -------------------------------------------------

class ControlRoot:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @property
    def enrollment(self) -> dict[str, Any] | None:
        return read_json(self.root / "enrollment.json")

    def attempt_dir(self, attempt_id: str) -> Path:
        if not attempt_id.startswith("att_") or "/" in attempt_id or len(attempt_id) > 64:
            raise ShimError("bad_attempt_id", f"invalid attempt id {attempt_id!r}")
        attempts = self.root / "attempts"
        if not _is_private_owned_dir(attempts):
            raise ShimError("unsafe_attempt_dir", "attempts root is not a private owned directory")
        path = attempts / attempt_id
        if path.is_symlink() or (path.exists() and not _is_private_owned_dir(path)):
            raise ShimError("unsafe_attempt_dir", "attempt path is not a private owned directory")
        return path

    def lock(self) -> "NodeLock":
        return NodeLock(self.root / ".lock")

    def require_enrolled(self, fleet_id: str | None = None) -> dict[str, Any]:
        if not os.path.isabs(self.root) or ".." in self.root.parts:
            raise ShimError("root_mismatch", "control root must be an absolute path without parent traversal")
        if _is_protected_system_root(self.root):
            raise ShimError("unsafe_root", "control root must not be inside a protected system directory")
        home = os.path.expanduser("~")
        if _is_home_config_root(self.root, home):
            raise ShimError("unsafe_root", "control root must not be inside a HOME config directory")
        cursor = Path("/")
        for component in self.root.parts[1:]:
            cursor /= component
            if cursor.is_symlink():
                raise ShimError("root_mismatch", "control root path must not contain symlink aliases")
        enr = self.enrollment
        if not enr or "_unreadable" in enr:
            raise ShimError("not_enrolled", f"{self.root} is not an enrolled control root")
        if os.path.realpath(self.root) != enr.get("control_root"):
            raise ShimError("root_mismatch", "control root path differs from the pinned enrollment path")
        st = self.root.stat()
        if not stat.S_ISDIR(st.st_mode):
            raise ShimError("unsafe_root", "control root is not a directory")
        if fleet_id is not None and enr.get("fleet_id") != fleet_id:
            raise ShimError("fleet_mismatch", "this node is enrolled to a different fleet")
        return enr

    def highest_epoch(self) -> int:
        fence = read_json(self.root / "fence.json") or {}
        return int(fence.get("highest_epoch", 0))


def _safe_rmtree(root: ControlRoot, target: Path) -> None:
    """Remove a private subtree through pinned directory descriptors.

    Every path component is opened relative to its already-open parent with
    O_NOFOLLOW.  In particular, rmtree receives the leaf name and pinned
    parent fd, so replacing a checked parent with a symlink cannot redirect
    cleanup elsewhere.
    """
    if (not getattr(shutil.rmtree, "avoids_symlink_attacks", False)
            or os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")):
        raise ShimError("unsafe_cleanup", "safe descriptor-relative cleanup is unavailable")
    base = Path(os.path.abspath(root.root))
    absolute = Path(os.path.abspath(target))
    try:
        relative = absolute.relative_to(base)
    except ValueError as exc:
        raise ShimError("unsafe_cleanup", "cleanup path is outside the control root") from exc
    if not relative.parts:
        raise ShimError("unsafe_cleanup", "refusing to remove the control root")
    enrollment = root.enrollment
    if (not enrollment or enrollment.get("control_root") != str(base)
            or not base.is_absolute() or len(base.parts) < 2
            or _is_protected_system_root(base)
            or _is_home_config_root(base, os.path.expanduser("~"))):
        raise ShimError("unsafe_cleanup", "cleanup root is not the enrolled control root")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fds: list[int] = []
    try:
        fd = os.open("/", flags)
        fds.append(fd)
        # Pin the whole path from /; the control root itself must be private.
        for index, component in enumerate(base.parts[1:]):
            fd = os.open(component, flags, dir_fd=fd)
            fds.append(fd)
            if index == len(base.parts[1:]) - 1 and not _private_owned_fd(fd):
                raise ShimError("unsafe_cleanup", "control root is not a private owned directory")
        # Keep the enrolled root descriptor and open each parent below it.
        for component in relative.parts[:-1]:
            fd = os.open(component, flags, dir_fd=fd)
            fds.append(fd)
            if not _private_owned_fd(fd):
                raise ShimError("unsafe_cleanup", "cleanup path has an unsafe parent")
        parent_fd = fds[-1]
        leaf = relative.parts[-1]
        try:
            leaf_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(leaf_stat.st_mode) or leaf_stat.st_uid != os.getuid() or leaf_stat.st_mode & 0o022:
            raise ShimError("unsafe_cleanup", "cleanup target is not a private owned directory")
        shutil.rmtree(leaf, dir_fd=parent_fd)
    except ShimError:
        raise
    except OSError as exc:
        raise ShimError("unsafe_cleanup", f"cannot safely remove cleanup target: {exc}") from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _private_owned_fd(fd: int) -> bool:
    st = os.fstat(fd)
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not (st.st_mode & 0o022)


def _is_private_owned_dir(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not (st.st_mode & 0o022)


def _valid_install_bootstrap(root: Path) -> bool:
    """Accept only onboard's pre-enrollment releases/ plus bin/fq-node layout."""
    if not _is_private_owned_dir(root):
        return False
    try:
        if set(os.listdir(root)) != {"bin", "releases"}:
            return False
        bin_dir, releases = root / "bin", root / "releases"
        if not _is_private_owned_dir(bin_dir) or not _is_private_owned_dir(releases):
            return False
        if set(os.listdir(bin_dir)) != {"fq-node"}:
            return False
        link = bin_dir / "fq-node"
        if not link.is_symlink():
            return False
        target = os.readlink(link)
        parts = Path(target).parts
        if (len(parts) != 4 or parts[0] != ".." or parts[1] != "releases"
                or len(parts[2]) != 16 or any(ch not in "0123456789abcdef" for ch in parts[2])
                or parts[3] != "fq-node"):
            return False
        release_dirs = os.listdir(releases)
        if not release_dirs or any(len(name) != 16 or any(ch not in "0123456789abcdef" for ch in name)
                                   for name in release_dirs):
            return False
        for name in release_dirs:
            release = releases / name
            if not _is_private_owned_dir(release) or set(os.listdir(release)) != {"fq-node"}:
                return False
            executable = release / "fq-node"
            st = executable.lstat()
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or not st.st_mode & 0o100 or st.st_mode & 0o022):
                return False
        return (releases / parts[2] / "fq-node").is_file()
    except OSError:
        return False

class NodeLock:
    """A bounded node-wide flock: waits at most LOCK_TIMEOUT_S, never forever."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "NodeLock":
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise ShimError("lock_timeout", "node control lock is busy")
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


def fact(adir: Path, name: str) -> dict[str, Any] | None:
    return read_json(adir / "facts" / name)


def set_fact(adir: Path, name: str, data: dict[str, Any] | None = None) -> None:
    write_atomic(adir / "facts" / name, {"ts": time.time(), "boot_id": boot_id(), **(data or {})})


# ---- systemd user manager ---------------------------------------------------------

def user_env() -> dict[str, str]:
    """Resolve and validate the user manager's runtime dir (§2.6).

    Exporting strings alone doesn't create a working user manager, so the
    runtime dir and bus socket are checked, not assumed.
    """
    uid = os.getuid()
    runtime = f"/run/user/{uid}"
    env = {"PATH": os.environ.get("FQ_NODE_PATH", "/usr/local/bin:/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/"), "LANG": "C.UTF-8"}
    override = os.environ.get("FQ_NODE_RUNTIME_DIR")          # tests only
    runtime = override or runtime
    try:
        st = os.stat(runtime)
    except FileNotFoundError:
        raise ShimError("no_user_runtime", f"{runtime} does not exist: is linger enabled?")
    if st.st_uid != uid and not override:
        raise ShimError("bad_user_runtime", f"{runtime} is not owned by this user")
    env["XDG_RUNTIME_DIR"] = runtime
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return env


def unit_name(attempt_id: str) -> str:
    return f"fq-{attempt_id}.service"


def unit_show(attempt_id: str) -> dict[str, str]:
    rc, out, err = run_bounded(
        ["systemctl", "--user", "show", unit_name(attempt_id),
         "-p", "LoadState,ActiveState,SubState,Result,ExecMainStatus,ExecMainCode,ControlGroup,InvocationID"],
        timeout=SYSTEMCTL_TIMEOUT_S, env={**os.environ, **user_env()})
    if rc is None:
        raise ShimError("systemctl_timeout", "systemctl show timed out")
    props = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    return props


def cgroup_empty(props: dict[str, str]) -> bool | None:
    cg = props.get("ControlGroup") or ""
    if not cg:
        return props.get("ActiveState") in ("inactive", "failed", "")
    base = os.environ.get("FQ_NODE_CGROUP_ROOT", "/sys/fs/cgroup")
    try:
        return Path(base + cg, "cgroup.procs").read_text().strip() == ""
    except FileNotFoundError:
        return True
    except OSError:
        return None


# ---- GPU inventory and the launch gate ------------------------------------------------

def nvidia_snapshot(*, include_process_identity: bool = False, root: ControlRoot | None = None,
                    fleet_id: str | None = None) -> dict[str, Any]:
    """One bounded nvidia-smi reading. Unsupported or failed means *unknown*, never zero (§2.2)."""
    rc, out, err = run_bounded(
        ["nvidia-smi", "--query-gpu=uuid,index,pci.bus_id,name,memory.total,memory.used,utilization.gpu,"
         "compute_mode,mig.mode.current", "--format=csv,noheader,nounits"], timeout=NVSMI_TIMEOUT_S)
    if rc is None:
        return {"ok": False, "reason": "nvidia_smi_hung"}
    if rc != 0:
        return {"ok": False, "reason": "nvidia_smi_failed", "stderr": err[-500:]}
    gpus = {}
    for line in out.strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) != 9 or not cols[0].startswith("GPU-") or cols[0] in gpus:
            return {"ok": False, "reason": "nvidia_smi_parse_error", "line": line}

        def num(v):
            try:
                return float(v)
            except ValueError:
                return None
        gpus[cols[0]] = {"uuid": cols[0], "index": num(cols[1]), "pci_bus": cols[2], "model": cols[3],
                         "mem_total_mib": num(cols[4]), "mem_used_mib": num(cols[5]), "util": num(cols[6]),
                         "compute_mode": cols[7], "mig": cols[8]}
    # Cap captured process rows at one beyond our supported bound. `pipefail`
    # preserves nvidia-smi failures while `head` prevents an unbounded process
    # listing or diagnostic stream from filling the response or shim memory.
    rc, out, err = run_bounded(
        ["/bin/bash", "-o", "pipefail", "-c",
         "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>&1"
         f" | head -n {MAX_GPU_PROCESS_ROWS + 1}"], timeout=NVSMI_TIMEOUT_S)
    if rc is None:
        return {"ok": False, "reason": "compute_apps_unavailable"}
    if len(out.splitlines()) > MAX_GPU_PROCESS_ROWS:
        return {"ok": False, "reason": "compute_apps_too_many"}
    if rc != 0:
        return {"ok": False, "reason": "compute_apps_unavailable"}
    apps: dict[str, list[int]] = {}
    for line in out.strip().splitlines():
        if line.strip() == "No running processes found":
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) != 3 or cols[0] not in gpus or not cols[1].isdigit() or int(cols[1]) <= 0:
            return {"ok": False, "reason": "compute_apps_parse_error", "line": line[:200]}
        apps.setdefault(cols[0], []).append(int(cols[1]))
        if sum(len(items) for items in apps.values()) > MAX_GPU_PROCESS_ROWS:
            return {"ok": False, "reason": "compute_apps_too_many"}
    current_boot_id = boot_id()
    for uuid, info in gpus.items():
        info["pids"] = apps.get(uuid, [])
        if include_process_identity:
            info["processes"] = [gpu_process_identity(pid, current_boot_id=current_boot_id, root=root,
                                                       fleet_id=fleet_id)
                                 for pid in info["pids"]]
    return {"ok": True, "gpus": gpus, "boot_id": current_boot_id, "taken_at": time.time()}


def gate_gpus(uuids: list[str], policy: dict[str, Any]) -> tuple[bool, list[str], str | None]:
    """The immediate launch gate: GATE_SAMPLES readings, all clean (§2.2–2.3)."""
    if not uuids:
        return True, [], None
    limits = {**DEFAULT_GPU_IDLE, **(policy or {})}
    busy: set[str] = set()
    for i in range(GATE_SAMPLES):
        snap = nvidia_snapshot()
        if not snap.get("ok"):
            return False, list(uuids), snap.get("reason")
        for uuid in uuids:
            g = snap["gpus"].get(uuid)
            if g is None:
                return False, [uuid], "gpu_missing"
            if (g.get("mig") or "").lower() == "enabled":
                return False, [uuid], "mig_enabled"
            if (g.get("compute_mode") or "").lower() == "prohibited":
                return False, [uuid], "compute_mode_prohibited"
            baseline = (policy or {}).get("baselines", {}).get(uuid, limits["max_mem_mib"])
            if (g["pids"] or g["mem_used_mib"] is None or g["mem_used_mib"] > baseline
                    or g["util"] is None or g["util"] > limits["max_util"]):
                busy.add(uuid)
        if busy:
            return False, sorted(busy), "gpu_busy"
        if i < GATE_SAMPLES - 1:
            time.sleep(float(os.environ.get("FQ_NODE_GATE_INTERVAL", GATE_INTERVAL_S)))
    return True, [], None


def mem_available_mib() -> int | None:
    try:
        for line in Path(os.environ.get("FQ_NODE_MEMINFO", "/proc/meminfo")).read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


# ---- commands ---------------------------------------------------------------------------

def cmd_enroll(root: ControlRoot, args) -> dict[str, Any]:
    if not os.path.isabs(root.root) or ".." in root.root.parts:
        raise ShimError("unsafe_root", "control root must be an absolute path without parent traversal")
    cursor = Path("/")
    for component in root.root.parts[1:]:
        cursor /= component
        if cursor.is_symlink():
            raise ShimError("unsafe_root", "control root path must not contain symlink aliases")
    real = os.path.realpath(root.root)
    home = os.path.realpath(os.path.expanduser("~"))
    if (real == "/" or _is_protected_system_root(real) or real == home
            or home.startswith(real.rstrip("/") + "/")
            or _is_home_config_root(real, home)):
        raise ShimError("unsafe_root", "control root must be a dedicated directory, not home or its ancestor")
    existing = root.enrollment
    if existing and "_unreadable" not in existing:
        if existing.get("fleet_id") != args.fleet_id:
            raise ShimError("fleet_mismatch", "already enrolled to a different fleet")
        return {"enrolled": True, "already": True, **existing}
    if root.root.exists():
        st = root.root.lstat()
        empty_owned = (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
                       and not any(root.root.iterdir()) and not (st.st_mode & 0o022))
        if not empty_owned and not _valid_install_bootstrap(root.root):
            raise ShimError("unsafe_root", "existing control root must be empty or contain a verified private install bootstrap")
    for sub in ("", "alloc", "cache", "inbox", "attempts", "probes"):
        (root.root / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    enr = {"fleet_id": args.fleet_id, "node_id": args.node_id, "control_root": real,
           "enrolled_at": time.time(), "shim_version": SHIM_VERSION}
    write_atomic(root.root / "enrollment.json", enr)
    write_atomic(root.root / "fence.json", {"highest_epoch": 0})
    return {"enrolled": True, "already": False, **enr}


def cmd_fence(root: ControlRoot, args) -> dict[str, Any]:
    root.require_enrolled(args.fleet_id)
    with root.lock():
        seen = root.highest_epoch()
        attempts = sorted(p.name for p in (root.root / "attempts").iterdir() if p.is_dir())
        if args.epoch <= seen:
            return {"accepted": False, "highest_epoch_seen": seen, "attempts": attempts}
        write_atomic(root.root / "fence.json", {"highest_epoch": args.epoch, "fenced_at": time.time()})
    return {"accepted": True, "highest_epoch_seen": seen, "attempts": attempts}


def _require_current_epoch(root: ControlRoot, epoch: int) -> None:
    seen = root.highest_epoch()
    if epoch < seen:
        raise ShimError("stale_epoch", "request epoch is below the node's fence",
                        highest_epoch_seen=seen)
    if epoch > seen:
        raise ShimError("future_epoch", "request epoch has not been fenced",
                        highest_epoch_seen=seen)


def cmd_publish_stage(root: ControlRoot, args) -> dict[str, Any]:
    """Atomically publish a completed, uniquely uploaded stage at its epoch."""
    root.require_enrolled(args.fleet_id)
    if not args.stage_id.startswith("att_") or "/" in args.stage_id or len(args.stage_id) > 100:
        raise ShimError("bad_stage_id", "invalid stage id")
    attempt, sep, suffix = args.stage_id.partition("-")
    if not sep or not suffix:
        raise ShimError("bad_stage_id", "invalid stage id")
    inbox_root = root.root / "inbox"
    incoming = inbox_root / f".stage-{args.stage_id}"
    final = inbox_root / attempt
    manifest = read_json(incoming / "manifest.json")
    if not manifest or "_unreadable" in manifest:
        raise ShimError("inbox_missing", "completed stage has no readable manifest")
    if manifest.get("attempt_id") != attempt or manifest.get("fleet_id") != args.fleet_id:
        raise ShimError("manifest_mismatch", "staged manifest does not match this attempt")
    if manifest.get("epoch") != args.epoch:
        raise ShimError("manifest_mismatch", "staged manifest epoch differs from publication epoch")
    with root.lock():
        _require_current_epoch(root, args.epoch)
        if fact(root.attempt_dir(attempt), "cache.released"):
            raise ShimError("cache_pin_released", "this attempt's cache pin was already released")
        backup = inbox_root / f".previous-{args.stage_id}"
        if final.exists():
            os.replace(final, backup)
        try:
            os.replace(incoming, final)
        except BaseException:
            if backup.exists() and not final.exists():
                os.replace(backup, final)
            raise
        _fsync_dir(inbox_root)
    _safe_rmtree(root, backup)
    return {"published": True}


def cmd_prepare(root: ControlRoot, args) -> dict[str, Any]:
    """Move a staged inbox into its attempt dir and extract the snapshot. Idempotent."""
    root.require_enrolled(args.fleet_id)
    adir = root.attempt_dir(args.attempt)
    inbox = root.root / "inbox" / args.attempt
    with root.lock():
        _require_current_epoch(root, args.epoch)
        if fact(adir, "cache.released"):
            raise ShimError("cache_pin_released", "this attempt's cache pin was already released")
        staged_fact = fact(adir, "staged")
        if staged_fact:
            existing = read_json(adir / "manifest.json")
            if (existing and existing.get("epoch") == args.epoch
                    and staged_fact.get("manifest_digest") == existing.get("spec_digest")):
                return {"staged": True, "already": True}
            raise ShimError("manifest_mismatch", "staged attempt does not match requested epoch")
        manifest = read_json(inbox / "manifest.json")
        if not isinstance(manifest, dict) or "_unreadable" in manifest:
            raise ShimError("inbox_missing", "no staged manifest in the inbox")
        if manifest.get("attempt_id") != args.attempt or manifest.get("fleet_id") != args.fleet_id:
            raise ShimError("manifest_mismatch", "inbox manifest does not match this attempt")
        if manifest.get("epoch") != args.epoch:
            raise ShimError("stale_epoch", "manifest epoch does not match the current node fence")
        digest = manifest.get("bundle_digest")
        if digest is not None and (not isinstance(digest, str) or not digest.startswith("sha256:")
                                   or not DIGEST_RE.fullmatch(digest[7:])):
            raise ShimError("bad_digest", "bundle digest must be a canonical sha256 digest")
        adir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (adir / "facts").mkdir(mode=0o700, exist_ok=True)
        (adir / "replay").mkdir(mode=0o700, exist_ok=True)
        # A code tree without its durable staged fact is an interrupted prior
        # prepare. Fail closed rather than recursively deleting under the node lock.
        if (adir / "code").exists():
            raise ShimError("partial_stage", "an incomplete prepared code tree needs operator cleanup")
        write_atomic(adir / "manifest.json", manifest)
    code_tmp: Path | None = None
    if digest:
        cached = root.root / "cache" / (digest.split(":", 1)[1] + ".tar.gz")
        incoming = inbox / "bundle.tar.gz"
        if not cached.exists():
            if not incoming.exists():
                raise ShimError("bundle_missing", "bundle neither cached nor staged")
            os.replace(incoming, cached)
        code = adir / "code"
        if not code.exists():
            code_tmp = adir / f".code-{secrets.token_hex(8)}"
            try:
                bundle_extract(cached, code_tmp, expected_digest=digest)
            except BundleError as exc:
                _safe_rmtree(root, code_tmp)
                raise ShimError("bundle_invalid", f"{exc.code}: {exc.message}")
    try:
        with root.lock():
            _require_current_epoch(root, args.epoch)
            if read_json(adir / "manifest.json") != manifest:
                raise ShimError("manifest_mismatch", "attempt manifest changed during preparation")
            if code_tmp is not None:
                code = adir / "code"
                if code.exists():
                    raise ShimError("partial_stage", "another prepare left an incomplete code tree")
                os.replace(code_tmp, code)
                _fsync_dir(adir)
            set_fact(adir, "staged", {"manifest_digest": manifest.get("spec_digest")})
    except ShimError:
        if code_tmp is not None:
            _safe_rmtree(root, code_tmp)
        raise
    return {"staged": True, "already": False}


def _launch_argv(shim: str, attempt: str, manifest: dict[str, Any], root: ControlRoot) -> list[str]:
    res = manifest["resources"]
    python = sys.executable or "python3"
    props = [
        "Type=exec", "Restart=no", "KillMode=control-group",
        f"MemoryMax={int(res['mem_mb'])}M", "MemorySwapMax=0",
        f"CPUQuota={int(res['cpus']) * 100}%", "TasksMax=4096",
        f"RuntimeMaxSec={int(res['time_s'])}",
        f"TimeoutStopSec={int(manifest.get('kill_grace_s', 60))}",
        f"ExecStopPost={python} {shim} --root {root.root} finalize {attempt}",
    ]
    argv = ["systemd-run", "--user", f"--unit={unit_name(attempt)}", "--no-block", "--quiet"]
    for prop in props:
        argv += ["-p", prop]
    return argv + ["--", python, shim, "--root", str(root.root), "run", attempt]


def cmd_launch(root: ControlRoot, args) -> dict[str, Any]:
    root.require_enrolled(args.fleet_id)
    adir = root.attempt_dir(args.attempt)
    manifest = read_json(adir / "manifest.json")
    if manifest is None or not fact(adir, "staged"):
        return {"result": "never_started", "reason": "not_staged"}
    with root.lock():
        seen = root.highest_epoch()
        if args.epoch != seen:
            return {"result": "never_started", "reason": "stale_epoch" if args.epoch < seen else "future_epoch",
                    "highest_epoch_seen": seen}
        if fact(adir, "cache.released"):
            return {"result": "never_started", "reason": "cache_pin_released"}
        if fact(adir, "cancel") and not fact(adir, "runner_entered"):
            return {"result": "never_started", "reason": "cancelled"}
        if fact(adir, "runner_entered") or fact(adir, "start_requested"):
            # Idempotent replay: never a second start of an entered payload.
            props = unit_show(args.attempt)
            if fact(adir, "runner_entered") or props.get("ActiveState") in ("active", "activating", "deactivating"):
                return {"result": "started", "unit": unit_name(args.attempt), "boot_id": boot_id(), "replay": True}
        # Allocation markers held by another live attempt make the GPU busy.
        gpus = manifest.get("gpus") or []
        for uuid in gpus:
            marker = read_json(root.root / "alloc" / f"{uuid}.json")
            if marker and marker.get("attempt_id") != args.attempt:
                other = root.attempt_dir(marker["attempt_id"])
                if not fact(other, "released"):
                    return {"result": "placement_refused", "reason": "gpu_allocated_by_fleetq", "gpus": [uuid]}
        ok, busy, why = gate_gpus(gpus, manifest.get("gpu_policy") or {})
        if not ok:
            return {"result": "placement_refused", "reason": why, "gpus": busy}
        need = int(manifest["resources"]["mem_mb"]) + int(manifest.get("mem_headroom_mb", 1024))
        avail = mem_available_mib()
        if avail is None or avail < need:
            return {"result": "placement_refused", "reason": "insufficient_physical_memory",
                    "available_mib": avail, "needed_mib": need}
        for uuid in gpus:
            write_atomic(root.root / "alloc" / f"{uuid}.json",
                         {"attempt_id": args.attempt, "epoch": args.epoch, "ts": time.time()})
        nonce = secrets.token_hex(12)
        set_fact(adir, "start_requested", {"nonce": nonce, "epoch": args.epoch})
        rc, out, err = run_bounded(_launch_argv(args.shim_path, args.attempt, manifest, root),
                                   timeout=SYSTEMCTL_TIMEOUT_S, env={**os.environ, **user_env()})
    if rc is None:
        return {"result": "unknown", "reason": "systemd_run_timeout"}
    if rc != 0:
        # systemd refused the transient unit: it never started.
        return {"result": "never_started", "reason": "systemd_run_failed", "stderr": err[-500:]}
    return {"result": "started", "unit": unit_name(args.attempt), "boot_id": boot_id(), "nonce": nonce}


def _job_env(manifest: dict[str, Any]) -> dict[str, str]:
    env = {
        "PATH": manifest.get("path") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/"),
        "USER": os.environ.get("USER", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "FQ_JOB_ID": str(manifest["job_id"]),
        "FQ_ATTEMPT_ID": manifest["attempt_id"],
        "FQ_GPUS": ",".join(manifest.get("gpus") or []),
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        # UUIDs, never indices: index order differs between tools (§2.1).
        "CUDA_VISIBLE_DEVICES": ",".join(manifest.get("gpus") or []),
    }
    if manifest.get("array_index") is not None:
        env["FQ_ARRAY_TASK_ID"] = str(manifest["array_index"])
    if manifest.get("attempt_n"):
        env["FQ_ATTEMPT"] = str(manifest["attempt_n"])
    if manifest.get("checkpoint_dir"):
        env["FQ_CHECKPOINT_DIR"] = manifest["checkpoint_dir"]
        env["FQ_RESUMED"] = "1" if manifest.get("resumed") else "0"
    for key, value in (manifest.get("env") or {}).items():
        if key not in RESERVED_ENV and not key.startswith(("FQ_", "SLURM_")):
            env[key] = value
    return env


def _job_command(manifest: dict[str, Any]) -> list[str]:
    cmd = manifest["command"]
    setup = manifest.get("setup")
    if "argv" in cmd:
        payload = list(cmd["argv"])
    elif "script" in cmd:
        payload = ["/bin/bash", cmd["script"]["path"], *cmd["script"]["args"]]
    else:
        payload = ["/bin/bash", "-c", cmd["wrap"]]
    if setup:
        # Setup is intentional code; argv keeps its argument boundaries via "$@".
        return ["/bin/bash", "-c", f"{setup}\nexec \"$@\"", "fq-setup", *payload]
    return payload


def cmd_run(root: ControlRoot, args) -> int:
    """The per-attempt runner inside the unit. Enters the payload at most once."""
    adir = root.attempt_dir(args.attempt)
    manifest = read_json(adir / "manifest.json")
    if manifest is None:
        return 70
    invocation = os.environ.get("INVOCATION_ID", secrets.token_hex(8))
    with root.lock():
        if fact(adir, "cancel"):
            create_exclusive(adir / "replay" / f"cancelled-{invocation}.json", {"ts": time.time()})
            return 0
        claimed = create_exclusive(adir / "facts" / "runner_entered",
                                   {"invocation": invocation, "ts": time.time(), "boot_id": boot_id()})
    if not claimed:
        # A replayed unit (or scheduler restart) must not rerun the payload.
        create_exclusive(adir / "replay" / f"runner-{invocation}.json", {"ts": time.time(), "boot_id": boot_id()})
        return 0
    cwd = manifest.get("in_place") or str(adir / "code" / (manifest.get("subdir") or "."))
    # One directory per job that outlives its attempts on this node, so a job
    # resubmitted after TIMEOUT or preemption can resume where it left off.
    ckpt = root.root / "jobs" / str(manifest["job_id"]) / "checkpoint"
    manifest["resumed"] = ckpt.is_dir() and any(ckpt.iterdir())
    ckpt.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest["checkpoint_dir"] = str(ckpt)
    set_fact(adir, "payload_started", {"invocation": invocation})
    with open(adir / "stdout.log", "ab") as out, open(adir / "stderr.log", "ab") as err:
        try:
            child = subprocess.Popen(_job_command(manifest), cwd=cwd, env=_job_env(manifest),
                                     stdout=out, stderr=err, stdin=subprocess.DEVNULL)
        except OSError as exc:
            write_atomic(adir / "result.json", {"invocation": invocation, "exit_code": 127, "signal": None,
                                                "error": f"could not start payload: {exc}", "ts": time.time()})
            return 127
        forwarded = {"sig": None}

        def forward(signum, _frame):
            forwarded["sig"] = signum
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
        signal.signal(signal.SIGTERM, forward)
        warn = manifest.get("warn") or {}
        timer = None
        time_s = (manifest.get("resources") or {}).get("time_s")
        if warn.get("signal") and time_s:
            signum = getattr(signal, "SIG" + warn["signal"])

            # The main payload process only (setup execs into it), like Slurm's --signal=B:.
            # Signalling the whole group would kill children that don't handle it --
            # DataLoader workers, say -- and crash the very run that wanted to checkpoint.
            def warn_payload():
                try:
                    child.send_signal(signum)
                except ProcessLookupError:
                    return
                set_fact(adir, "warned", {"signal": warn["signal"], "ts": time.time()})
            # RuntimeMaxSec counts from unit start, which is a moment before this.
            timer = threading.Timer(max(0.0, time_s - warn.get("before_s", 300)), warn_payload)
            timer.daemon = True
            timer.start()
        rc = child.wait()
        if timer is not None:
            timer.cancel()
    sig = -rc if rc < 0 else None
    result = {"invocation": invocation, "exit_code": rc if rc >= 0 else None, "signal": sig, "ts": time.time()}
    # Compare-and-write: never replace the winning invocation's result (§2.6).
    if not create_exclusive(adir / "result.json", result):
        create_exclusive(adir / "replay" / f"result-{invocation}.json", result)
    return rc if rc >= 0 else 128 + (sig or 0)


def cmd_finalize(root: ControlRoot, args) -> int:
    """ExecStopPost: persist systemd's view independently of the runner (§2.6)."""
    adir = root.attempt_dir(args.attempt)
    record = {"service_result": os.environ.get("SERVICE_RESULT"), "exit_code": os.environ.get("EXIT_CODE"),
              "exit_status": os.environ.get("EXIT_STATUS"), "invocation": os.environ.get("INVOCATION_ID"),
              "ts": time.time(), "boot_id": boot_id()}
    if not create_exclusive(adir / "service_result.json", record):
        create_exclusive(adir / "replay" / f"finalize-{record['invocation'] or secrets.token_hex(4)}.json", record)
    if not fact(adir, "stopped"):
        set_fact(adir, "stopped", {"via": "finalizer"})
    return 0


def classify(adir: Path, props: dict[str, str] | None) -> dict[str, Any]:
    """Turn facts + systemd evidence into one observation (see executors/base.py)."""
    if not adir.exists():
        return {"state": "absent"}
    started = fact(adir, "start_requested")
    entered = fact(adir, "runner_entered")
    result = read_json(adir / "result.json")
    service = read_json(adir / "service_result.json")
    cancelled = fact(adir, "cancel") is not None
    current_boot = boot_id()
    obs: dict[str, Any] = {"payload_entered": entered is not None, "boot_id": current_boot,
                           "evidence": {"result": result, "service": service, "unit": props}}
    if cancelled and entered is None:
        active = props and props.get("ActiveState") in ("active", "activating")
        if not active:
            return {**obs, "state": "refused"}
    if entered is None:
        if started is None:
            return {**obs, "state": "staged" if fact(adir, "staged") else "absent"}
        if started.get("boot_id") != current_boot:
            return {**obs, "state": "staged"}    # the start request died with the old boot
        active = props and props.get("ActiveState") in ("active", "activating")
        return {**obs, "state": "running" if active else "start_requested"}
    if entered.get("boot_id") != current_boot and result is None:
        return {**obs, "state": "stopped", "outcome": "NODE_FAIL", "cgroup_empty": True, "boot_changed": True}
    active = props is not None and props.get("ActiveState") in ("active", "activating", "deactivating")
    if active:
        return {**obs, "state": "running"}
    empty = cgroup_empty(props or {}) if props else None
    sres = (service or {}).get("service_result")
    outcome, code, sig = None, None, None
    if result and "_unreadable" not in result:
        code, sig = result.get("exit_code"), result.get("signal")
    # Scheduler/cgroup evidence outranks an application's own result (§3.6).
    if sres == "oom-kill":
        outcome = "OUT_OF_MEMORY"
    elif sres == "timeout":
        outcome = "TIMEOUT"
    elif cancelled and (sig is not None or code is None or sres in ("signal", "exit-code")):
        outcome = "CANCELLED"
    elif code == 0 and sres in (None, "success"):
        outcome = "COMPLETED"
    elif code is not None:
        outcome = "FAILED"
    elif sig is not None:
        outcome = "FAILED"
    else:
        outcome = "UNKNOWN_EXIT"
    return {**obs, "state": "stopped", "outcome": outcome, "exit_code": code, "exit_signal": sig,
            "cgroup_empty": empty if empty is not None else (props or {}).get("ActiveState") in ("inactive", "failed")}


def cmd_status(root: ControlRoot, args) -> dict[str, Any]:
    enrollment = root.require_enrolled(args.fleet_id)
    out = {}
    for attempt in args.attempts:
        adir = root.attempt_dir(attempt)
        props = None
        if adir.exists() and fact(adir, "start_requested"):
            try:
                props = unit_show(attempt)
            except ShimError as exc:
                out[attempt] = {"state": "unknown", "reason": exc.code}
                continue
        out[attempt] = classify(adir, props)
    gpu_snapshot = nvidia_snapshot(include_process_identity=True, root=root, fleet_id=enrollment["fleet_id"])
    gpu_processes = {"complete": bool(gpu_snapshot.get("ok")),
                     "boot_id": gpu_snapshot.get("boot_id"),
                     "taken_at": gpu_snapshot.get("taken_at"), "gpus": []}
    if gpu_snapshot.get("ok"):
        for uuid, gpu in sorted(gpu_snapshot["gpus"].items()):
            processes = gpu.get("processes") or []
            gpu_processes["gpus"].append({"uuid": uuid, "processes": processes,
                                          "complete": all(p.get("attribution") != "ownership_unknown"
                                                          for p in processes)})
        gpu_processes["complete"] = all(g["complete"] for g in gpu_processes["gpus"])
    else:
        gpu_processes["error"] = gpu_snapshot.get("reason", "gpu_inventory_unavailable")
    body = {"attempts": out, "boot_id": boot_id(), "mem_available_mib": mem_available_mib(),
            "gpu_processes": gpu_processes}
    if args.log:
        body["logs"] = _read_log_requests(root, args.log, args.log_budget)
    return body


def _read_log(adir: Path, stream: str, offset: int, max_bytes: int) -> dict[str, Any]:
    import base64
    path = adir / ("stderr.log" if stream == "stderr" else "stdout.log")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {"size": None if not adir.exists() else 0, "offset": offset, "data_b64": ""}
    offset = min(max(0, offset), size)
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(max(0, min(max_bytes, 1024 * 1024)))
    return {"size": size, "offset": offset, "data_b64": base64.b64encode(data).decode()}


def _read_log_requests(root: ControlRoot, requests: list[str], budget: int) -> dict[str, Any]:
    """Log deltas riding on a status reply: ATTEMPT:STREAM:OFFSET, one budget shared by all,
    so the whole reply stays well inside fleetctl's captured-stdout cap."""
    each = max(4096, budget // max(1, len(requests)))
    out: dict[str, dict[str, Any]] = {}
    for req in requests:
        attempt, _, rest = req.partition(":")
        stream, _, offset = rest.partition(":")
        if stream not in ("stdout", "stderr") or not offset.isdigit():
            raise ShimError("bad_log_request", f"expected ATTEMPT:STREAM:OFFSET, got {req!r}")
        out.setdefault(attempt, {})[stream] = _read_log(root.attempt_dir(attempt), stream, int(offset), each)
    return out


def cmd_cancel(root: ControlRoot, args) -> dict[str, Any]:
    root.require_enrolled(args.fleet_id)
    adir = root.attempt_dir(args.attempt)
    with root.lock():
        seen = root.highest_epoch()
        if args.epoch != seen:
            return {"stopped": False, "never_started": False,
                    "reason": "stale_epoch" if args.epoch < seen else "future_epoch",
                    "highest_epoch_seen": seen}
        if not adir.exists():
            adir.mkdir(mode=0o700, parents=True)
            (adir / "facts").mkdir(mode=0o700)
        if not fact(adir, "cancel"):
            set_fact(adir, "cancel", {"epoch": args.epoch})
        entered = fact(adir, "runner_entered") is not None
        if fact(adir, "start_requested") or entered:
            rc, _out, err = run_bounded(["systemctl", "--user", "stop", unit_name(args.attempt)],
                                        timeout=SYSTEMCTL_TIMEOUT_S, env={**os.environ, **user_env()})
            if rc is None:
                return {"stopped": False, "never_started": False, "reason": "stop_timeout"}
            props = unit_show(args.attempt)
            stopped = props.get("ActiveState") in ("inactive", "failed", "") and bool(cgroup_empty(props))
            return {"stopped": stopped, "never_started": not entered,
                    "reason": None if stopped else "not_yet_stopped"}
        return {"stopped": True, "never_started": True}


def cmd_release(root: ControlRoot, args) -> dict[str, Any]:
    root.require_enrolled(args.fleet_id)
    adir = root.attempt_dir(args.attempt)
    with root.lock():
        seen = root.highest_epoch()
        if args.epoch != seen:
            return {"released": False, "reason": "stale_epoch" if args.epoch < seen else "future_epoch",
                    "highest_epoch_seen": seen}
        manifest = read_json(adir / "manifest.json") or {}
        obs = classify(adir, unit_show(args.attempt) if fact(adir, "start_requested") else None)
        if obs["state"] not in ("stopped", "refused", "staged", "absent"):
            return {"released": False, "reason": f"attempt is {obs['state']}"}
        if obs["state"] == "stopped" and obs.get("cgroup_empty") is not True:
            return {"released": False, "reason": "descendants_remain"}
        for uuid in manifest.get("gpus") or []:
            marker_path = root.root / "alloc" / f"{uuid}.json"
            marker = read_json(marker_path)
            if marker and marker.get("attempt_id") == args.attempt:
                marker_path.unlink()
        if adir.exists():
            set_fact(adir, "released")
    run_bounded(["systemctl", "--user", "reset-failed", unit_name(args.attempt)],
                timeout=SYSTEMCTL_TIMEOUT_S, env={**os.environ, **user_env()})
    return {"released": True}


def cmd_cache_release(root: ControlRoot, args) -> dict[str, Any]:
    """Drop the bundle retention pin only after terminal artifacts are collected."""
    root.require_enrolled(args.fleet_id)
    adir = root.attempt_dir(args.attempt)
    with root.lock():
        _require_current_epoch(root, args.epoch)
        manifest = read_json(adir / "manifest.json")
        if (not isinstance(manifest, dict) or "_unreadable" in manifest
                or manifest.get("attempt_id") != args.attempt
                or manifest.get("fleet_id") != args.fleet_id
                or not isinstance(manifest.get("epoch"), int)
                or manifest.get("epoch") <= 0 or manifest.get("epoch") > args.epoch):
            raise ShimError("manifest_mismatch", "attempt manifest does not match cache release fence")
        digest = manifest.get("bundle_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not DIGEST_RE.fullmatch(digest[7:]):
            raise ShimError("attempt_digest_invalid", "attempt has no valid bundle digest")
        marker = fact(adir, "cache.released")
        if marker is not None:
            marker_epoch = marker.get("epoch") if isinstance(marker, dict) else None
            if (not isinstance(marker_epoch, int) or marker_epoch < manifest["epoch"] or marker_epoch > args.epoch
                    or marker.get("attempt_id") != args.attempt or marker.get("fleet_id") != args.fleet_id
                    or marker.get("bundle_digest") != digest
                    or marker.get("spec_digest") != manifest.get("spec_digest")):
                raise ShimError("cache_release_marker_invalid", "existing cache release marker does not match attempt")
            return {"released": True, "already": True}
        released = fact(adir, "released")
        if not isinstance(released, dict) or "_unreadable" in released:
            return {"released": False, "reason": "allocation_not_released"}
        obs = classify(adir, unit_show(args.attempt) if fact(adir, "start_requested") else None)
        if obs["state"] not in ("stopped", "refused", "staged", "absent"):
            return {"released": False, "reason": f"attempt is {obs['state']}"}
        if obs["state"] == "stopped" and obs.get("cgroup_empty") is not True:
            return {"released": False, "reason": "descendants_remain"}
        set_fact(adir, "cache.released", {"attempt_id": args.attempt, "fleet_id": args.fleet_id,
                                           "epoch": args.epoch, "bundle_digest": digest,
                                           "spec_digest": manifest.get("spec_digest")})
    return {"released": True}


def _owned_regular_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()


def _cache_gc_candidates(root: ControlRoot, now: float) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Inventory old bundle files; unreadable attempt metadata blocks collection."""
    cache = root.root / "cache"
    if not _is_private_owned_dir(cache):
        return [], [{"path": str(cache), "reason": "cache_not_private_owned_directory"}]
    latest_release: dict[str, float] = {}
    pinned: set[str] = set()
    blocked: list[dict[str, str]] = []
    attempts = root.root / "attempts"
    if not _is_private_owned_dir(attempts):
        return [], [{"path": str(attempts), "reason": "attempts_not_private_owned_directory"}]
    for adir in attempts.iterdir():
        if not adir.is_dir() or adir.is_symlink():
            blocked.append({"path": str(adir), "reason": "unexpected_attempt_entry"})
            continue
        manifest_path = adir / "manifest.json"
        if not _owned_regular_file(manifest_path):
            blocked.append({"path": str(manifest_path), "reason": "attempt_manifest_not_owned_regular_file"})
            continue
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict) or "_unreadable" in manifest:
            blocked.append({"path": str(adir / "manifest.json"), "reason": "attempt_manifest_unreadable"})
            continue
        digest = manifest.get("bundle_digest")
        if digest is None:
            continue
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not DIGEST_RE.fullmatch(digest[7:]):
            blocked.append({"path": str(adir / "manifest.json"), "reason": "attempt_digest_invalid"})
            continue
        released = fact(adir, "cache.released")
        if released is None:
            pinned.add(digest[7:])
        else:
            released_path = adir / "facts" / "cache.released"
            manifest_epoch = manifest.get("epoch")
            marker_epoch = released.get("epoch") if isinstance(released, dict) else None
            marker_valid = (isinstance(released, dict) and "_unreadable" not in released
                            and released.get("attempt_id") == manifest.get("attempt_id")
                            and released.get("fleet_id") == manifest.get("fleet_id")
                            and isinstance(manifest_epoch, int) and manifest_epoch > 0
                            and isinstance(marker_epoch, int) and marker_epoch >= manifest_epoch
                            and marker_epoch <= root.highest_epoch()
                            and released.get("bundle_digest") == digest
                            and released.get("spec_digest") == manifest.get("spec_digest"))
            if not _is_private_owned_dir(adir / "facts") or not _owned_regular_file(released_path):
                blocked.append({"path": str(released_path), "reason": "cache_release_marker_not_owned_regular_file"})
                continue
            if not marker_valid:
                blocked.append({"path": str(released_path), "reason": "cache_release_marker_invalid"})
                continue
            try:
                released_at = float(released["ts"])
            except (KeyError, TypeError, ValueError):
                blocked.append({"path": str(released_path), "reason": "cache_release_time_invalid"})
                continue
            latest_release[digest[7:]] = max(latest_release.get(digest[7:], 0.0), released_at)
    # A bundle may already exist from an earlier attempt while a fresh staged
    # inbox is waiting for prepare. That inbox is an authoritative pin too.
    inbox = root.root / "inbox"
    if not _is_private_owned_dir(inbox):
        blocked.append({"path": str(inbox), "reason": "inbox_not_private_owned_directory"})
    else:
        for idir in inbox.iterdir():
            if not idir.is_dir() or idir.is_symlink():
                blocked.append({"path": str(idir), "reason": "unexpected_inbox_entry"})
                continue
            manifest_path = idir / "manifest.json"
            if not _owned_regular_file(manifest_path):
                blocked.append({"path": str(manifest_path), "reason": "inbox_manifest_not_owned_regular_file"})
                continue
            manifest = read_json(manifest_path)
            if not isinstance(manifest, dict) or "_unreadable" in manifest:
                blocked.append({"path": str(idir / "manifest.json"), "reason": "inbox_manifest_unreadable"})
                continue
            digest = manifest.get("bundle_digest")
            if digest is not None:
                if not isinstance(digest, str) or not digest.startswith("sha256:") or not DIGEST_RE.fullmatch(digest[7:]):
                    blocked.append({"path": str(idir / "manifest.json"), "reason": "inbox_digest_invalid"})
                else:
                    pinned.add(digest[7:])
    if blocked:
        return [], blocked
    eligible = []
    for path in sorted(cache.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{64}\.tar\.gz", path.name):
            continue
        name = path.name[:-7] if path.name.endswith(".tar.gz") else path.name
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                blocked.append({"path": str(path), "reason": "cache_entry_not_owned_regular_file"})
                continue
            if name in pinned:
                continue
            age = now - max(st.st_mtime, latest_release.get(name, 0.0))
            if age >= REMOTE_CACHE_GRACE_S:
                eligible.append({"digest": "sha256:" + name, "path": str(path), "bytes": st.st_size,
                                 "age_seconds": int(age)})
        except OSError as exc:
            blocked.append({"path": str(path), "reason": str(exc)})
    return eligible, blocked


def cmd_cache_gc(root: ControlRoot, args) -> dict[str, Any]:
    """Two-pass, operator-invoked remote cache retention: inspect, then explicit purge."""
    root.require_enrolled(args.fleet_id)
    with root.lock():
        _require_current_epoch(root, args.epoch)
        now = time.time()
        eligible, blocked = _cache_gc_candidates(root, now)
        if not args.purge:
            return {"mode": "inspect", "eligible": eligible, "blocked": blocked,
                    "grace_seconds": REMOTE_CACHE_GRACE_S}
        if not args.purge.startswith("sha256:") or not DIGEST_RE.fullmatch(args.purge[7:]):
            raise ShimError("bad_digest", "purge requires a canonical sha256:<64 lowercase hex> digest")
        if blocked:
            return {"mode": "purge", "removed": [], "blocked": blocked}
        item = next((x for x in eligible if x["digest"] == args.purge), None)
        if item is None:
            return {"mode": "purge", "removed": [],
                    "blocked": [{"digest": args.purge, "reason": "not_currently_eligible"}]}
        path = Path(item["path"])
        # Recheck the exact leaf immediately before unlink while holding the node lock.
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return {"mode": "purge", "removed": [],
                    "blocked": [{"digest": args.purge, "reason": "cache_entry_changed"}]}
        path.unlink()
        _fsync_dir(path.parent)
        return {"mode": "purge", "removed": [item], "blocked": []}


def cmd_logs(root: ControlRoot, args) -> dict[str, Any]:
    root.require_enrolled(args.fleet_id)
    chunk = _read_log(root.attempt_dir(args.attempt), args.stream, args.offset, args.max_bytes)
    return {**chunk, "size": chunk["size"] or 0}


def _collect_stage_impl(root: ControlRoot, args) -> dict[str, Any]:
    """Stage approved outputs for one pull (§6.4). Idempotent: a staged outbox is reported again.

    Only regular files under the attempt's own workdir, never through a symlink,
    never outside it. Each is hard-linked (or copied, across filesystems) to a
    numbered name, so no transfer-side exclude pattern can drop a real output.
    """
    adir = root.attempt_dir(args.attempt)
    outbox = adir / "outbox"
    staged = read_json(outbox / "manifest.json")
    if staged and "_unreadable" not in staged:
        return {**staged, "already": True}
    manifest = read_json(adir / "manifest.json")
    if not manifest or "_unreadable" in manifest:
        raise ShimError("not_staged", "no manifest for this attempt")
    if manifest.get("in_place"):
        raise ShimError("in_place_collect", "in-place jobs need an approved output root")
    workdir = (adir / "code" / (manifest.get("subdir") or ".")).resolve()
    files, missing, refused, total = [], [], [], 0
    for rel in args.path:
        top = workdir / rel
        if not os.path.lexists(top):
            missing.append(rel)
            continue
        real = os.path.realpath(top)
        if real != str(workdir) and not real.startswith(str(workdir) + os.sep):
            refused.append({"path": rel, "reason": "escapes_workdir"})
            continue
        if os.path.islink(top):
            refused.append({"path": rel, "reason": "symlink"})
            continue
        if os.path.isfile(top):
            walk = [(str(top.parent), [], [top.name])]
        elif os.path.isdir(top):
            walk = os.walk(top, followlinks=False)
        else:
            refused.append({"path": rel, "reason": "not_a_regular_file"})
            continue
        for dirpath, dirnames, filenames in walk:
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                st = os.lstat(path)
                relpath = os.path.relpath(path, workdir)
                if not stat.S_ISREG(st.st_mode):
                    refused.append({"path": relpath, "reason": "not_a_regular_file"})
                    continue
                files.append({"relpath": relpath, "size": st.st_size, "src": path})
                total += st.st_size
            for d in list(dirnames):
                if os.path.islink(os.path.join(dirpath, d)):
                    refused.append({"path": os.path.relpath(os.path.join(dirpath, d), workdir), "reason": "symlink"})
                    dirnames.remove(d)
            if len(files) > args.max_files:
                raise ShimError("too_many_files", f"more than {args.max_files} files to collect")
    if total > args.max_bytes:
        raise ShimError("too_large", f"{total} bytes to collect exceeds the {args.max_bytes}-byte allowance",
                        total=total)
    tmp = adir / f".outbox.{os.getpid()}"
    if tmp.exists() or tmp.is_symlink():
        raise ShimError("unsafe_cleanup", "temporary outbox path already exists")
    tmp.mkdir(mode=0o700)
    out = []
    for n, f in enumerate(files):
        slot = f"{n:06d}"
        try:
            os.link(f["src"], tmp / slot)
        except OSError:
            shutil.copyfile(f["src"], tmp / slot)
        digest = hashlib.sha256()
        with open(tmp / slot, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        out.append({"slot": slot, "relpath": f["relpath"], "size": f["size"], "sha256": digest.hexdigest()})
    body = {"files": out, "missing": missing, "refused": refused, "total": total}
    write_atomic(tmp / "manifest.json", body)
    with root.lock():
        _require_current_epoch(root, args.epoch)
        os.replace(tmp, outbox)
        _fsync_dir(adir)
    return {**body, "already": False}


def cmd_collect_stage(root: ControlRoot, args) -> dict[str, Any]:
    root.require_enrolled(args.fleet_id)
    with root.lock():
        _require_current_epoch(root, args.epoch)
    return _collect_stage_impl(root, args)


def cmd_collect_clean(root: ControlRoot, args) -> dict[str, Any]:
    root.require_enrolled(args.fleet_id)
    outbox = root.attempt_dir(args.attempt) / "outbox"
    trash = outbox.with_name(f".outbox-clean-{secrets.token_hex(8)}")
    with root.lock():
        _require_current_epoch(root, args.epoch)
        if outbox.exists():
            os.replace(outbox, trash)
    _safe_rmtree(root, trash)
    return {"cleaned": True}


def cmd_shell(root: ControlRoot, args) -> int:
    """`fq shell`: a shell (or one command) inside a running allocation (§6.1).

    Runs in its own transient scope that BindsTo the allocation's unit, so when
    the allocation ends -- walltime or cancel -- the shell and everything started
    from it end too. It sees exactly the allocation's GPUs, by UUID.
    """
    try:
        root.require_enrolled(args.fleet_id)
        adir = root.attempt_dir(args.attempt)
        manifest = read_json(adir / "manifest.json")
        if not manifest or "_unreadable" in manifest or not fact(adir, "payload_started"):
            raise ShimError("not_running", "this attempt has not started")
        props = unit_show(args.attempt)
        if props.get("ActiveState") not in ("active", "activating"):
            raise ShimError("not_running", f"the allocation's unit is {props.get('ActiveState') or 'gone'}")
        env = {**os.environ, **user_env(), **_job_env(manifest)}
        env["PATH"] = os.environ.get("PATH") or env["PATH"]      # a person's shell keeps their PATH
        env["FQ_SHELL"] = "1"
        cwd = manifest.get("in_place") or str(adir / "code" / (manifest.get("subdir") or "."))
        command = [c for c in args.command if c != "--"] or [_login_shell(), "-l"]
        unit = unit_name(args.attempt)
        scope = f"{unit.removesuffix('.service')}-shell-{secrets.token_hex(3)}"
        argv = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={scope}",
                "-p", f"BindsTo={unit}", "-p", f"After={unit}", "--", *command]
        os.chdir(cwd)
        tool = shutil.which("systemd-run", path=user_env()["PATH"])
        if tool is None:
            raise ShimError("no_systemd_run", "systemd-run is not on the trusted PATH")
        os.execve(tool, argv, env)
    except ShimError as exc:
        sys.stdout.write(json.dumps({"schema": "fq-node/v1", "ok": False,
                                     "error": {"code": exc.code, "message": exc.message}}) + "\n")
        return 2
    return 0                                                    # not reached: execve replaced us


def _login_shell() -> str:
    import pwd
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
    except KeyError:
        return "/bin/bash"


def cmd_probe(root: ControlRoot, args) -> dict[str, Any]:
    """Capability report used for enablement evidence (§10). Changes nothing."""
    facts: dict[str, Any] = {"shim_version": SHIM_VERSION, "python": sys.version.split()[0],
                             "boot_id": boot_id(), "mem_available_mib": mem_available_mib(),
                             "cpus": os.cpu_count()}
    try:
        env = user_env()
        facts["user_runtime"] = "ok"
        rc, out, _ = run_bounded(["systemctl", "--user", "is-system-running"], timeout=SYSTEMCTL_TIMEOUT_S,
                                 env={**os.environ, **env})
        facts["user_manager"] = out.strip() if rc is not None else "timeout"
    except ShimError as exc:
        facts["user_runtime"] = exc.code
    rc, out, _ = run_bounded(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"], timeout=PROBE_TIMEOUT_S)
    facts["linger"] = out.strip().endswith("yes") if rc == 0 else None
    uid = os.getuid()
    controllers = Path(os.environ.get("FQ_NODE_CGROUP_ROOT", "/sys/fs/cgroup"),
                       f"user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers")
    try:
        facts["delegated_controllers"] = controllers.read_text().split()
    except OSError:
        facts["delegated_controllers"] = None
    facts["gpus"] = nvidia_snapshot()
    try:
        runtime_dir = _probe_runtime_dir()
        facts["control_root_writable"] = _bounded_write_test(root.root, runtime_dir=runtime_dir)
    except ShimError as exc:
        facts["control_root_writable"] = exc.code
    return facts


def _proc_identity(pid: int) -> tuple[str, int] | None:
    """Return process state and start tick; PID alone is not a durable identity."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_bytes()
        fields = raw[raw.rfind(b")") + 1:].split()
        value = int(fields[19])
        return (fields[0].decode("ascii"), value) if value > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _proc_start_ticks(pid: int) -> int | None:
    identity = _proc_identity(pid)
    return identity[1] if identity else None


def _probe_identity_live(record: dict[str, Any]) -> bool:
    pid, ticks = record.get("pid"), record.get("start_ticks")
    recorded_boot, current_boot = record.get("boot_id"), boot_id()
    if (isinstance(recorded_boot, str) and recorded_boot not in ("", "unknown")
            and isinstance(current_boot, str) and current_boot not in ("", "unknown")
            and recorded_boot != current_boot):
        return False  # a process cannot survive a reboot
    if (recorded_boot != current_boot or current_boot in ("", "unknown")
            or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or isinstance(ticks, bool) or not isinstance(ticks, int) or ticks <= 0):
        # Missing or ambiguous identity is uncertain: retain the quarantine.
        return True
    identity = _proc_identity(pid)
    return bool(identity and identity[0] not in ("Z", "X") and identity[1] == ticks)


def _probe_runtime_dir() -> Path:
    """Resolve a protected runtime filesystem, never the path under test."""
    uid = os.getuid()
    runtime = Path(os.environ.get("FQ_NODE_RUNTIME_DIR", f"/run/user/{uid}"))
    try:
        st = runtime.lstat()
    except OSError as exc:
        raise ShimError("no_probe_runtime", f"probe runtime unavailable: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
        raise ShimError("bad_probe_runtime", "probe runtime must be a private directory owned by this user")
    return runtime


def _bounded_write_test(path: Path, *, runtime_dir: Path) -> str:
    """Probe a target under a deadline, accounting for abandoned children on healthy disk.

    runtime_dir/fleetq-probes must be on the protected runtime filesystem, never
    on the target mount being checked. A timed-out child is retained in the
    registry until its boot/PID/start identity is confirmed gone.
    """
    probe = path / f".probe-{secrets.token_hex(4)}"
    ledger = runtime_dir / "fleetq-probes"
    try:
        ledger.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = ledger.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return "failed: unsafe probe ledger directory"
        os.chmod(ledger, 0o700)
        key = hashlib.sha256(os.fsencode(os.path.normpath(str(path)))).hexdigest()
        record_path = ledger / f"{key}.json"
        with NodeLock(ledger / ".lock"):
            live = []
            for candidate in ledger.glob("*.json"):
                record = read_json(candidate)
                if not isinstance(record, dict) or "_unreadable" in record or _probe_identity_live(record):
                    live.append(candidate)
                else:
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
            if record_path in live or len(live) >= MAX_OUTSTANDING_PROBES:
                return "quarantined"
            # Reserve the slot before launching. If this fails, the suspect
            # path has not been touched and no helper exists to account for.
            write_atomic(record_path, {"state": "starting", "path": str(path),
                                       "boot_id": boot_id(), "started_at": time.time()})
            # Run the target syscall in the tracked process itself. A shell
            # wrapper could die while a descendant remains stuck on the mount.
            code = "import os,sys; p=sys.argv[1]; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.close(fd); os.unlink(p)"
            command = [sys.executable, "-c", code, str(probe)]
            try:
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, start_new_session=True, text=True)
            except Exception:
                record_path.unlink(missing_ok=True)
                raise
            try:
                ticks = _proc_start_ticks(child.pid)
                write_atomic(record_path, {"pid": child.pid, "boot_id": boot_id(), "start_ticks": ticks,
                                           "path": str(path), "started_at": time.time()})
            except Exception:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Keep the marker: uncertain work must quarantine rather than
                # being multiplied on the next probe.
                return "failed: probe identity could not be persisted"
    except (OSError, ShimError) as exc:
        return f"failed: {str(exc)[:120]}"
    try:
        child.communicate(timeout=PROBE_TIMEOUT_S)
        rc = child.returncode
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
        return "ok" if rc == 0 else f"failed: probe exited {rc}"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return "hung"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fq-node")
    parser.add_argument("--root", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("enroll"); p.add_argument("--fleet-id", required=True); p.add_argument("--node-id", required=True)
    p = sub.add_parser("fence"); p.add_argument("--fleet-id", required=True); p.add_argument("--epoch", type=int, required=True)
    p = sub.add_parser("publish-stage"); p.add_argument("--fleet-id", required=True)
    p.add_argument("--epoch", type=int, required=True); p.add_argument("--stage-id", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--fleet-id", required=True)
    p.add_argument("--epoch", type=int, required=True); p.add_argument("attempt")
    p = sub.add_parser("launch"); p.add_argument("--fleet-id", required=True); p.add_argument("--epoch", type=int, required=True)
    # realpath: bin/fq-node is a symlink into releases/<digest>/, and a unit keeps running
    # (and finalizing with) the release it was launched from across a shim upgrade (§11).
    p.add_argument("--shim-path", default=os.path.realpath(sys.argv[0])); p.add_argument("attempt")
    p = sub.add_parser("run"); p.add_argument("attempt")
    p = sub.add_parser("finalize"); p.add_argument("attempt")
    p = sub.add_parser("status"); p.add_argument("--fleet-id", required=True); p.add_argument("attempts", nargs="*")
    p.add_argument("--log", action="append", default=[], help="ATTEMPT:STREAM:OFFSET log delta to include")
    p.add_argument("--log-budget", type=int, default=384 * 1024, help="total log bytes in one reply")
    p = sub.add_parser("cancel"); p.add_argument("--fleet-id", required=True); p.add_argument("--epoch", type=int, required=True)
    p.add_argument("attempt")
    p = sub.add_parser("release"); p.add_argument("--fleet-id", required=True); p.add_argument("--epoch", type=int, required=True)
    p.add_argument("attempt")
    p = sub.add_parser("cache-release"); p.add_argument("--fleet-id", required=True); p.add_argument("--epoch", type=int, required=True)
    p.add_argument("attempt")
    p = sub.add_parser("cache-gc"); p.add_argument("--fleet-id", required=True); p.add_argument("--epoch", type=int, required=True)
    p.add_argument("--purge", help="explicit digest returned by a prior inspect")
    p = sub.add_parser("logs"); p.add_argument("--fleet-id", required=True); p.add_argument("attempt")
    p.add_argument("--stream", default="stdout"); p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-bytes", type=int, default=65536)
    p = sub.add_parser("collect-stage"); p.add_argument("--fleet-id", required=True)
    p.add_argument("--epoch", type=int, required=True); p.add_argument("attempt")
    p.add_argument("--path", action="append", default=[]); p.add_argument("--max-files", type=int, default=10000)
    p.add_argument("--max-bytes", type=int, default=2 * 1024 ** 3)
    p = sub.add_parser("collect-clean"); p.add_argument("--fleet-id", required=True)
    p.add_argument("--epoch", type=int, required=True); p.add_argument("attempt")
    p = sub.add_parser("shell"); p.add_argument("--fleet-id", required=True); p.add_argument("attempt")
    p.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("probe")
    args = parser.parse_args(argv)
    root = ControlRoot(args.root)
    if args.cmd == "run":
        return cmd_run(root, args)
    if args.cmd == "finalize":
        return cmd_finalize(root, args)
    if args.cmd == "shell":
        return cmd_shell(root, args)
    handlers = {"enroll": cmd_enroll, "fence": cmd_fence, "publish-stage": cmd_publish_stage,
                "prepare": cmd_prepare, "launch": cmd_launch,
                "status": cmd_status, "cancel": cmd_cancel, "release": cmd_release,
                "cache-release": cmd_cache_release, "logs": cmd_logs,
                "probe": cmd_probe, "cache-gc": cmd_cache_gc, "collect-stage": cmd_collect_stage,
                "collect-clean": cmd_collect_clean}
    try:
        body = {"ok": True, **handlers[args.cmd](root, args)}
    except ShimError as exc:
        body = {"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.extra}}
    except BundleError as exc:
        body = {"ok": False, "error": {"code": "bundle_" + exc.code, "message": exc.message}}
    sys.stdout.write(json.dumps({"schema": "fq-node/v1", **body}, sort_keys=True) + "\n")
    return 0
