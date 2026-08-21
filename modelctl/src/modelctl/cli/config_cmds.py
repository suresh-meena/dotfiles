"""Version and config commands."""

from __future__ import annotations

import click

from ..config.loader import load_config
from ..config.resolver import explain_target
from ..diagnostics import version_info
from .base import Ctx, emit, handle_error, load_pair, pass_ctx


@click.command("version")
@click.option("--remote", "remote_machine", type=str, default=None, help="remote machine alias")
@pass_ctx
def version_cmd(ctx: Ctx, remote_machine):
    try:
        emit({"ok": True, "version": version_info(machine=remote_machine)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def config():
    pass


@config.command("validate")
@click.pass_context
def config_validate(click_ctx):
    c: Ctx = click_ctx.obj
    try:
        _cfg, sources = load_config(c.config_path)
        emit({"ok": True, "message": "configuration valid", "sources": [str(s) for s in sources]}, c)
    except Exception as e:
        handle_error(e, c)


@config.command("resolve")
@click.option("--target", type=str, required=True, help="model@machine")
@pass_ctx
def config_resolve(ctx: Ctx, target):
    try:
        cfg, _ = load_pair(ctx)
        resolved = explain_target(cfg, target)
        emit({"ok": True, "target": target, **resolved}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@config.command("explain")
@click.option("--target", type=str, required=True)
@pass_ctx
def config_explain(ctx: Ctx, target):
    try:
        import yaml

        cfg, _ = load_pair(ctx)
        resolved = explain_target(cfg, target)
        text = yaml.safe_dump(resolved["resolved"], sort_keys=True) + f"digest: {resolved['config_digest']}\n"
        emit({"ok": True, "target": target, **resolved, "_render": {"kind": "text", "text": text}}, ctx)
    except Exception as e:
        handle_error(e, ctx)
