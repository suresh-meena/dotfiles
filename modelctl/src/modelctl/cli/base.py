"""Shared CLI plumbing: context object, error handling, unified output rendering."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

import click

from ..config.loader import load_config
from ..errors import ModelctlError
from ..inventory.registry import Registry
from ..privacy.redaction import redact_dict


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


def load_pair(ctx: Ctx) -> tuple[dict[str, Any], Registry]:
    cfg, _sources = load_config(ctx.config_path)
    return cfg, Registry()


def handle_error(e: Exception, ctx: Ctx | None, *, json_out: bool | None = None) -> None:
    use_json = json_out if json_out is not None else (ctx.json_out if ctx else False)
    debug = ctx.debug if ctx else False
    if isinstance(e, ModelctlError):
        d = e.to_dict()
        if ctx and ctx.trace_id and "trace_id" not in d:
            d["trace_id"] = ctx.trace_id
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


def _render_table(columns: list[str], rows: list[dict[str, Any]]) -> None:
    widths = {c: len(c) for c in columns}
    cells: list[list[str]] = []
    for row in rows:
        line = []
        for c in columns:
            v = str(row.get(c, ""))
            widths[c] = max(widths[c], len(v))
            line.append(v)
        cells.append(line)
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    click.echo(header.rstrip())
    for line in cells:
        click.echo("  ".join(v.ljust(widths[i]) for i, v in enumerate(line)).rstrip())


def emit(payload: dict[str, Any], ctx: Ctx) -> None:
    """Single output path: JSON when --json, otherwise optional _render hint,
    then message-or-JSON fallback."""
    payload = dict(payload)
    render = payload.pop("_render", None)
    payload = redact_dict(payload)
    if ctx.json_out:
        click.echo(json.dumps(payload, indent=2))
        return
    if isinstance(render, dict):
        kind = render.get("kind")
        if kind == "text":
            click.echo(str(render.get("text", "")))
            return
        if kind == "table":
            _render_table(list(render["columns"]), list(render["rows"]))
            return
    if "message" in payload and len(payload) <= 2:
        click.echo(str(payload["message"]))
        return
    click.echo(json.dumps(payload, indent=2))
