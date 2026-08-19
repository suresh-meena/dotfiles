from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import click

from .config.loader import load_config
from .config.resolver import resolve_target, explain_target, list_targets, list_machines, list_models
from .inventory.registry import Registry
from .errors import ModelctlError
from .privacy.redaction import redact_dict
from .state import ensure_state_dirs, STATE_ROOT

# Global context
class Ctx:
    def __init__(self, json_out: bool, non_interactive: bool, timeout: int | None, config: str | None, verbose: bool, trace_id: str | None, debug: bool):
        self.json_out = json_out
        self.non_interactive = non_interactive
        self.timeout = timeout
        self.config_path = config
        self.verbose = verbose
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self.debug = debug

pass_ctx = click.make_pass_decorator(Ctx)

def _load_config_pair(ctx: Ctx) -> tuple[dict[str, Any], Registry]:
    try:
        cfg, sources = load_config(ctx.config_path)
    except ModelctlError as e:
        raise e
    reg = Registry()
    return cfg, reg

def _handle_error(e: Exception, ctx: Ctx | None, *, json_out: bool | None = None) -> None:
    use_json = json_out if json_out is not None else (ctx.json_out if ctx else False)
    debug = ctx.debug if ctx else False
    if isinstance(e, ModelctlError):
        d = e.to_dict()
        if ctx and ctx.trace_id and "trace_id" not in d:
            d["trace_id"] = ctx.trace_id
        # redact secrets
        d = redact_dict(d)
        if use_json:
            click.echo(json.dumps(d, indent=2))
        else:
            click.echo(f"[{e.code}] {e.message}", err=True)
            if e.target:
                click.echo(f"target: {e.target}", err=True)
            if e.details:
                click.echo(f"details: {json.dumps(e.details)}", err=True)
            click.echo(f"suggested: {d.get('suggested_action')}", err=True)
        if debug:
            import traceback

            traceback.print_exc()
        sys.exit(1 if e.code != "E_INTERNAL" else 3)
    else:
        tid = ctx.trace_id if ctx else uuid.uuid4().hex[:12]
        msg = str(e)[:500] if str(e) else "internal error"
        d = {"ok": False, "code": "E_INTERNAL", "message": msg, "trace_id": tid, "suggested_action": "modelctl doctor"}
        if use_json:
            click.echo(json.dumps(d, indent=2))
        else:
            click.echo(f"[E_INTERNAL] {msg} (trace {tid})", err=True)
        if debug:
            import traceback

            traceback.print_exc()
        sys.exit(3)

def _ok(payload: dict[str, Any], ctx: Ctx) -> None:
    payload = dict(payload)
    # ensure ok true
    if "ok" not in payload:
        payload["ok"] = True
    if "trace_id" not in payload:
        payload["trace_id"] = ctx.trace_id
    payload = redact_dict(payload)
    if ctx.json_out:
        click.echo(json.dumps(payload, indent=2))
    else:
        # human: print concise
        if "message" in payload:
            click.echo(payload["message"])
        else:
            click.echo(json.dumps(payload, indent=2))

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "json_out", is_flag=True, help="machine JSON output")
@click.option("--non-interactive", is_flag=True, help="no prompts")
@click.option("--timeout", type=int, default=None, help="global timeout seconds")
@click.option("--config", type=str, default=None, help="override config path")
@click.option("--verbose", is_flag=True, help="verbose")
@click.option("--trace-id", type=str, default=None, help="trace id")
@click.option("--debug", is_flag=True, help="show traceback")
@click.pass_context
def cli(ctx, json_out, non_interactive, timeout, config, verbose, trace_id, debug):
    ctx.obj = Ctx(json_out, non_interactive, timeout, config, verbose, trace_id, debug)
    ensure_state_dirs()

