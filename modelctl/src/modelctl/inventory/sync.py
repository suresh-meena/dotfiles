from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ModelctlError
from .registry import Registry
from .scanner import scan_local_roots, remote_scan


def _record_artifact(registry: Registry, machine_id: str, target_id: str, apath: str, match: dict[str, Any] | None, model_alias: str, total_found: list[int]) -> None:
    artifact_id = f"{machine_id}:{apath}"
    if match:
        registry.upsert_artifact(artifact_id, machine_id, model_alias, apath, "safetensors", match["size_bytes"], match["fingerprint"], "AVAILABLE")
        registry.add_observation(artifact_id, 1, match["size_bytes"], match["fingerprint"], probe_version="1.5", error_code=None)
        total_found[0] += 1
    elif Path(apath).is_dir() and (Path(apath) / "config.json").exists():
        from .fingerprint import fingerprint_from_listing

        listing = []
        for p in Path(apath).iterdir():
            if p.is_file():
                st = p.stat()
                listing.append({"relative": p.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
        fp = fingerprint_from_listing(apath, listing)
        size = sum(x["size"] for x in listing)
        registry.upsert_artifact(artifact_id, machine_id, model_alias, apath, "safetensors", size, fp, "AVAILABLE")
        registry.add_observation(artifact_id, 1, size, fp, probe_version="1.5", error_code=None)
        total_found[0] += 1
    else:
        registry.upsert_artifact(artifact_id, machine_id, model_alias, apath, None, None, None, "MISSING")
        registry.add_observation(artifact_id, 0, None, None, probe_version="1.5", error_code="E_ARTIFACT_MISSING")


def sync_machine(*, registry: Registry, config: dict[str, Any], machine_id: str, deep_hash: bool = False) -> dict[str, Any]:
    """Probe one machine's inventory and record artifacts.

    For simulation hosts (ssh host containing ``example.internal``) or hosts
    without ssh config, the local filesystem is scanned. Real hosts are
    scanned through SSH with roots sent over stdin.
    """
    m = config.get("machines", {}).get(machine_id)
    if not m:
        raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=f"machine {machine_id} not found", machine=machine_id)
    ssh = m.get("ssh", {})
    host = ssh.get("host")
    roots = m.get("inventory", {}).get("roots", [])

    found: list[dict[str, Any]] = []
    reachable = False
    scan_method = "local-filesystem"
    if host and "example.internal" not in host:
        from ..transport.ssh import SSHTransport

        t = SSHTransport(host, ssh.get("user"), ssh.get("port"), ssh.get("password_file"))
        ok, detail = t.check_reachable(timeout=10)
        if not ok:
            raise ModelctlError(code="E_SSH_UNREACHABLE", message=f"{machine_id}: {detail}", machine=machine_id)
        reachable = True
        scan_method = "ssh"
        found = remote_scan(t, roots)
    else:
        scan_method = "local-filesystem"
        found = scan_local_roots(roots)

    targets = [tid for tid, t in config.get("targets", {}).items() if t["machine"] == machine_id]
    total_found: list[int] = [0]
    for tid in targets:
        t = config["targets"][tid]
        apath = t["artifact"]["path"]
        match = next((f for f in found if f["canonical_path"] == apath), None)
        _record_artifact(registry, machine_id, tid, apath, match, t["model"], total_found)

    registry.upsert_machine(machine_id, last_probe_status="OK" if (found or not targets) else "DEGRADED")
    registry.add_event("INVENTORY_PROBE_STARTED", machine_id=machine_id, result="ok", details={"roots": roots, "scan_method": scan_method})
    for tid in targets:
        artifact_id = f"{machine_id}:{config['targets'][tid]['artifact']['path']}"
        art = registry.get_artifact(artifact_id)
        if art and art["current_status"] == "AVAILABLE":
            registry.add_event("INVENTORY_ARTIFACT_FOUND", target_id=tid, machine_id=machine_id, details={"path": art["canonical_path"]})
        else:
            registry.add_event("INVENTORY_ARTIFACT_MISSING", target_id=tid, machine_id=machine_id, details={"path": config["targets"][tid]["artifact"]["path"]})

    return {
        "machine_id": machine_id,
        "scan_method": scan_method,
        "reachable": reachable,
        "dirs_found": len(found),
        "verified": total_found[0],
    }
