"""GPU group: status/reservations/reconcile."""

from __future__ import annotations

import click

from .base import Ctx, emit, handle_error, load_pair, pass_ctx


@click.group()
def gpu():
    pass


@gpu.command("status")
@click.option("--machine", required=False)
@pass_ctx
def gpu_status(ctx: Ctx, machine):
    try:
        from ..gpu.nvml import query_via_nvidia_smi

        emit({"ok": True, "machine": machine, **query_via_nvidia_smi()}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@gpu.command("reservations")
@pass_ctx
def gpu_reservations(ctx: Ctx):
    try:
        from ..gpu.reservations import list_reservations

        emit({"ok": True, "reservations": list_reservations()}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@gpu.command("reconcile")
@pass_ctx
def gpu_reconcile(ctx: Ctx):
    try:
        from ..lifecycle.reconcile import reconcile

        cfg, reg = load_pair(ctx)
        emit(reconcile(registry=reg, config=cfg, machine=None, fix_safe=False), ctx)
    except Exception as e:
        handle_error(e, ctx)
