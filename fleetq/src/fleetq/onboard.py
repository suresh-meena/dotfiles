"""Workstation onboarding: install the shim, enroll, probe (§10, §11).

One node at a time, only on an explicit, target-matching authorization, and
never implied by ``doctor``. The shim is pushed into an immutable
``<root>/releases/<digest>/`` directory, hash-checked on the node, and
activated by atomically repointing ``<root>/bin/fq-node``. Running units keep
the release they were launched from, because the shim resolves its own real
path when it writes a unit.

A probe changes nothing on the node. Its GPU inventory becomes ``node_gpus``
(a vanished GPU is drained, never deleted, since reservations may name it),
and its capability evidence decides whether the node is fit to enable.
Enabling stays a config decision; the report says what blocks it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .engine import fence
from .transport.fleetctl import Fleetctl
from .util import utcnow

# An installed release ships the shim beside its venv and names it in the unit
# (FLEETQ_SHIM); a source checkout uses its own build output.
SHIM_BUILD = Path(os.environ.get("FLEETQ_SHIM") or Path(__file__).resolve().parents[2] / "build" / "fq-node")
PUSH_TIMEOUT_S = 300
CALL_TIMEOUT_S = 90

# Shared read-only structure check. This mirrors _valid_install_bootstrap:
# accept an empty private root, staged releases before first activation, or
# the established bin/ + releases/ layout used by upgrades.
ROOT_LAYOUT_CHECK = r'''layout=$("$node_python" - "$root" <<'PY'
import os, re, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
uid = os.getuid()
def private_dir(path):
    try: st = path.lstat()
    except OSError: return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == uid and not (st.st_mode & 0o022)
def valid_release(path, name):
    if not re.fullmatch(r"[0-9a-f]{16}", name) or not private_dir(path): return False
    try:
        if os.listdir(path) != ["fq-node"]: return False
        f = path / "fq-node"
        st = f.lstat()
        return stat.S_ISREG(st.st_mode) and st.st_uid == uid and not (st.st_mode & 0o022)
    except OSError: return False
def emit(ok):
    print("ok" if ok else "unsafe")
    raise SystemExit(0)
if not root.exists(): emit(True)
if not private_dir(root): emit(False)
try: entries = set(os.listdir(root))
except OSError: emit(False)
if not entries: emit(True)
if "enrollment.json" in entries:
    # An already enrolled root has a fixed top-level shape. Validate its
    # pinned identity and owned private entries before treating this as an
    # upgrade destination; this prevents accepting arbitrary directories such
    # as ~/.ssh merely because they are owned by the user.
    allowed = {"alloc", "cache", "inbox", "attempts", "probes", "jobs", "pycache", ".lock",
               "enrollment.json", "fence.json", "bin", "releases"}
    if not {"alloc", "cache", "inbox", "attempts", "probes", "enrollment.json", "fence.json"} <= entries:
        emit(False)
    if not entries <= allowed: emit(False)
    try:
        for name in ("alloc", "cache", "inbox", "attempts", "probes", "jobs", "pycache"):
            if name in entries and not private_dir(root / name): emit(False)
        for name in ("enrollment.json", "fence.json", ".lock"):
            if name not in entries: continue
            st = (root / name).lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o022: emit(False)
        enrollment = __import__("json").loads((root / "enrollment.json").read_text())
        fence = __import__("json").loads((root / "fence.json").read_text())
        if (not isinstance(enrollment, dict) or not enrollment.get("fleet_id")
                or not enrollment.get("node_id") or enrollment.get("control_root") != os.path.realpath(root)):
            emit(False)
        if not isinstance(fence, dict) or not isinstance(fence.get("highest_epoch"), int): emit(False)
        if "bin" in entries or "releases" in entries:
            if not {"bin", "releases"} <= entries: emit(False)
            bin_dir, releases = root / "bin", root / "releases"
            if not private_dir(bin_dir) or not private_dir(releases): emit(False)
            release_names = os.listdir(releases)
            if not release_names or any(not valid_release(releases / name, name) for name in release_names): emit(False)
            if os.listdir(bin_dir) != ["fq-node"]: emit(False)
            link = bin_dir / "fq-node"
            if not link.is_symlink(): emit(False)
            parts = Path(os.readlink(link)).parts
            if (len(parts) != 4 or parts[0] != ".." or parts[1] != "releases"
                    or not re.fullmatch(r"[0-9a-f]{16}", parts[2]) or parts[3] != "fq-node"
                    or parts[2] not in release_names): emit(False)
    except (OSError, ValueError, TypeError): emit(False)
    emit(True)
if not entries <= {"bin", "releases"} or "releases" not in entries: emit(False)
releases = root / "releases"
if not private_dir(releases): emit(False)
try: names = os.listdir(releases)
except OSError: emit(False)
if not names or any(not valid_release(releases / name, name) for name in names): emit(False)
if "bin" not in entries: emit(True)
bin_dir = root / "bin"
if not private_dir(bin_dir): emit(False)
try:
    if os.listdir(bin_dir) != ["fq-node"]: emit(False)
    link = bin_dir / "fq-node"
    if not link.is_symlink(): emit(False)
    parts = Path(os.readlink(link)).parts
except OSError: emit(False)
if (len(parts) != 4 or parts[0] != ".." or parts[1] != "releases"
        or not re.fullmatch(r"[0-9a-f]{16}", parts[2]) or parts[3] != "fq-node"
        or parts[2] not in names): emit(False)
emit(True)
PY
) || layout=unsafe
[ "$layout" = ok ]
'''

# This runs before rsync. realpath -m resolves symlink components while
# allowing a dedicated root not to exist yet.
PROTECTED_SYSTEM_ROOT_CHECK = r'''
case "$canonical" in
  /etc|/etc/*|/usr|/usr/*|/var|/var/*|/root|/root/*|/boot|/boot/*|\
  /proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/bin|/bin/*|/sbin|/sbin/*|\
  /lib|/lib/*|/lib64|/lib64/*|/opt|/opt/*|/run|/run/*)
    echo '{"ok":false,"error":"unsafe_root"}'; exit 0;;
esac
'''
ROOT_CHECK_SCRIPT = r'''
root=$1
node_python=${2:-python3}
home=${HOME:-}
if [ -z "$home" ] || [ "${home#/}" = "$home" ] || [ "${root#/}" = "$root" ]; then
  echo '{"ok":false,"error":"unsafe_root"}'; exit 0
fi
case "/$root/" in */../*) echo '{"ok":false,"error":"unsafe_root"}'; exit 0;; esac
canonical=$(realpath -m -- "$root" 2>/dev/null) || { echo '{"ok":false,"error":"unsafe_root"}'; exit 0; }
home_canonical=$(realpath -m -- "$home" 2>/dev/null) || { echo '{"ok":false,"error":"unsafe_root"}'; exit 0; }
expected=${root%/}; [ -n "$expected" ] || expected=/
if [ "$canonical" != "$expected" ] || [ "$canonical" = / ] || [ "$canonical" = "$home_canonical" ]; then
  echo '{"ok":false,"error":"unsafe_root"}'; exit 0
fi
case "$canonical" in
  "$home_canonical"/.ssh|"$home_canonical"/.ssh/*|"$home_canonical"/.gnupg|"$home_canonical"/.gnupg/*|\
  "$home_canonical"/.config|"$home_canonical"/.config/*|"$home_canonical"/.local|"$home_canonical"/.local/*|\
  "$home_canonical"/.cache|"$home_canonical"/.cache/*)
    echo '{"ok":false,"error":"unsafe_root"}'; exit 0;;
esac
case "$home_canonical/" in "$canonical/"*) echo '{"ok":false,"error":"unsafe_root"}'; exit 0;; esac
'''+PROTECTED_SYSTEM_ROOT_CHECK+ROOT_LAYOUT_CHECK+r'''
if [ $? -ne 0 ]; then echo '{"ok":false,"error":"unsafe_root"}'; exit 0; fi
echo '{"ok":true}'
'''

# Arguments: root digest sha256. Recheck the protected root, verify the pushed
# file, then switch bin/fq-node atomically.
ACTIVATE_SCRIPT = r'''
set -u
root=$1; digest=$2; want=$3
node_python=${4:-python3}
home=${HOME:-}
canonical=$(realpath -m -- "$root" 2>/dev/null) || canonical=
home_canonical=$(realpath -m -- "$home" 2>/dev/null) || home_canonical=
expected=${root%/}; [ -n "$expected" ] || expected=/
case "/$root/" in */../*) canonical=unsafe;; esac
if [ -z "$home_canonical" ] || [ "$canonical" != "$expected" ] || [ "$canonical" = / ] ||
   [ "$canonical" = "$home_canonical" ]; then
  echo '{"ok":false,"error":"unsafe_root"}'; exit 0
fi
case "$canonical" in
  "$home_canonical"/.ssh|"$home_canonical"/.ssh/*|"$home_canonical"/.gnupg|"$home_canonical"/.gnupg/*|\
  "$home_canonical"/.config|"$home_canonical"/.config/*|"$home_canonical"/.local|"$home_canonical"/.local/*|\
  "$home_canonical"/.cache|"$home_canonical"/.cache/*)
    echo '{"ok":false,"error":"unsafe_root"}'; exit 0;;
esac
case "$home_canonical/" in "$canonical/"*) echo '{"ok":false,"error":"unsafe_root"}'; exit 0;; esac
'''+PROTECTED_SYSTEM_ROOT_CHECK+ROOT_LAYOUT_CHECK+r'''
if [ $? -ne 0 ]; then echo '{"ok":false,"error":"unsafe_root"}'; exit 0; fi
f="$root/releases/$digest/fq-node"
have=$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)
if [ "$have" != "$want" ]; then printf '{"ok":false,"error":"hash_mismatch","have":"%s"}\n' "$have"; exit 0; fi
chmod 0755 "$f" && mkdir -p "$root/bin" || { echo '{"ok":false,"error":"root_unwritable"}'; exit 0; }
ln -sfn "../releases/$digest/fq-node" "$root/bin/.fq-node.new" && mv -Tf "$root/bin/.fq-node.new" "$root/bin/fq-node" \
  || { echo '{"ok":false,"error":"activate_failed"}'; exit 0; }
printf '{"ok":true,"active":"%s"}\n' "$(readlink "$root/bin/fq-node")"
'''


class OnboardError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _node(cfg, name: str):
    node = next((n for n in cfg.nodes if n.id == name), None)
    if node is None:
        raise OnboardError("unknown_node", f"{name!r} is not a [[node]] in the fleetqd config")
    if node.backend != "bare":
        raise OnboardError("not_a_workstation", f"{name!r} is a {node.backend} site; nothing is installed on clusters")
    root = node.control_root
    if (not root or not root.startswith("/") or root == "/" or
            ".." in root.split("/") or "." in root.split("/")):
        raise OnboardError("no_control_root", f"{name!r} needs an absolute control_root on a local, healthy disk")
    return node


def _shim_argv(root: str, *args: str, node_python: str = "python3") -> list[str]:
    return [node_python, f"{root}/bin/fq-node", "--root", root, *args]


def _body(res) -> dict[str, Any] | None:
    body = res.payload() if res.outcome in ("ok", "remote_failed") else None
    return body if isinstance(body, dict) else None


async def install(cfg, store, transport: Fleetctl, name: str, *, shim: Path = SHIM_BUILD) -> dict[str, Any]:
    node = _node(cfg, name)
    target, root = node.fleetctl_target or node.id, node.control_root
    node_python = node.node_python or "python3"
    check = await transport.exec(target, ["sh", "-c", ROOT_CHECK_SCRIPT, "fleetq", root, node_python],
                                 timeout=CALL_TIMEOUT_S, mutation=False, expected_role="workstation")
    check_body = _body(check)
    if check_body is None or not check_body.get("ok"):
        raise OnboardError("unsafe_control_root", f"remote control_root precheck failed: {check_body or check.stderr[-300:]}")
    if not shim.exists():
        raise OnboardError("shim_not_built", f"{shim} is missing; run scripts/build.py")
    data = shim.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    digest = sha[:16]
    report: dict[str, Any] = {"node": name, "target": target, "control_root": root, "shim_sha256": sha}
    tmp = Path(tempfile.mkdtemp(prefix="fq-install-"))
    try:
        (tmp / "fq-node").write_bytes(data)
        pushed = await transport.sync_push(target, tmp, f"{root}/releases/{digest}/", timeout=PUSH_TIMEOUT_S,
                                           expected_role="workstation")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not pushed.ok:
        raise OnboardError("push_failed", f"shim push {pushed.outcome}: {pushed.stderr[-300:]}")
    res = await transport.exec(target, ["sh", "-c", ACTIVATE_SCRIPT, "fleetq", root, digest, sha, node_python],
                               timeout=CALL_TIMEOUT_S, mutation=True, expected_role="workstation")
    body = _body(res)
    if body is None or not body.get("ok"):
        raise OnboardError("activate_failed", f"activation {res.outcome}: {body or res.stderr[-300:]}")
    report["active"] = body.get("active")
    fleet_id = store.run_sync(fence.ensure_fleet_id)
    res = await transport.exec(target, _shim_argv(root, "enroll", "--fleet-id", fleet_id, "--node-id", name,
                                                   node_python=node_python),
                               timeout=CALL_TIMEOUT_S, mutation=True, expected_role="workstation")
    body = _body(res)
    if body is None or not body.get("ok"):
        raise OnboardError("enroll_failed", f"enroll {res.outcome}: {body or res.stderr[-300:]}")
    report["enrollment"] = {"fleet_id": fleet_id, "already": bool(body.get("already"))}
    report["probe"] = await probe(cfg, store, transport, name)
    return report


async def probe(cfg, store, transport: Fleetctl, name: str) -> dict[str, Any]:
    node = _node(cfg, name)
    target, root = node.fleetctl_target or node.id, node.control_root
    res = await transport.exec(target, _shim_argv(root, "probe", node_python=node.node_python or "python3"), timeout=CALL_TIMEOUT_S, mutation=False,
                               expected_role="workstation")
    body = _body(res)
    if body is None or body.get("schema") != "fq-node/v1" or not body.get("ok"):
        raise OnboardError("probe_failed", f"probe {res.outcome}: {body or res.stderr[-300:]}")
    verdict = assess(body, mode=node.mode or "exclusive")
    store.run_sync(lambda c: record_probe(c, name, body, verdict))
    return verdict


def assess(facts: dict[str, Any], *, mode: str) -> dict[str, Any]:
    """What the probe says about fitness to run jobs. Blockers keep a node disabled."""
    blockers: list[str] = []
    warnings: list[str] = []
    if facts.get("user_runtime") != "ok":
        blockers.append(f"no usable user systemd runtime ({facts.get('user_runtime')})")
    elif facts.get("user_manager") not in ("running", "degraded"):
        blockers.append(f"user manager is {facts.get('user_manager')!r}")
    if facts.get("linger") is not True:
        blockers.append("linger is off: user units die when the last session ends (loginctl enable-linger)")
    controllers = facts.get("delegated_controllers")
    if not controllers or "memory" not in controllers:
        msg = "memory controller not delegated to the user manager: MemoryMax is not enforced"
        blockers.append(msg)
    if controllers and "cpu" not in controllers:
        warnings.append("cpu controller not delegated: CPUQuota is not enforced")
    if facts.get("control_root_writable") != "ok":
        blockers.append(f"control root not writable ({facts.get('control_root_writable')})")
    snapshot = facts.get("gpus") or {}
    gpus: list[dict[str, Any]] = []
    if not snapshot.get("ok"):
        blockers.append(f"GPU inventory unavailable ({snapshot.get('reason')})")
    else:
        for uuid, g in sorted(snapshot.get("gpus", {}).items(), key=lambda kv: kv[1].get("index") or 0):
            drain = None
            if str(g.get("mig", "")).lower() == "enabled":
                drain = "mig_enabled"            # MIG slices are out of scope in v1 (§2.1)
            if "exclusive" in str(g.get("compute_mode", "")).lower():
                warnings.append(f"{uuid} is in {g['compute_mode']} compute mode: multi-process CUDA jobs fail there")
            gpus.append({"uuid": uuid, "index": g.get("index"), "model": g.get("model"),
                         "vram_total_mib": g.get("mem_total_mib"), "pci_bus": g.get("pci_bus"), "drain": drain})
    return {"fit": not blockers, "blockers": blockers, "warnings": warnings, "gpus": gpus,
            "boot_id": facts.get("boot_id"), "shim_version": facts.get("shim_version")}


def record_probe(conn: sqlite3.Connection, node_id: str, facts: dict[str, Any], verdict: dict[str, Any]) -> None:
    now = utcnow()
    conn.execute("UPDATE nodes SET last_probe_json = ?, last_probe_at = ?, boot_id = COALESCE(?, boot_id),"
                 " updated_at = ? WHERE id = ?",
                 (json.dumps({"facts": facts, "verdict": verdict}, sort_keys=True), now, facts.get("boot_id"),
                  now, node_id))
    if not (facts.get("gpus") or {}).get("ok"):
        return                                   # unknown is not "no GPUs": keep the last inventory
    seen = set()
    for g in verdict["gpus"]:
        seen.add(g["uuid"])
        conn.execute(
            "INSERT INTO node_gpus (node_id, uuid, model, vram_total, pci_bus, idx, drained, drain_reason)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(node_id, uuid) DO UPDATE SET model = excluded.model,"
            " vram_total = excluded.vram_total, pci_bus = excluded.pci_bus, idx = excluded.idx,"
            # A probe only clears the drains a probe set; an operator's drain stays.
            " drained = CASE WHEN node_gpus.drain_reason IN ('mig_enabled','missing_from_probe') OR"
            "   node_gpus.drained = 0 THEN excluded.drained ELSE node_gpus.drained END,"
            " drain_reason = CASE WHEN node_gpus.drain_reason IN ('mig_enabled','missing_from_probe') OR"
            "   node_gpus.drained = 0 THEN excluded.drain_reason ELSE node_gpus.drain_reason END",
            (node_id, g["uuid"], g["model"], int(g["vram_total_mib"]) if g["vram_total_mib"] is not None else None,
             g["pci_bus"], int(g["index"]) if g["index"] is not None else None, int(bool(g["drain"])), g["drain"]))
    for row in conn.execute("SELECT uuid FROM node_gpus WHERE node_id = ?", (node_id,)).fetchall():
        if row["uuid"] not in seen:
            conn.execute("UPDATE node_gpus SET drained = 1, drain_reason = 'missing_from_probe'"
                         " WHERE node_id = ? AND uuid = ? AND drained = 0", (node_id, row["uuid"]))
