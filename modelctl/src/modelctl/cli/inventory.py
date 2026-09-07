"""Discovery groups: inventory, machines, models, targets."""

from __future__ import annotations

import click

from ..config.resolver import explain_target, list_machines, list_models, list_targets
from ..errors import ModelctlError
from .base import Ctx, emit, handle_error, load_pair, pass_ctx


@click.group()
def inventory():
    pass


@inventory.command("sync")
@click.option("--machine", required=False)
@click.option("--deep-hash", is_flag=True)
@pass_ctx
def inv_sync(ctx: Ctx, machine, deep_hash):
    try:
        from ..inventory.sync import sync_machine

        cfg, reg = load_pair(ctx)
        machines = [machine] if machine else list_machines(cfg)
        results = [sync_machine(registry=reg, config=cfg, machine_id=mid, deep_hash=deep_hash) for mid in machines]
        emit({"ok": True, "machines": machines, "results": results, "verified": sum(r["verified"] for r in results)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@inventory.command("list")
@click.option("--machine", required=False)
@click.option("--model", "model_filter", required=False)
@pass_ctx
def inv_list(ctx: Ctx, machine, model_filter):
    try:
        _, reg = load_pair(ctx)
        arts = reg.list_artifacts(machine=machine, model=model_filter)
        rows = [{
            "MODEL": a.get("model_alias") or "-",
            "MACHINE": a["machine_id"],
            "STATUS": a.get("current_status") or "-",
            "PATH": a["canonical_path"],
            "LAST VERIFIED": (a.get("last_seen_at") or "-")[:19],
        } for a in arts]
        columns = ["MODEL", "MACHINE", "STATUS", "PATH", "LAST VERIFIED"]
        emit({"ok": True, "artifacts": arts, "_render": {"kind": "table", "columns": columns, "rows": rows}}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@inventory.command("show")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@pass_ctx
def inv_show(ctx: Ctx, model, machine):
    try:
        cfg, reg = load_pair(ctx)
        target = f"{model}@{machine}"
        targets = cfg.get("targets", {})
        if target not in targets:
            raise ModelctlError(code="E_TARGET_NOT_FOUND", message=f"target {target} not found", target=target)
        apath = targets[target]["artifact"]["path"]
        aid = f"{machine}:{apath}"
        art = reg.get_artifact(aid)
        if not art:
            raise ModelctlError(code="E_ARTIFACT_MISSING", message=f"artifact not observed: {apath}", target=target)
        emit({"ok": True, "target": target, "artifact": art, "history": reg.artifact_history(aid)[:5]}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@inventory.command("history")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@pass_ctx
def inv_history(ctx: Ctx, model, machine):
    try:
        cfg, reg = load_pair(ctx)
        target = f"{model}@{machine}"
        if target not in cfg.get("targets", {}):
            raise ModelctlError(code="E_TARGET_NOT_FOUND", message=target, target=target)
        aid = f"{machine}:{cfg['targets'][target]['artifact']['path']}"
        hist = reg.artifact_history(aid)
        text = "\n".join(f"{h['observed_at']} exists={h['exists_flag']} fp={h['manifest_fingerprint'] or '-'} err={h['error_code'] or '-'}" for h in hist)
        emit({"ok": True, "target": target, "history": hist, "_render": {"kind": "text", "text": text}}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@inventory.command("diff")
@click.option("--machine", required=True)
@pass_ctx
def inv_diff(ctx: Ctx, machine):
    try:
        _, reg = load_pair(ctx)
        diffs = []
        for a in reg.list_artifacts(machine=machine):
            hist = reg.artifact_history(a["artifact_id"])
            if len(hist) >= 2 and hist[0]["manifest_fingerprint"] != hist[1]["manifest_fingerprint"]:
                diffs.append({"artifact_id": a["artifact_id"], "from": hist[1]["manifest_fingerprint"], "to": hist[0]["manifest_fingerprint"], "at": hist[0]["observed_at"]})
        emit({"ok": True, "machine": machine, "diffs": diffs}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@inventory.command("stale")
@click.option("--max-age", type=int, default=3600)
@pass_ctx
def inv_stale(ctx: Ctx, max_age):
    try:
        _, reg = load_pair(ctx)
        emit({"ok": True, "stale": reg.stale_artifacts(max_age_s=max_age)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def machines():
    pass


@machines.command("list")
@pass_ctx
def machines_list(ctx: Ctx):
    try:
        cfg, reg = load_pair(ctx)
        rows = []
        for mid in list_machines(cfg):
            rec = reg.get_machine(mid)
            rows.append({"machine_id": mid, "last_seen": rec["last_seen_at"] if rec else None, "status": rec["last_probe_status"] if rec else "UNKNOWN", "ssh": cfg["machines"][mid].get("ssh")})
        emit({"ok": True, "machines": rows}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@machines.command("show")
@click.argument("machine", required=True)
@pass_ctx
def machines_show(ctx: Ctx, machine):
    try:
        cfg, reg = load_pair(ctx)
        if machine not in cfg.get("machines", {}):
            raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=machine, machine=machine)
        emit({"ok": True, "machine": machine, "config": cfg["machines"][machine], "observed": reg.get_machine(machine)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@machines.command("probe")
@click.option("--machine", required=True)
@pass_ctx
def machines_probe(ctx: Ctx, machine):
    try:
        from ..transport.ssh import SSHTransport

        cfg, reg = load_pair(ctx)
        if machine not in cfg.get("machines", {}):
            raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=machine, machine=machine)
        ssh_cfg = cfg["machines"][machine].get("ssh", {})
        host = ssh_cfg.get("host")
        if "example.internal" in host:
            reg.upsert_machine(machine, last_probe_status="OK")
            emit({"ok": True, "machine": machine, "reachable": True, "simulation": True}, ctx)
            return
        ok, detail = SSHTransport(host, ssh_cfg.get("user"), ssh_cfg.get("port"), ssh_cfg.get("password_file")).check_reachable()
        reg.upsert_machine(machine, last_probe_status="OK" if ok else "UNREACHABLE")
        if not ok:
            raise ModelctlError(code="E_SSH_UNREACHABLE", message=detail, machine=machine)
        emit({"ok": True, "machine": machine, "reachable": True, "detail": detail}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def models():
    pass


@models.command("list")
@pass_ctx
def models_list(ctx: Ctx):
    try:
        cfg, _ = load_pair(ctx)
        emit({"ok": True, "models": [{"model": m, **cfg["models"][m]} for m in list_models(cfg)]}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@models.command("show")
@click.argument("model", required=True)
@pass_ctx
def models_show(ctx: Ctx, model):
    try:
        cfg, _ = load_pair(ctx)
        if model not in cfg.get("models", {}):
            raise ModelctlError(code="E_MODEL_NOT_FOUND", message=model)
        emit({"ok": True, "model": model, "config": cfg["models"][model]}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def targets():
    pass


@targets.command("list")
@pass_ctx
def targets_list(ctx: Ctx):
    try:
        cfg, _ = load_pair(ctx)
        rows = []
        for tid in list_targets(cfg):
            t = cfg["targets"][tid]
            rows.append({"target_id": tid, "model": t["model"], "machine": t["machine"], "artifact": t["artifact"]["path"], "gpus": t.get("gpus", [])})
        emit({"ok": True, "targets": rows}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@targets.command("show")
@click.argument("target", required=True)
@pass_ctx
def targets_show(ctx: Ctx, target):
    try:
        cfg, _ = load_pair(ctx)
        resolved = explain_target(cfg, target)
        emit({"ok": True, "target": target, **resolved}, ctx)
    except Exception as e:
        handle_error(e, ctx)
