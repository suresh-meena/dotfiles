"""vLLM lifecycle: start/stop/restart/status/ps/logs/events/reconcile/gc/doctor."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from typing import Any

import click

from ..config.resolver import resolve_target
from ..diagnostics import doctor as doctor_fn
from ..errors import ModelctlError
from .base import Ctx, emit, handle_error, load_pair, pass_ctx


def _target(model: str, machine: str) -> str:
    return f"{model}@{machine}"


@click.command("start")
@click.argument("model", required=True)
@click.option("--machine", required=True, help="machine alias")
@click.option("--wait/--no-wait", default=True)
@click.option("--replace", is_flag=True)
@click.option("--ttl", type=str, default=None)
@click.option("--persistent", is_flag=True)
@pass_ctx
def start_cmd(ctx: Ctx, model, machine, wait, replace, ttl, persistent):
    try:
        from ..lifecycle.start import start_target

        cfg, reg = load_pair(ctx)
        result = start_target(registry=reg, config=cfg, target_id=_target(model, machine), replace=replace, ttl=None if persistent else ttl, wait=wait, trace_id=ctx.trace_id)
        emit(result, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("stop")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("--force", is_flag=True)
@pass_ctx
def stop_cmd(ctx: Ctx, model, machine, force):
    try:
        from ..lifecycle.stop import stop_target

        cfg, reg = load_pair(ctx)
        emit(stop_target(registry=reg, config=cfg, target_id=_target(model, machine), force=force, trace_id=ctx.trace_id), ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("restart")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("--replace", is_flag=True, default=True)
@pass_ctx
def restart_cmd(ctx: Ctx, model, machine, replace):
    try:
        from ..lifecycle.start import start_target
        from ..lifecycle.stop import stop_target

        cfg, reg = load_pair(ctx)
        target = _target(model, machine)
        try:
            stop_target(registry=reg, config=cfg, target_id=target, force=False, trace_id=ctx.trace_id)
        except ModelctlError as e:
            if e.code in ("E_TARGET_NOT_FOUND", "E_LEAK_SUSPECTED"):
                if e.code == "E_LEAK_SUSPECTED":
                    raise
            pass
        emit(start_target(registry=reg, config=cfg, target_id=target, replace=replace, wait=True, trace_id=ctx.trace_id), ctx)
    except Exception as e:
        handle_error(e, ctx)


def _status_payload(cfg: dict[str, Any], reg, target: str, model: str) -> dict[str, Any]:
    from ..lifecycle.procs import deployment_live, lease_expired
    from ..tunnel import TunnelManager

    dep = reg.deployment_for_target(target)
    if not dep:
        raise ModelctlError(code="E_TARGET_NOT_FOUND", message=f"no deployment for {target}", target=target)
    try:
        resolved = resolve_target(cfg, target)
    except Exception:
        resolved = None
    live = deployment_live(dep)
    vllm = "unknown"
    if dep["state"] == "READY":
        vllm = "healthy" if live else "not_running"
    elif dep["state"] == "STARTING":
        vllm = "starting"
    elif dep["state"] == "LEAK_SUSPECTED":
        vllm = "leak_suspected"
    else:
        vllm = dep["state"].lower()
    ep = TunnelManager(reg).endpoint(target_id=target)
    payload: dict[str, Any] = {
        "ok": True,
        "target": target,
        "CONTROL": "managed" if dep else "unmanaged",
        "PROCESS": "running" if live else "not running",
        "VLLM": vllm,
        "MODEL": model,
        "ARTIFACT": dep.get("artifact_fingerprint", "")[:12] if dep.get("artifact_fingerprint") else "unknown",
        "STATE": dep["state"],
        "CONFIG_DIGEST": dep["config_digest"],
        "TUNNEL": ep.get("local") if ep.get("ok") else "none",
        "deployment": dep,
        "simulation": True,
        "backend": "local-simulation",
    }
    if lease_expired(dep):
        payload["LEASE"] = "expired"
    if dep.get("server_pid"):
        payload["pid"] = dep["server_pid"]
    if resolved:
        payload["bind_host"] = resolved.get("bind_host")
        payload["port"] = resolved.get("port")
    return payload


@click.command("status")
@click.argument("model", required=False)
@click.option("--machine", required=False)
@pass_ctx
def status_cmd(ctx: Ctx, model, machine):
    try:
        cfg, reg = load_pair(ctx)
        if model and machine:
            emit(_status_payload(cfg, reg, _target(model, machine), model), ctx)
            return
        deps = reg.list_deployments(machine=machine)
        rows = [{"TARGET": d["target_id"], "STATE": d["state"], "DEPLOYMENT": d["deployment_id"][:12]} for d in deps]
        emit({"ok": True, "deployments": deps, "_render": {"kind": "table", "columns": ["TARGET", "STATE", "DEPLOYMENT"], "rows": rows}}, ctx)
    except Exception as e:
        handle_error(e, ctx)


def _age(started_at: str | None) -> str:
    if not started_at:
        return "-"
    try:
        age_s = int(datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(started_at).timestamp())
    except Exception:
        return "-"
    if age_s < 60:
        return f"{age_s}s"
    if age_s < 3600:
        return f"{age_s // 60}m"
    return f"{age_s // 3600}h{age_s % 3600 // 60}m"


@click.command("ps")
@click.option("--machine", required=False)
@pass_ctx
def ps_cmd(ctx: Ctx, machine):
    try:
        from ..lifecycle.procs import deployment_live

        _, reg = load_pair(ctx)
        deps = reg.list_deployments(machine=machine)
        latest: dict[str, dict[str, Any]] = {}
        for d in deps:
            tid = d["target_id"]
            if tid not in latest or (d.get("started_at") or "") >= (latest[tid].get("started_at") or ""):
                latest[tid] = d
        out = []
        rows = []
        for d in latest.values():
            live = deployment_live(d)
            row = dict(d)
            row["live"] = live
            out.append(row)
            state = d["state"]
            if state == "READY" and not live:
                state = "READY(dead)"
            rows.append({
                "TARGET": d["target_id"],
                "STATE": state,
                "PID": str(d["server_pid"]) if d.get("server_pid") else "-",
                "PORT": str(d["port"]) if d.get("port") else "-",
                "AGE": _age(d.get("started_at")),
                "LEASE": (d.get("lease_expires_at") or "-")[:19].replace("T", " "),
            })
        columns = ["TARGET", "STATE", "PID", "PORT", "AGE", "LEASE"]
        emit({"ok": True, "targets": out, "simulation": True, "backend": "local-simulation", "_render": {"kind": "table", "columns": columns, "rows": rows}}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("logs")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("-f", "follow", is_flag=True)
@click.option("--since", type=str, default=None)
@pass_ctx
def logs_cmd(ctx: Ctx, model, machine, follow, since):
    try:
        from ..state import STATE_ROOT

        _, reg = load_pair(ctx)
        target = _target(model, machine)
        dep = reg.deployment_for_target(target)
        if not dep:
            raise ModelctlError(code="E_TARGET_NOT_FOUND", message=f"no deployment for {target}", target=target)
        gen_dir = STATE_ROOT / "generated" / dep["deployment_id"].replace(":", "_").replace("/", "_")
        log_file = gen_dir / "vllm.log"
        if log_file.exists():
            content = log_file.read_text()[-4000:]
        else:
            content = f"(no log file yet for {dep['deployment_id']}; unit {dep['supervisor_unit']})\nTry journalctl --user -u {dep['supervisor_unit']} on {machine} via ssh"
        emit({"ok": True, "target": target, "log": content, "_render": {"kind": "text", "text": content}}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("events")
@click.option("--machine", required=False)
@click.option("--target", required=False)
@click.option("--limit", type=int, default=50)
@pass_ctx
def events_cmd(ctx: Ctx, machine, target, limit):
    try:
        _, reg = load_pair(ctx)
        emit({"ok": True, "events": reg.list_events(machine=machine, target=target, limit=limit)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("reconcile")
@click.option("--machine", required=False)
@click.option("--fix-safe", is_flag=True)
@pass_ctx
def reconcile_cmd(ctx: Ctx, machine, fix_safe):
    try:
        from ..lifecycle.reconcile import reconcile

        cfg, reg = load_pair(ctx)
        emit(reconcile(registry=reg, config=cfg, machine=machine, fix_safe=fix_safe), ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("gc")
@click.option("--machine", required=False)
@pass_ctx
def gc_cmd(ctx: Ctx, machine):
    try:
        from ..lifecycle.reconcile import reconcile
        from ..state import STATE_ROOT

        _, reg = load_pair(ctx)
        gen_root = STATE_ROOT / "generated"
        removed = 0
        if gen_root.exists():
            active_ids = {d["deployment_id"].replace(":", "_").replace("/", "_") for d in reg.list_deployments()}
            for p in gen_root.iterdir():
                try:
                    if time.time() - p.stat().st_mtime > 7 * 86400 and p.name not in active_ids:
                        shutil.rmtree(p)
                        removed += 1
                except Exception:
                    pass
        rec = reconcile(registry=reg, config={"machines": {}, "models": {}, "targets": {}}, machine=machine, fix_safe=True)
        emit({"ok": True, "removed_generated": removed, "reconcile": rec}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("doctor")
@click.option("--machine", required=False)
@click.option("--target", required=False)
@pass_ctx
def doctor_cmd(ctx: Ctx, machine, target):
    try:
        cfg, reg = load_pair(ctx)
        emit(doctor_fn(registry=reg, config=cfg, machine=machine, target=target), ctx)
    except Exception as e:
        handle_error(e, ctx)
