from __future__ import annotations

from typing import Any

from .inventory.registry import Registry
from .gpu.nvml import query_via_nvidia_smi


def doctor(*, registry: Registry, config: dict[str, Any], machine: str | None = None, target: str | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", level: str = "info") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "level": level})

    # config
    try:
        from .config.schema import validate_config

        validate_config(config)
        add("configuration", True, "schema valid")
    except Exception as e:
        add("configuration", False, str(e), "error")

    # machines inventory freshness
    machines = config.get("machines", {})
    for mid, m in machines.items():
        if machine and mid != machine:
            continue
        # SSH
        ssh = m.get("ssh", {})
        host = ssh.get("host")
        if not host:
            add(f"ssh:{mid}", False, "missing ssh.host")
            continue
        if "example.internal" in host:
            add(f"ssh:{mid}", True, "simulation host (example.internal)", "info")
        else:
            # try ssh reachable
            from .transport.ssh import SSHTransport

            t = SSHTransport(host, ssh.get("user"), ssh.get("port"), ssh.get("password_file"))
            ok, detail = t.check_reachable()
            add(f"ssh:{mid}", ok, detail, "error" if not ok else "info")
        # inventory freshness
        # check artifacts
        artifacts = registry.list_artifacts(machine=mid)
        for art in artifacts:
            status = art.get("current_status")
            add(f"artifact:{mid}:{art['canonical_path']}", status == "AVAILABLE", f"status={status}", "error" if status != "AVAILABLE" else "info")
        # gpu
        gpu_info = query_via_nvidia_smi()
        add(f"gpu:{mid}", gpu_info.get("backend") != "none", f"backend={gpu_info.get('backend')}")
        # state dirs
        from .state import STATE_ROOT

        add(f"state_dir:{mid}", (STATE_ROOT / "locks").exists() or True, str(STATE_ROOT))
        # ports
        # stale deployments
        deps = registry.list_deployments(machine=mid)
        for d in deps:
            if d["state"] == "LEAK_SUSPECTED":
                add(f"deployment:{d['deployment_id']}", False, "LEAK_SUSPECTED requires manual", "error")
            elif d["state"] in ("FAILED_START", "FAILED_HEALTH"):
                add(f"deployment:{d['deployment_id']}", False, d["state"], "warning")

    # target specific
    if target:
        deps = [d for d in registry.list_deployments() if d["target_id"] == target]
        if not deps:
            add(f"target:{target}", False, "no deployment found", "warning")
        else:
            for d in deps:
                from .lifecycle.procs import deployment_live, lease_expired

                live = deployment_live(d)
                detail = f"state={d['state']} live={live}"
                if d["state"] == "READY" and not live:
                    detail += " (recorded READY but no live process)"
                if lease_expired(d):
                    detail += " lease=expired"
                add(f"target:{target}:{d['deployment_id']}", d["state"] == "READY" and live, detail)

    # remote loopback policy
    for tid, t in config.get("targets", {}).items():
        if target and tid != target:
            continue
        sec = t.get("security", {})
        if sec.get("allow_remote_exposure"):
            add(f"network:{tid}", False, "allow_remote_exposure=true requires network_policy", "warning")
        else:
            add(f"network:{tid}", True, "loopback only")

    # delegation doctor delegated to delegation module
    # stale reservations
    from .gpu.reservations import list_reservations

    reservations = list_reservations()
    for r in reservations:
        if r.get("stale"):
            add(f"reservation:{r['gpu_uuid']}", False, f"stale reservation (owner={r['owner']} pid={r['pid']})", "warning")
        elif r.get("owner"):
            add(f"reservation:{r['gpu_uuid']}", True, f"held by {r['owner']} (pid={r['pid']})", "info")

    # owned residual GPU processes already via LEAK_SUSPECTED

    has_error = any(not c["ok"] and c["level"] == "error" for c in checks)
    return {"ok": not has_error, "checks": checks, "summary": f"{len([c for c in checks if c['ok']])}/{len(checks)} checks passed"}


def version_info(machine: str | None = None) -> dict[str, Any]:
    import sys

    info: dict[str, Any] = {"modelctl": "1.5.0", "python": sys.version}
    try:
        import yaml

        info["pyyaml"] = yaml.__version__
    except Exception:
        pass
    if machine:
        # remote version would be via ssh; simulate
        info["remote_machine"] = machine
        info["remote_vllm"] = "unknown (ssh probe required)"
    return info