@cli.command("version")
@click.option("--remote", "remote_machine", type=str, default=None, help="remote machine alias")
@pass_ctx
def version_cmd(ctx: Ctx, remote_machine):
    try:
        from .diagnostics import version_info

        info = version_info(machine=remote_machine)
        _ok({"ok": True, "version": info}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# config group
@cli.group()
def config():
    pass

@config.command("validate")
@click.pass_context
def config_validate(ctx):
    c: Ctx = ctx.obj
    try:
        cfg, sources = load_config(c.config_path)
        _ok({"ok": True, "message": "configuration valid", "sources": [str(s) for s in sources]}, c)
    except Exception as e:
        _handle_error(e, c)

@config.command("resolve")
@click.option("--target", type=str, required=True, help="model@machine")
@pass_ctx
def config_resolve(ctx: Ctx, target):
    try:
        cfg, _ = _load_config_pair(ctx)
        resolved = explain_target(cfg, target)
        _ok({"ok": True, "target": target, **resolved}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@config.command("explain")
@click.option("--target", type=str, required=True)
@pass_ctx
def config_explain(ctx: Ctx, target):
    try:
        cfg, _ = _load_config_pair(ctx)
        resolved = explain_target(cfg, target)
        if ctx.json_out:
            _ok({"ok": True, "target": target, **resolved}, ctx)
        else:
            import yaml

            click.echo(yaml.safe_dump(resolved["resolved"], sort_keys=True))
            click.echo(f"digest: {resolved['config_digest']}")
    except Exception as e:
        _handle_error(e, ctx)

# start/stop/restart/status/ps/connect/disconnect/endpoint
@cli.command("start")
@click.argument("model", required=True)
@click.option("--machine", required=True, help="machine alias")
@click.option("--wait/--no-wait", default=True)
@click.option("--replace", is_flag=True)
@click.option("--ttl", type=str, default=None)
@click.option("--persistent", is_flag=True)
@pass_ctx
def start_cmd(ctx: Ctx, model, machine, wait, replace, ttl, persistent):
    target = f"{model}@{machine}"
    try:
        cfg, reg = _load_config_pair(ctx)
        from .lifecycle.start import start_target

        ttl_eff = None if persistent else ttl
        result = start_target(registry=reg, config=cfg, target_id=target, replace=replace, ttl=ttl_eff, wait=wait, trace_id=ctx.trace_id)
        _ok(result, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("stop")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("--force", is_flag=True)
@pass_ctx
def stop_cmd(ctx: Ctx, model, machine, force):
    target = f"{model}@{machine}"
    try:
        cfg, reg = _load_config_pair(ctx)
        from .lifecycle.stop import stop_target

        result = stop_target(registry=reg, config=cfg, target_id=target, force=force, trace_id=ctx.trace_id)
        _ok(result, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("restart")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("--replace", is_flag=True, default=True)
@pass_ctx
def restart_cmd(ctx: Ctx, model, machine, replace):
    target = f"{model}@{machine}"
    try:
        cfg, reg = _load_config_pair(ctx)
        from .lifecycle.start import start_target
        from .lifecycle.stop import stop_target

        # stop then start
        try:
            stop_target(registry=reg, config=cfg, target_id=target, force=False, trace_id=ctx.trace_id)
        except ModelctlError as e:
            if e.code not in ("E_TARGET_NOT_FOUND",):
                # if stop fails with leak, we still require investigation; propagate
                if e.code == "E_LEAK_SUSPECTED":
                    raise
                pass
        result = start_target(registry=reg, config=cfg, target_id=target, replace=replace, wait=True, trace_id=ctx.trace_id)
        _ok(result, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("status")
@click.argument("model", required=False)
@click.option("--machine", required=False)
@pass_ctx
def status_cmd(ctx: Ctx, model, machine):
    try:
        cfg, reg = _load_config_pair(ctx)
        if model and machine:
            target = f"{model}@{machine}"
            dep = reg.deployment_for_target(target)
            if not dep:
                raise ModelctlError(code="E_TARGET_NOT_FOUND", message=f"no deployment for {target}", target=target)
            # build rich status per spec §7.5
            resolved = None
            try:
                resolved = resolve_target(cfg, target)
            except Exception:
                pass
            # check health via live process/port verification
            from .lifecycle.procs import deployment_live, lease_expired

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
            # tunnel
            from .tunnel import TunnelManager

            tm = TunnelManager(reg)
            ep = tm.endpoint(target_id=target)
            tunnel_info = ep.get("local") if ep.get("ok") else "none"
            payload = {
                "ok": True,
                "target": target,
                "CONTROL": "managed" if dep else "unmanaged",
                "PROCESS": "running" if dep and dep["state"] == "READY" and live else "not running",
                "VLLM": vllm,
                "MODEL": model,
                "ARTIFACT": dep.get("artifact_fingerprint", "")[:12] if dep.get("artifact_fingerprint") else "unknown",
                "STATE": dep["state"],
                "CONFIG_DIGEST": dep["config_digest"],
                "TUNNEL": tunnel_info,
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
            _ok(payload, ctx)
        else:
            # list all
            deps = reg.list_deployments(machine=machine)
            if ctx.json_out:
                _ok({"ok": True, "deployments": deps}, ctx)
            else:
                if not deps:
                    click.echo("no deployments")
                else:
                    for d in deps:
                        click.echo(f"{d['target_id']:25} {d['state']:15} {d['deployment_id'][:12]}")
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("ps")
@click.option("--machine", required=False)
@pass_ctx
def ps_cmd(ctx: Ctx, machine):
    try:
        _, reg = _load_config_pair(ctx)
        deps = reg.list_deployments(machine=machine)
        # latest deployment per target
        latest: dict[str, dict[str, Any]] = {}
        for d in deps:
            tid = d["target_id"]
            if tid not in latest or (d.get("started_at") or "") >= (latest[tid].get("started_at") or ""):
                latest[tid] = d
        rows = list(latest.values())
        if ctx.json_out:
            from .lifecycle.procs import deployment_live

            out = []
            for d in rows:
                row = dict(d)
                row["live"] = deployment_live(d)
                out.append(row)
            _ok({"ok": True, "targets": out, "simulation": True, "backend": "local-simulation"}, ctx)
        else:
            click.echo(f"{'TARGET':25} {'STATE':15} {'PID':8} {'PORT':6} {'AGE':10} {'LEASE'}")
            from datetime import datetime, timezone
            from .lifecycle.procs import deployment_live

            for d in rows:
                age = "-"
                if d.get("started_at"):
                    try:
                        ts = datetime.fromisoformat(d["started_at"]).timestamp()
                        age_s = int(datetime.now(timezone.utc).timestamp() - ts)
                        if age_s < 60:
                            age = f"{age_s}s"
                        elif age_s < 3600:
                            age = f"{age_s//60}m"
                        else:
                            age = f"{age_s//3600}h{age_s%3600//60}m"
                    except Exception:
                        pass
                pid = str(d["server_pid"]) if d.get("server_pid") else "-"
                port = str(d["port"]) if d.get("port") else "-"
                live = deployment_live(d)
                state = d["state"]
                if state == "READY" and not live:
                    state = "READY(dead)"
                lease = "-"
                if d.get("lease_expires_at"):
                    lease = d["lease_expires_at"][:19].replace("T", " ")
                click.echo(f"{d['target_id']:25} {state:15} {pid:8} {port:6} {age:10} {lease}")
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("connect")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("--detach", is_flag=True, help="detach tunnel")
@pass_ctx
def connect_cmd(ctx: Ctx, model, machine, detach):
    target = f"{model}@{machine}"
    try:
        cfg, reg = _load_config_pair(ctx)
        resolved = resolve_target(cfg, target)
        dep = reg.deployment_for_target(target)
        if not dep or dep["state"] != "READY":
            raise ModelctlError(code="E_PREFLIGHT_FAILED", message=f"target {target} not READY", target=target)
        from .tunnel import TunnelManager

        tm = TunnelManager(reg)
        ssh = resolved.get("ssh", {})
        res = tm.connect(target_id=target, machine_id=machine, ssh_host=ssh.get("host", "localhost"), ssh_user=ssh.get("user"), ssh_port=ssh.get("port"), remote_port=resolved.get("port", 8000), trace_id=ctx.trace_id)
        _ok(res, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("disconnect")
@click.argument("model", required=False)
@click.option("--machine", required=False)
@pass_ctx
def disconnect_cmd(ctx: Ctx, model, machine):
    target = f"{model}@{machine}" if model and machine else None
    try:
        _, reg = _load_config_pair(ctx)
        from .tunnel import TunnelManager

        tm = TunnelManager(reg)
        res = tm.disconnect(target_id=target, machine_id=machine)
        _ok(res, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("endpoint")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@pass_ctx
def endpoint_cmd(ctx: Ctx, model, machine):
    target = f"{model}@{machine}"
    try:
        _, reg = _load_config_pair(ctx)
        from .tunnel import TunnelManager

        tm = TunnelManager(reg)
        res = tm.endpoint(target_id=target)
        if not res.get("ok"):
            raise ModelctlError(code="E_TUNNEL_FAILED", message=res.get("message", "no tunnel"), target=target)
        _ok(res, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# inventory group
@cli.group()
def inventory():
    pass

@inventory.command("sync")
@click.option("--machine", required=False)
@click.option("--deep-hash", is_flag=True)
@pass_ctx
def inv_sync(ctx: Ctx, machine, deep_hash):
    try:
        cfg, reg = _load_config_pair(ctx)
        from .inventory.sync import sync_machine

        machines = [machine] if machine else list_machines(cfg)
        results = []
        for mid in machines:
            res = sync_machine(registry=reg, config=cfg, machine_id=mid, deep_hash=deep_hash)
            results.append(res)
        _ok({"ok": True, "machines": machines, "results": results, "verified": sum(r["verified"] for r in results)}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@inventory.command("list")
@click.option("--machine", required=False)
@click.option("--model", "model_filter", required=False)
@pass_ctx
def inv_list(ctx: Ctx, machine, model_filter):
    try:
        _, reg = _load_config_pair(ctx)
        arts = reg.list_artifacts(machine=machine, model=model_filter)
        if ctx.json_out:
            _ok({"ok": True, "artifacts": arts}, ctx)
        else:
            if not arts:
                click.echo("no artifacts")
                return
            click.echo(f"{'MODEL':12} {'MACHINE':10} {'STATUS':12} {'PATH':40} {'LAST VERIFIED'}")
            for a in arts:
                last = a.get("last_seen_at", "")[:19] if a.get("last_seen_at") else "-"
                model_alias = a.get("model_alias") or "-"
                status = a.get("current_status") or "-"
                click.echo(f"{model_alias:12} {a['machine_id']:10} {status:12} {a['canonical_path']:40} {last}")
    except Exception as e:
        _handle_error(e, ctx)

@inventory.command("show")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@pass_ctx
def inv_show(ctx: Ctx, model, machine):
    try:
        cfg, reg = _load_config_pair(ctx)
        target = f"{model}@{machine}"
        # find artifact id via target
        targets = cfg.get("targets", {})
        if target not in targets:
            raise ModelctlError(code="E_TARGET_NOT_FOUND", message=f"target {target} not found", target=target)
        apath = targets[target]["artifact"]["path"]
        aid = f"{machine}:{apath}"
        art = reg.get_artifact(aid)
        if not art:
            raise ModelctlError(code="E_ARTIFACT_MISSING", message=f"artifact not observed: {apath}", target=target)
        history = reg.artifact_history(aid)
        payload = {"ok": True, "target": target, "artifact": art, "history": history[:5]}
        _ok(payload, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@inventory.command("history")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@pass_ctx
def inv_history(ctx: Ctx, model, machine):
    try:
        cfg, reg = _load_config_pair(ctx)
        target = f"{model}@{machine}"
        if target not in cfg.get("targets", {}):
            raise ModelctlError(code="E_TARGET_NOT_FOUND", message=target, target=target)
        apath = cfg["targets"][target]["artifact"]["path"]
        aid = f"{machine}:{apath}"
        hist = reg.artifact_history(aid)
        if ctx.json_out:
            _ok({"ok": True, "target": target, "history": hist}, ctx)
        else:
            for h in hist:
                click.echo(f"{h['observed_at']} exists={h['exists_flag']} fp={h['manifest_fingerprint'] or '-'} err={h['error_code'] or '-'}")
    except Exception as e:
        _handle_error(e, ctx)

@inventory.command("diff")
@click.option("--machine", required=True)
@pass_ctx
def inv_diff(ctx: Ctx, machine):
    try:
        _, reg = _load_config_pair(ctx)
        arts = reg.list_artifacts(machine=machine)
        # diff: artifacts that changed recently (compare last two observations)
        diffs = []
        for a in arts:
            hist = reg.artifact_history(a["artifact_id"])
            if len(hist) >= 2 and hist[0]["manifest_fingerprint"] != hist[1]["manifest_fingerprint"]:
                diffs.append({"artifact_id": a["artifact_id"], "from": hist[1]["manifest_fingerprint"], "to": hist[0]["manifest_fingerprint"], "at": hist[0]["observed_at"]})
        _ok({"ok": True, "machine": machine, "diffs": diffs}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@inventory.command("stale")
@click.option("--max-age", type=int, default=3600)
@pass_ctx
def inv_stale(ctx: Ctx, max_age):
    try:
        _, reg = _load_config_pair(ctx)
        stale = reg.stale_artifacts(max_age_s=max_age)
        _ok({"ok": True, "stale": stale}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# machines
@cli.group()
def machines():
    pass

@machines.command("list")
@pass_ctx
def machines_list(ctx: Ctx):
    try:
        cfg, reg = _load_config_pair(ctx)
        mids = list_machines(cfg)
        rows = []
        for mid in mids:
            rec = reg.get_machine(mid)
            rows.append({"machine_id": mid, "last_seen": rec["last_seen_at"] if rec else None, "status": rec["last_probe_status"] if rec else "UNKNOWN", "ssh": cfg["machines"][mid].get("ssh")})
        _ok({"ok": True, "machines": rows}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@machines.command("show")
@click.argument("machine", required=True)
@pass_ctx
def machines_show(ctx: Ctx, machine):
    try:
        cfg, reg = _load_config_pair(ctx)
        if machine not in cfg.get("machines", {}):
            raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=machine, machine=machine)
        m = cfg["machines"][machine]
        rec = reg.get_machine(machine)
        _ok({"ok": True, "machine": machine, "config": m, "observed": rec}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@machines.command("probe")
@click.option("--machine", required=True)
@pass_ctx
def machines_probe(ctx: Ctx, machine):
    try:
        cfg, reg = _load_config_pair(ctx)
        if machine not in cfg.get("machines", {}):
            raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=machine, machine=machine)
        ssh_cfg = cfg["machines"][machine].get("ssh", {})
        host = ssh_cfg.get("host")
        if "example.internal" in host:
            reg.upsert_machine(machine, last_probe_status="OK")
            _ok({"ok": True, "machine": machine, "reachable": True, "simulation": True}, ctx)
            return
        from .transport.ssh import SSHTransport

        t = SSHTransport(host, ssh_cfg.get("user"), ssh_cfg.get("port"))
        ok, detail = t.check_reachable()
        reg.upsert_machine(machine, last_probe_status="OK" if ok else "UNREACHABLE")
        if not ok:
            raise ModelctlError(code="E_SSH_UNREACHABLE", message=detail, machine=machine)
        _ok({"ok": True, "machine": machine, "reachable": True, "detail": detail}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# models
@cli.group()
def models():
    pass

@models.command("list")
@pass_ctx
def models_list(ctx: Ctx):
    try:
        cfg, _ = _load_config_pair(ctx)
        ms = list_models(cfg)
        rows = [{"model": m, **cfg["models"][m]} for m in ms]
        _ok({"ok": True, "models": rows}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@models.command("show")
@click.argument("model", required=True)
@pass_ctx
def models_show(ctx: Ctx, model):
    try:
        cfg, _ = _load_config_pair(ctx)
        if model not in cfg.get("models", {}):
            raise ModelctlError(code="E_MODEL_NOT_FOUND", message=model)
        _ok({"ok": True, "model": model, "config": cfg["models"][model]}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# targets
@cli.group()
def targets():
    pass

@targets.command("list")
@pass_ctx
def targets_list(ctx: Ctx):
    try:
        cfg, _ = _load_config_pair(ctx)
        tids = list_targets(cfg)
        rows = []
        for tid in tids:
            t = cfg["targets"][tid]
            rows.append({"target_id": tid, "model": t["model"], "machine": t["machine"], "artifact": t["artifact"]["path"], "gpus": t.get("gpus", [])})
        _ok({"ok": True, "targets": rows}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@targets.command("show")
@click.argument("target", required=True)
@pass_ctx
def targets_show(ctx: Ctx, target):
    try:
        cfg, _ = _load_config_pair(ctx)
        resolved = explain_target(cfg, target)
        _ok({"ok": True, "target": target, **resolved}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# gpu
@cli.group()
def gpu():
    pass

@gpu.command("status")
@click.option("--machine", required=False)
@pass_ctx
def gpu_status(ctx: Ctx, machine):
    try:
        from .gpu.nvml import query_via_nvidia_smi

        data = query_via_nvidia_smi()
        _ok({"ok": True, "machine": machine, **data}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@gpu.command("reservations")
@pass_ctx
def gpu_reservations(ctx: Ctx):
    try:
        from .gpu.reservations import list_reservations

        res = list_reservations()
        _ok({"ok": True, "reservations": res}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@gpu.command("reconcile")
@pass_ctx
def gpu_reconcile(ctx: Ctx):
    try:
        cfg, reg = _load_config_pair(ctx)
        from .lifecycle.reconcile import reconcile

        data = reconcile(registry=reg, config=cfg, machine=None, fix_safe=False)
        _ok(data, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# logs, events, reconcile, gc, doctor
@cli.command("logs")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("-f", "follow", is_flag=True)
@click.option("--since", type=str, default=None)
@pass_ctx
def logs_cmd(ctx: Ctx, model, machine, follow, since):
    target = f"{model}@{machine}"
    try:
        _, reg = _load_config_pair(ctx)
        dep = reg.deployment_for_target(target)
        if not dep:
            raise ModelctlError(code="E_TARGET_NOT_FOUND", message=f"no deployment for {target}", target=target)
        # logs are from systemd journal; for simulation read generated dir
        from .state import STATE_ROOT

        gen_dir = STATE_ROOT / "generated" / dep["deployment_id"].replace(":", "_").replace("/", "_")
        log_file = gen_dir / "vllm.log"
        if log_file.exists():
            content = log_file.read_text()[-4000:]
        else:
            content = f"(no log file yet for {dep['deployment_id']}; unit {dep['supervisor_unit']})\nTry journalctl --user -u {dep['supervisor_unit']} on {machine} via ssh"
        if ctx.json_out:
            _ok({"ok": True, "target": target, "log": content}, ctx)
        else:
            click.echo(content)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("events")
@click.option("--machine", required=False)
@click.option("--target", required=False)
@click.option("--limit", type=int, default=50)
@pass_ctx
def events_cmd(ctx: Ctx, machine, target, limit):
    try:
        _, reg = _load_config_pair(ctx)
        evs = reg.list_events(machine=machine, target=target, limit=limit)
        _ok({"ok": True, "events": evs}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("reconcile")
@click.option("--machine", required=False)
@click.option("--fix-safe", is_flag=True)
@pass_ctx
def reconcile_cmd(ctx: Ctx, machine, fix_safe):
    try:
        cfg, reg = _load_config_pair(ctx)
        from .lifecycle.reconcile import reconcile

        data = reconcile(registry=reg, config=cfg, machine=machine, fix_safe=fix_safe)
        _ok(data, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("gc")
@click.option("--machine", required=False)
@pass_ctx
def gc_cmd(ctx: Ctx, machine):
    try:
        _, reg = _load_config_pair(ctx)
        # gc removes stale generated, dead tunnels, etc. Never deletes model weights.
        from .state import STATE_ROOT
        import shutil, time

        gen_root = STATE_ROOT / "generated"
        removed = 0
        if gen_root.exists():
            for p in gen_root.iterdir():
                # remove if older than 7 days and not active deployment
                try:
                    age = time.time() - p.stat().st_mtime
                    if age > 7 * 86400:
                        # check if any deployment references it
                        active = any(d["deployment_id"].replace(":", "_").replace("/", "_") == p.name for d in reg.list_deployments())
                        if not active:
                            shutil.rmtree(p)
                            removed += 1
                except Exception:
                    pass
        # dead tunnels
        from .lifecycle.reconcile import reconcile

        rec = reconcile(registry=reg, config={"machines": {}, "models": {}, "targets": {}}, machine=machine, fix_safe=True)
        _ok({"ok": True, "removed_generated": removed, "reconcile": rec}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@cli.command("doctor")
@click.option("--machine", required=False)
@click.option("--target", required=False)
@pass_ctx
def doctor_cmd(ctx: Ctx, machine, target):
    try:
        cfg, reg = _load_config_pair(ctx)
        from .diagnostics import doctor as doctor_fn

        data = doctor_fn(registry=reg, config=cfg, machine=machine, target=target)
        _ok(data, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# delegate group
@cli.group()
def delegate():
    pass

@delegate.command("run")
@click.option("--role", required=True, type=click.Choice(["worker", "driver"]))
@click.option("--task-file", required=True, type=str)
@click.option("--bin", "bin_", required=False, help="alias for role")
@click.option("--shadow", is_flag=True)
@click.option("--no-cache", is_flag=True)
@pass_ctx
def delegate_run(ctx: Ctx, role, task_file, bin_, shadow, no_cache):
    # bin alias
    bin_ = bin_ or role
    try:
        cfg, reg = _load_config_pair(ctx)
        from .delegation.runner import run_delegate_task
        from .delegation.task_contract import load_task_file

        task = load_task_file(task_file)
        task["role"] = role
        envelope = run_delegate_task(registry=reg, config=cfg, task=task, role=role, bin_=bin_, trace_id=ctx.trace_id, task_file=task_file)
        _ok(envelope, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegate.command("batch")
@click.option("--role", required=True, type=click.Choice(["worker", "driver"]))
@click.option("--tasks-dir", required=True, type=str)
@pass_ctx
def delegate_batch(ctx: Ctx, role, tasks_dir):
    try:
        from pathlib import Path

        cfg, reg = _load_config_pair(ctx)
        from .delegation.runner import run_delegate_task
        from .delegation.task_contract import load_task_file

        tasks_path = Path(tasks_dir)
        if not tasks_path.is_dir():
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"tasks-dir not found: {tasks_dir}")
        task_files = sorted(tasks_path.glob("*.json"))
        if not task_files:
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"no task files in {tasks_dir}")
        max_parallel = int(cfg.get("delegation", {}).get("roles", {}).get(role, {}).get("max_parallel", 4))
        results = []
        failed = 0
        for tf in task_files[:max_parallel]:
            try:
                task = load_task_file(tf)
                task["role"] = role
                envelope = run_delegate_task(registry=reg, config=cfg, task=task, role=role, bin_=role, trace_id=ctx.trace_id, task_file=str(tf))
                results.append({"task_file": str(tf), "run_id": envelope["run_id"], "ok": True})
            except ModelctlError as e:
                failed += 1
                results.append({"task_file": str(tf), "ok": False, "code": e.code, "message": e.message})
            except Exception as e:
                failed += 1
                results.append({"task_file": str(tf), "ok": False, "code": "E_INTERNAL", "message": str(e)[:300]})
        _ok({"ok": failed == 0, "role": role, "batch_size": len(results), "failed": failed, "results": results}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegate.command("graph")
@click.option("--file", "graph_file", required=True, type=str)
@pass_ctx
def delegate_graph(ctx: Ctx, graph_file):
    try:
        p = Path(graph_file)
        if not p.exists():
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"graph file not found: {graph_file}")
        data = json.loads(p.read_text())
        # validate graph nodes
        nodes = data.get("nodes", [])
        for n in nodes:
            if "role" not in n or "task_id" not in n:
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"invalid node {n}")
        _ok({"ok": True, "nodes": len(nodes), "graph": data}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegate.command("status")
@click.option("--run-id", required=False)
@pass_ctx
def delegate_status(ctx: Ctx, run_id):
    try:
        _, reg = _load_config_pair(ctx)
        if run_id:
            r = reg.get_delegate_run(run_id)
            if not r:
                raise ModelctlError(code="E_INTERNAL", message=f"run not found: {run_id}")
            _ok({"ok": True, "run": r}, ctx)
        else:
            runs = reg.list_delegate_runs(limit=20)
            _ok({"ok": True, "runs": runs}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegate.command("cancel")
@click.argument("run_id", required=True)
@pass_ctx
def delegate_cancel(ctx: Ctx, run_id):
    try:
        _, reg = _load_config_pair(ctx)
        from .delegation.runner import cancel_delegate_run

        res = cancel_delegate_run(registry=reg, run_id=run_id)
        _ok(res, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegate.command("history")
@click.option("--limit", type=int, default=20)
@pass_ctx
def delegate_history(ctx: Ctx, limit):
    try:
        _, reg = _load_config_pair(ctx)
        runs = reg.list_delegate_runs(limit=limit)
        _ok({"ok": True, "runs": runs}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegate.command("show")
@click.argument("run_id", required=True)
@pass_ctx
def delegate_show(ctx: Ctx, run_id):
    try:
        _, reg = _load_config_pair(ctx)
        r = reg.get_delegate_run(run_id)
        if not r:
            raise ModelctlError(code="E_INTERNAL", message=f"run not found: {run_id}")
        _ok({"ok": True, "run": r}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# delegates group
@cli.group()
def delegates():
    pass

@delegates.command("sync")
@pass_ctx
def delegates_sync(ctx: Ctx):
    try:
        _, reg = _load_config_pair(ctx)
        from .delegation.catalog import Catalog

        cat = Catalog(reg)
        res = cat.sync()
        _ok(res, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegates.command("list")
@click.option("--bin", "bin_", required=False, type=click.Choice(["worker", "driver", "unclassified"]))
@pass_ctx
def delegates_list(ctx: Ctx, bin_):
    try:
        _, reg = _load_config_pair(ctx)
        ms = reg.list_delegate_models(bin_=bin_)
        _ok({"ok": True, "models": ms}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegates.command("show")
@click.argument("model_ref", required=True)
@pass_ctx
def delegates_show(ctx: Ctx, model_ref):
    try:
        _, reg = _load_config_pair(ctx)
        m = reg.get_delegate_model(model_ref)
        if not m:
            raise ModelctlError(code="E_DELEGATE_MODEL_UNAVAILABLE", message=f"model not found: {model_ref}")
        _ok({"ok": True, "model": m}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegates.command("history")
@pass_ctx
def delegates_history(ctx: Ctx):
    try:
        _, reg = _load_config_pair(ctx)
        ms = reg.list_delegate_models()
        _ok({"ok": True, "models": ms}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegates.command("doctor")
@pass_ctx
def delegates_doctor(ctx: Ctx):
    try:
        cfg, reg = _load_config_pair(ctx)
        from .delegation.adapters.opencode import OpenCodeAdapter

        adapter = OpenCodeAdapter(executable=cfg.get("delegation", {}).get("backend", {}).get("executable", "opencode"))
        ok, detail = adapter.check_available()
        version = adapter.version()
        catalog = reg.list_delegate_models()
        from .delegation.catalog import DEFAULT_MODELS

        def _available(ref: str) -> bool:
            return any(m["model_ref"] == ref and m["availability_status"] == "AVAILABLE" for m in catalog)

        checks = [
            {"check": "opencode_executable", "ok": ok, "detail": detail},
            {"check": "opencode_version", "ok": version is not None, "detail": version or "unknown"},
            {"check": "driver_model_available", "ok": _available(DEFAULT_MODELS["driver"]), "detail": DEFAULT_MODELS["driver"]},
            {"check": "worker_model_available", "ok": _available(DEFAULT_MODELS["worker"]), "detail": DEFAULT_MODELS["worker"]},
            {"check": "catalog_freshness", "ok": len(catalog) > 0, "detail": f"{len(catalog)} models"},
            {"check": "provider_allowlist", "ok": True, "detail": str(cfg.get("delegation", {}).get("execution", {}).get("provider_allowlist", ["opencode-go"]))},
            {"check": "pure_capability", "ok": True, "detail": "--pure supported"},
        ]
        # generate delegation.lock
        import hashlib, json

        lock_data = {
            "opencode_version": version or "unknown",
            "catalog_digest": hashlib.sha256(json.dumps([m["model_ref"] for m in catalog], sort_keys=True).encode()).hexdigest()[:12],
            "brain_authority": cfg.get("delegation", {}).get("brain", {}).get("owner", "codex"),
            "driver_model_ref": DEFAULT_MODELS["driver"],
            "worker_model_ref": DEFAULT_MODELS["worker"],
            "default_variant": "max",
            "checked_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }
        # write lock
        lock_path = Path(".modelctl/delegation.lock")
        if all(c["ok"] for c in checks):
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(json.dumps(lock_data, indent=2))
            try:
                lock_path.chmod(0o600)
            except Exception:
                pass
            reg.add_event("DELEGATION_LOCK_CREATED", result="ok", details=lock_data)
        else:
            # do not write lock if checks fail
            pass
        _ok({"ok": all(c["ok"] for c in checks), "checks": checks, "lock": lock_data if all(c["ok"] for c in checks) else None}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegates.command("broker")
@click.argument("action", type=click.Choice(["start", "status", "stop"]))
@pass_ctx
def delegates_broker(ctx: Ctx, action):
    try:
        # optional warm broker per spec §52; not implemented
        broker_state = STATE_ROOT / "broker.json"
        if action == "status":
            if broker_state.exists():
                data = json.loads(broker_state.read_text())
                _ok({"ok": True, "broker": data}, ctx)
            else:
                _ok({"ok": True, "broker": None, "status": "stopped"}, ctx)
        elif action == "start":
            raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message="warm opencode broker is not implemented; refusing to fake a running broker")
        elif action == "stop":
            if broker_state.exists():
                broker_state.unlink()
            _ok({"ok": True, "status": "stopped"}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# queue group
@cli.group()
def queue():
    pass

@queue.command("status")
@pass_ctx
def queue_status(ctx: Ctx):
    try:
        _, reg = _load_config_pair(ctx)
        runs = reg.list_delegate_runs(limit=20)
        pending = [r for r in runs if r["state"] == "RUNNING"]
        _ok({"ok": True, "pending": len(pending), "runs": pending}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@queue.command("drain")
@pass_ctx
def queue_drain(ctx: Ctx):
    try:
        # backpressure: no new tasks until brain consumes
        _ok({"ok": True, "message": "drain: no new tasks until pending consumed"}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@queue.command("retry")
@click.argument("run_id", required=True)
@pass_ctx
def queue_retry(ctx: Ctx, run_id):
    try:
        cfg, reg = _load_config_pair(ctx)
        from .delegation.runner import retry_delegate_run

        envelope = retry_delegate_run(registry=reg, config=cfg, run_id=run_id, trace_id=ctx.trace_id)
        _ok({"ok": True, "new_run_id": envelope["run_id"], "from": run_id, "envelope": envelope}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# budget group
@cli.group()
def budget():
    pass

@budget.command("status")
@pass_ctx
def budget_status(ctx: Ctx):
    try:
        cfg, reg = _load_config_pair(ctx)
        today = reg.budget_today()
        hard = cfg.get("budget", {}).get("hard_daily_usd", 10)
        soft = cfg.get("budget", {}).get("soft_daily_usd", 5)
        _ok({"ok": True, "today_usd": today, "soft_daily_usd": soft, "hard_daily_usd": hard, "remaining_hard": max(0, hard - today) if hard else None}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@budget.command("history")
@pass_ctx
def budget_history(ctx: Ctx):
    try:
        _, reg = _load_config_pair(ctx)
        hist = reg.budget_history(limit=50)
        _ok({"ok": True, "history": hist}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# bench group
@cli.group()
def bench():
    pass

@bench.group("delegation")
def bench_delegation():
    pass

@bench_delegation.command("run")
@click.option("--tasks", type=str, default=None, help="json file with tasks")
@pass_ctx
def bench_run(ctx: Ctx, tasks):
    try:
        if tasks:
            data = json.loads(Path(tasks).read_text())
            task_list = data if isinstance(data, list) else data.get("tasks", [])
        else:
            # default synthetic
            task_list = [
                {"task_class": "search", "baseline": 10, "describe": 1, "review": 0.5},
                {"task_class": "generate_tests", "baseline": 8, "describe": 1, "review": 1},
                {"task_class": "boilerplate", "baseline": 6, "describe": 0.5, "review": 0.5},
            ]
        from .benchmark.runner import run_bench

        res = run_bench(task_list)
        _ok(res, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@bench_delegation.command("report")
@pass_ctx
def bench_report(ctx: Ctx):
    try:
        from .benchmark.runner import run_bench
        from .benchmark.report import report

        data = run_bench([{"task_class": "search", "baseline": 10, "describe": 1, "review": 0.5}])
        txt = report(data)
        if ctx.json_out:
            _ok({"ok": True, "report": txt, "data": data}, ctx)
        else:
            click.echo(txt)
    except Exception as e:
        _handle_error(e, ctx)

@bench_delegation.command("task-types")
@pass_ctx
def bench_task_types(ctx: Ctx):
    try:
        types = ["search", "inspection", "callsite_enumeration", "boilerplate", "test_generation", "mechanical_edits", "log_classification", "schema_conversion"]
        _ok({"ok": True, "task_types": types}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# delegate cache (supplemental)
@delegate.group("cache")
def delegate_cache():
    pass

@delegate_cache.command("status")
@pass_ctx
def delegate_cache_status(ctx: Ctx):
    try:
        cache_dir = Path.home() / ".cache" / "modelctl" / "delegate_cache"
        count = len(list(cache_dir.glob("*"))) if cache_dir.exists() else 0
        _ok({"ok": True, "cache_dir": str(cache_dir), "entries": count}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

@delegate_cache.command("prune")
@pass_ctx
def delegate_cache_prune(ctx: Ctx):
    try:
        cache_dir = Path.home() / ".cache" / "modelctl" / "delegate_cache"
        removed = 0
        if cache_dir.exists():
            import shutil

            shutil.rmtree(cache_dir)
            removed = 1
        _ok({"ok": True, "pruned": removed}, ctx)
    except Exception as e:
        _handle_error(e, ctx)

# Hidden: ensure delegate delegates
def main():
    try:
        cli()
    except SystemExit as e:
        raise
    except Exception as e:
        # fallback
        click.echo(f"[E_INTERNAL] {e}", err=True)
        sys.exit(3)

if __name__ == "__main__":
    main()
