"""SSH tunnel commands: connect/disconnect/endpoint."""

from __future__ import annotations

import click

from ..config.resolver import resolve_target
from ..errors import ModelctlError
from ..tunnel import TunnelManager
from .base import Ctx, emit, handle_error, load_pair, pass_ctx


@click.command("connect")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@click.option("--detach", is_flag=True, help="detach tunnel")
@pass_ctx
def connect_cmd(ctx: Ctx, model, machine, detach):
    try:
        cfg, reg = load_pair(ctx)
        target = f"{model}@{machine}"
        resolved = resolve_target(cfg, target)
        dep = reg.deployment_for_target(target)
        if not dep or dep["state"] != "READY":
            raise ModelctlError(code="E_PREFLIGHT_FAILED", message=f"target {target} not READY", target=target)
        ssh = resolved.get("ssh", {})
        res = TunnelManager(reg).connect(target_id=target, machine_id=machine, ssh_host=ssh.get("host", "localhost"), ssh_user=ssh.get("user"), ssh_port=ssh.get("port"), remote_port=resolved.get("port", 8000), local_port=resolved.get("tunnel", {}).get("local_port"), ssh_password_file=ssh.get("password_file"), trace_id=ctx.trace_id)
        emit(res, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("disconnect")
@click.argument("model", required=False)
@click.option("--machine", required=False)
@pass_ctx
def disconnect_cmd(ctx: Ctx, model, machine):
    try:
        _, reg = load_pair(ctx)
        target = f"{model}@{machine}" if model and machine else None
        emit(TunnelManager(reg).disconnect(target_id=target, machine_id=machine), ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.command("endpoint")
@click.argument("model", required=True)
@click.option("--machine", required=True)
@pass_ctx
def endpoint_cmd(ctx: Ctx, model, machine):
    try:
        _, reg = load_pair(ctx)
        target = f"{model}@{machine}"
        res = TunnelManager(reg).endpoint(target_id=target)
        if not res.get("ok"):
            raise ModelctlError(code="E_TUNNEL_FAILED", message=res.get("message", "no tunnel"), target=target)
        emit(res, ctx)
    except Exception as e:
        handle_error(e, ctx)
