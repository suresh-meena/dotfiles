from __future__ import annotations

import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config.resolver import resolve_target, target_digest
from ..domain import deployment_id as make_deployment_id
from ..errors import ModelctlError
from ..inventory.registry import Registry, utc_now
from ..inventory.sync import sync_machine
from ..events import EventLog
from ..supervisor import unit_name, render_unit, exec_start_for_vllm
from ..runtime import RuntimeAdapter, port_busy, spawn_local_fake, pid_alive
from ..gpu.reservations import try_reserve_gpus, update_owner_pid, release_for_owner
from ..state import STATE_ROOT, ensure_state_dirs
from .procs import compute_lease_expiry, deployment_live, deployment_live_ext, terminate_verified
from .remote import remote_lifecycle


def _gpu_uuids(machine_id: str, gpus: list[int]) -> list[str]:
    return [f"{machine_id}-gpu-{i}" for i in gpus]


def _refresh_artifact(registry: Registry, config: dict[str, Any], machine_id: str, target_id: str) -> None:
    try:
        sync_machine(registry=registry, config=config, machine_id=machine_id)
    except ModelctlError as e:
        raise ModelctlError(code="E_ARTIFACT_STALE", message=f"artifact auto-refresh failed: {e.message}", target=target_id, machine=machine_id)


def _check_inventory(registry: Registry, config: dict[str, Any], resolved: dict[str, Any], target_id: str, machine_id: str) -> None:
    inventory_cfg = config.get("machines", {}).get(machine_id, {}).get("inventory", {})
    max_age = inventory_cfg.get("max_age_before_start_s", 3600)
    auto_refresh = inventory_cfg.get("auto_refresh_before_start", True)
    artifact_path = resolved["artifact"]["path"]
    artifact_id = f"{machine_id}:{artifact_path}"
    art = registry.get_artifact(artifact_id)
    if not resolved["artifact"].get("require_observed"):
        return
    if not art or art.get("current_status") != "AVAILABLE":
        raise ModelctlError(code="E_ARTIFACT_MISSING", message=f"artifact not observed available: {artifact_path}", target=target_id, machine=machine_id)
    if art.get("last_seen_at"):
        try:
            ts = datetime.fromisoformat(art["last_seen_at"]).timestamp()
            age = time.time() - ts
            if age > max_age:
                if auto_refresh:
                    _refresh_artifact(registry, config, machine_id, target_id)
                    art = registry.get_artifact(artifact_id)
                    if not art or art.get("current_status") != "AVAILABLE":
                        raise ModelctlError(code="E_ARTIFACT_STALE", message=f"artifact observation stale after auto-refresh: {artifact_path}", target=target_id, machine=machine_id)
                    ts = datetime.fromisoformat(art["last_seen_at"]).timestamp()
                    if time.time() - ts > max_age:
                        raise ModelctlError(code="E_ARTIFACT_STALE", message=f"artifact observation stale ({int(time.time() - ts)}s ago) after auto-refresh: {artifact_path}", target=target_id, machine=machine_id)
                else:
                    raise ModelctlError(code="E_ARTIFACT_STALE", message=f"artifact stale: {artifact_path}", target=target_id)
        except ModelctlError:
            raise
        except Exception:
            pass


def _mark_terminal(registry: Registry, dep_id: str, state: str, stopped_at: str | None = None) -> None:
    conn = registry.conn()
    if stopped_at:
        conn.execute("UPDATE deployments SET state=?, stopped_at=? WHERE deployment_id=?", (state, stopped_at, dep_id))
    else:
        conn.execute("UPDATE deployments SET state=? WHERE deployment_id=?", (state, dep_id))
    conn.commit()


def _spawn_and_wait(registry: Registry, resolved: dict[str, Any], dep_id: str, target_id: str, machine_id: str, unit: str, artifact_fp: str | None, digest: str, artifact_id: str, gpu_uuids: list[str], lease_expires_at: str | None, wait: bool, events: EventLog, trace_id: str) -> dict[str, Any]:
    runtime = RuntimeAdapter()
    port = resolved.get("port", 8000)
    host = resolved.get("bind_host", "127.0.0.1")
    served_model = resolved.get("served_model_name", resolved["model"])
    gen_dir = STATE_ROOT / "generated" / dep_id.replace(":", "_").replace("/", "_")
    gen_dir.mkdir(parents=True, exist_ok=True)
    log_file = gen_dir / "vllm.log"

    proc = spawn_local_fake(served_model, port, log_file, child=True)
    pid = proc.pid
    for u in gpu_uuids:
        update_owner_pid(u, dep_id, pid)

    registry.upsert_deployment(dep_id, target_id, machine_id, digest, artifact_id, artifact_fp, "STARTING", unit, server_pid=pid, port=port, lease_expires_at=lease_expires_at)
    events.emit("SERVICE_STARTED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="spawned", details={"pid": pid, "port": port}, trace_id=trace_id)

    if not wait:
        return {
            "ok": True,
            "target": target_id,
            "deployment_id": dep_id,
            "state": "STARTING",
            "config_digest": digest,
            "unit": unit,
            "port": port,
            "pid": pid,
            "trace_id": trace_id,
            "simulation": True,
            "backend": "local-simulation",
        }

    deadline_s = float(resolved.get("startup_timeout_s", 1200))
    deadline = time.time() + deadline_s
    ready = False
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        if runtime.check_health(host, port) and runtime.check_model_identity(host, port, served_model):
            ready = True
            break
        time.sleep(0.25)

    if not pid_alive(pid):
        _mark_terminal(registry, dep_id, "FAILED_START")
        release_for_owner(gpu_uuids, dep_id)
        events.emit("START_FAILED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="process_exited", trace_id=trace_id)
        raise ModelctlError(code="E_START_EXITED", message=f"server process exited during startup (pid {pid})", target=target_id, machine=machine_id)

    if not ready:
        terminate_verified({"server_pid": pid, "port": port}, graceful_timeout_s=15, kill_timeout_s=10)
        _mark_terminal(registry, dep_id, "FAILED_HEALTH")
        release_for_owner(gpu_uuids, dep_id)
        events.emit("START_FAILED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="health_timeout", trace_id=trace_id)
        raise ModelctlError(code="E_HEALTH_FAILED", message=f"health/identity check failed within {int(deadline_s)}s", target=target_id, machine=machine_id)

    ready_at = utc_now()
    registry.upsert_deployment(dep_id, target_id, machine_id, digest, artifact_id, artifact_fp, "READY", unit, ready_at=ready_at, server_pid=pid, port=port, lease_expires_at=lease_expires_at)
    events.emit("HEALTH_READY", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="ok", trace_id=trace_id)
    events.emit("DEPLOYMENT_READY", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="ready", trace_id=trace_id)

    return {
        "ok": True,
        "target": target_id,
        "deployment_id": dep_id,
        "state": "READY",
        "config_digest": digest,
        "unit": unit,
        "port": port,
        "pid": pid,
        "endpoint": f"http://127.0.0.1:{port}/v1",
        "trace_id": trace_id,
        "simulation": True,
        "backend": "local-simulation",
    }


def _remote_start_and_wait(registry: Registry, rl: Any, resolved: dict[str, Any], dep_id: str, target_id: str, machine_id: str, unit: str, artifact_fp: str | None, digest: str, artifact_id: str, gpu_uuids: list[str], lease_expires_at: str | None, wait: bool, events: EventLog, trace_id: str, cfg_yaml_text: str) -> dict[str, Any]:
    """Real remote lifecycle over SSH: upload config, launch vLLM detached,
    then either return immediately with a STARTING guidance message or block
    until /health + model identity pass (fail-closed READY)."""
    port = int(resolved.get("port", 8000))
    served = resolved.get("served_model_name", resolved["model"])
    activate = (resolved.get("machine_runtime", {}) or {}).get("activate")
    base = (Path(activate).parent / "modelctl") if activate else Path("/tmp/modelctl")
    dep_dir = f"{base}/{dep_id.replace(':', '_')}"
    remote_yaml = f"{dep_dir}/vllm.yaml"
    remote_log = f"{dep_dir}/vllm.log"
    script_path = f"{dep_dir}/launch.sh"

    registry.upsert_deployment(dep_id, target_id, machine_id, digest, artifact_id, artifact_fp, "STARTING", unit, port=port, lease_expires_at=lease_expires_at)

    def _fail(code: str, message: str, state: str, result: str, tail: str | None = None) -> ModelctlError:
        _mark_terminal(registry, dep_id, state)
        release_for_owner(gpu_uuids, dep_id)
        details = {"log_tail": (tail or "")[-1500:]} if tail is not None else None
        events.emit("START_FAILED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result=result, details=details, trace_id=trace_id)
        return ModelctlError(code=code, message=message, target=target_id, machine=machine_id, details=details)

    try:
        rl.mkdir_p(dep_dir)
        rl.write_file(remote_yaml, cfg_yaml_text)
        rl.write_file(script_path, rl.build_launch_script(resolved, remote_yaml, remote_log))
        pid = rl.launch_detached(script_path, remote_log)
    except ModelctlError as e:
        raise _fail("E_START_EXITED", f"remote launch failed: {e.message}", "FAILED_START", "launch_error")

    # sanity: process must survive the first seconds
    time.sleep(3)
    if not rl.pid_alive(pid):
        tail = rl.log_tail(remote_log)
        raise _fail("E_START_EXITED", f"remote server exited during startup (pid {pid})", "FAILED_START", "process_exited", tail)

    for u in gpu_uuids:
        update_owner_pid(u, dep_id, pid)
    registry.upsert_deployment(dep_id, target_id, machine_id, digest, artifact_id, artifact_fp, "STARTING", unit, ready_at=None, server_pid=pid, port=port, lease_expires_at=lease_expires_at)
    events.emit("SERVICE_STARTED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="spawned", details={"pid": pid, "port": port, "backend": "ssh-remote"}, trace_id=trace_id)

    poll_hint = f"modelctl --json status {target_id}"
    loading_msg = (
        f"Everything is working: vLLM was launched on '{machine_id}' (pid {pid}, port {port}) and is now "
        f"loading the model weights. First readiness usually takes a few minutes. "
        f"Poll `{poll_hint}` until STATE=READY."
    )
    resp: dict[str, Any] = {
        "ok": True,
        "target": target_id,
        "deployment_id": dep_id,
        "state": "STARTING",
        "config_digest": digest,
        "unit": unit,
        "port": port,
        "pid": pid,
        "remote_log": remote_log,
        "message": loading_msg,
        "expected_ready_s": int(resolved.get("startup_timeout_s", 1200)),
        "poll": poll_hint,
        "trace_id": trace_id,
        "simulation": False,
        "backend": "ssh-remote",
    }

    if wait:
        deadline = time.time() + float(resolved.get("startup_timeout_s", 1200))
        ready = False
        while time.time() < deadline:
            if not rl.pid_alive(pid):
                break
            healthy, identity = rl.health(port, served)
            if healthy and identity:
                ready = True
                break
            time.sleep(5)
        if not rl.pid_alive(pid):
            tail = rl.log_tail(remote_log)
            raise _fail("E_START_EXITED", f"remote server exited during startup (pid {pid})", "FAILED_START", "process_exited", tail)
        if not ready:
            rl.terminate({"server_pid": pid, "port": port}, graceful_timeout_s=15, kill_timeout_s=10)
            raise _fail("E_HEALTH_FAILED", f"health/identity check failed within {int(resolved.get('startup_timeout_s', 1200))}s", "FAILED_HEALTH", "health_timeout", rl.log_tail(remote_log))
        registry.upsert_deployment(dep_id, target_id, machine_id, digest, artifact_id, artifact_fp, "READY", unit, ready_at=utc_now(), server_pid=pid, port=port, lease_expires_at=lease_expires_at)
        events.emit("HEALTH_READY", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="ok", trace_id=trace_id)
        events.emit("DEPLOYMENT_READY", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="ready", trace_id=trace_id)
        resp.update({"state": "READY", "endpoint": f"http://127.0.0.1:{port}/v1", "message": None})
    return resp


def start_target(*, registry: Registry, config: dict[str, Any], target_id: str, replace: bool = False, ttl: str | None = None, wait: bool = True, trace_id: str | None = None) -> dict[str, Any]:
    trace_id = trace_id or uuid.uuid4().hex[:12]
    events = EventLog(registry)
    events.emit("START_REQUESTED", target_id=target_id, result="requested", trace_id=trace_id)

    resolved = resolve_target(config, target_id)
    digest = target_digest(resolved)
    machine_id = resolved["machine"]
    model_alias = resolved["model"]
    artifact_path = resolved["artifact"]["path"]
    artifact_id = f"{machine_id}:{artifact_path}"
    port = resolved.get("port", 8000)
    host = resolved.get("bind_host", "127.0.0.1")
    gpu_uuids = _gpu_uuids(machine_id, resolved.get("gpus", []))
    rl = remote_lifecycle(config, machine_id)

    _check_inventory(registry, config, resolved, target_id, machine_id)

    existing = registry.deployment_for_target(target_id)
    if existing:
        dep_state = existing["state"]
        if existing["config_digest"] == digest:
            if dep_state == "READY" and deployment_live_ext(existing, rl):
                if ttl:
                    # extend/reapply the lease on the live deployment
                    conn = registry.conn()
                    conn.execute("UPDATE deployments SET lease_expires_at=? WHERE deployment_id=?", (compute_lease_expiry(ttl), existing["deployment_id"]))
                    conn.commit()
                events.emit("DEPLOYMENT_READY", target_id=target_id, deployment_id=existing["deployment_id"], machine_id=machine_id, result="idempotent", trace_id=trace_id)
                return {"ok": True, "target": target_id, "deployment_id": existing["deployment_id"], "state": "READY", "config_digest": digest, "idempotent": True, "trace_id": trace_id, "simulation": True, "backend": "local-simulation"}
            if dep_state == "READY" and not deployment_live(existing):
                # stale READY row: the process is gone; record it and continue
                _mark_terminal(registry, existing["deployment_id"], "STOPPED", stopped_at=utc_now())
                release_for_owner(_gpu_uuids(existing.get("machine_id") or machine_id, resolved.get("gpus", [])), existing["deployment_id"])
            elif dep_state == "STARTING":
                pid_ok = rl.pid_alive(existing.get("server_pid")) if rl else pid_alive(existing.get("server_pid"))
                if pid_ok:
                    if wait:
                        from .procs import finish_pending_start

                        ok, detail = finish_pending_start(
                            registry, existing, host=host, port=port, served_model=resolved.get("served_model_name", model_alias), deadline_s=float(resolved.get("startup_timeout_s", 1200))
                        )
                        if ok:
                            events.emit("DEPLOYMENT_READY", target_id=target_id, deployment_id=existing["deployment_id"], machine_id=machine_id, result="finished_pending", trace_id=trace_id)
                            return {"ok": True, "target": target_id, "deployment_id": existing["deployment_id"], "state": "READY", "config_digest": digest, "idempotent": True, "trace_id": trace_id, "simulation": True, "backend": "local-simulation"}
                        raise ModelctlError(code="E_HEALTH_FAILED", message=f"pending start did not become healthy: {detail}", target=target_id, machine=machine_id)
                    raise ModelctlError(code="E_GPU_RESERVATION_CONFLICT", message=f"start already in progress for {target_id} (--no-wait); retry with --wait or wait for it", target=target_id, details={"deployment_id": existing["deployment_id"]})
                else:
                    _mark_terminal(registry, existing["deployment_id"], "FAILED_START")
        else:
            # different digest
            if dep_state == "LEAK_SUSPECTED":
                raise ModelctlError(code="E_LEAK_SUSPECTED", message=f"target {target_id} is in LEAK_SUSPECTED; resolve via doctor before replace", target=target_id)
            if dep_state in ("READY", "STARTING", "DRAINING", "STOPPING"):
                if not replace:
                    raise ModelctlError(code="E_GPU_RESERVATION_CONFLICT", message=f"deployment already exists for {target_id} with different digest; use --replace", target=target_id, details={"existing_digest": existing["config_digest"], "requested_digest": digest})
                # replace: really stop the old deployment first
                term = terminate_verified(existing, graceful_timeout_s=float(resolved.get("graceful_stop_timeout_s", 30)), kill_timeout_s=float(resolved.get("kill_timeout_s", 15)))
                if not term["ok"]:
                    _mark_terminal(registry, existing["deployment_id"], "LEAK_SUSPECTED")
                    events.emit("LEAK_SUSPECTED", target_id=target_id, deployment_id=existing["deployment_id"], machine_id=machine_id, result="leak", trace_id=trace_id)
                    raise ModelctlError(code="E_LEAK_SUSPECTED", message=f"replace failed: could not verify termination of {existing['deployment_id']}: {term['detail']}", target=target_id, machine=machine_id)
                release_for_owner(_gpu_uuids(machine_id, resolved.get("gpus", [])), existing["deployment_id"])
                _mark_terminal(registry, existing["deployment_id"], "STOPPED", stopped_at=utc_now())
                events.emit("DEPLOYMENT_STOPPED", target_id=target_id, deployment_id=existing["deployment_id"], machine_id=machine_id, result="replaced", trace_id=trace_id)

    # port preflight (remote machines: check on the machine itself)
    if rl:
        if rl.port_busy(port):
            raise ModelctlError(code="E_PORT_BUSY", message=f"port {port} on {machine_id} is already in use", target=target_id, machine=machine_id)
    elif port_busy(host, port):
        raise ModelctlError(code="E_PORT_BUSY", message=f"port {port} on {host} is already in use", target=target_id, machine=machine_id)

    nonce = uuid.uuid4().hex[:8]
    dep_id = make_deployment_id(target_id, digest, nonce)
    unit = unit_name(target_id, digest)
    ensure_state_dirs()

    # GPU reservation (pid 0 = in-flight, bound after spawn). The deployment
    # id is the reservation owner so stop --replace can release it by owner.
    if gpu_uuids:
        ok, busy = try_reserve_gpus(gpu_uuids, dep_id)
        if not ok:
            raise ModelctlError(code="E_GPU_BUSY_FOREIGN", message=f"GPU {busy[0]} busy", target=target_id, machine=machine_id, details={"gpu_uuid": busy[0]})

    events.emit("GPU_RESERVED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, trace_id=trace_id)
    events.emit("PREFLIGHT_PASSED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, trace_id=trace_id)

    runtime = RuntimeAdapter()
    runtime.validate_target(resolved)
    gen_dir = STATE_ROOT / "generated" / dep_id.replace(":", "_").replace("/", "_")
    gen_dir.mkdir(parents=True, exist_ok=True)
    cfg_yaml = gen_dir / "vllm.yaml"
    runtime.generate_config(resolved, cfg_yaml)
    activate = resolved.get("machine_runtime", {}).get("activate")
    exec_start = exec_start_for_vllm(activate=activate, config_yaml_path=str(cfg_yaml))
    unit_content = render_unit(deployment_id=dep_id, exec_start=exec_start, env_file=None)
    unit_path = gen_dir / unit
    unit_path.write_text(unit_content)
    try:
        unit_path.chmod(0o600)
    except Exception:
        pass

    art = registry.get_artifact(artifact_id)
    lease_expires_at = compute_lease_expiry(ttl)
    if rl:
        return _remote_start_and_wait(
            registry, rl, resolved, dep_id, target_id, machine_id, unit,
            art.get("manifest_fingerprint") if art else None, digest, artifact_id,
            gpu_uuids, lease_expires_at, wait, events, trace_id,
            cfg_yaml.read_text(),
        )
    return _spawn_and_wait(registry, resolved, dep_id, target_id, machine_id, unit, art.get("manifest_fingerprint") if art else None, digest, artifact_id, gpu_uuids, lease_expires_at, wait, events, trace_id)