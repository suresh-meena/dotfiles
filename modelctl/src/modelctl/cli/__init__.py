"""modelctl CLI.

Error handling lives in ONE place: ModelctlCLI.invoke catches anything a
command raises and routes it through handle_error. Commands only build
payloads and call emit().
"""

from __future__ import annotations

import sys

import click

from .base import Ctx, handle_error
from ..state import ensure_state_dirs


class ModelctlCLI(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except SystemExit:
            raise
        except Exception as e:
            handle_error(e, ctx.obj)


@click.group(cls=ModelctlCLI, context_settings={"help_option_names": ["-h", "--help"]})
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


from .config_cmds import config, version_cmd  # noqa: E402
from .delegation import bench, budget, delegate, delegates, queue  # noqa: E402
from .gpu import gpu  # noqa: E402
from .inventory import inventory, machines, models, targets  # noqa: E402
from .lifecycle import (  # noqa: E402
    doctor_cmd,
    events_cmd,
    gc_cmd,
    logs_cmd,
    ps_cmd,
    reconcile_cmd,
    restart_cmd,
    start_cmd,
    status_cmd,
    stop_cmd,
)
from .tunnel import connect_cmd, disconnect_cmd, endpoint_cmd  # noqa: E402

for _cmd in (
    version_cmd,
    config,
    start_cmd,
    stop_cmd,
    restart_cmd,
    status_cmd,
    ps_cmd,
    connect_cmd,
    disconnect_cmd,
    endpoint_cmd,
    inventory,
    machines,
    models,
    targets,
    gpu,
    logs_cmd,
    events_cmd,
    reconcile_cmd,
    gc_cmd,
    doctor_cmd,
    delegate,
    delegates,
    queue,
    budget,
    bench,
):
    cli.add_command(_cmd)


def main():
    try:
        cli()
    except SystemExit:
        raise
    except Exception as e:
        click.echo(f"[E_INTERNAL] {e}", err=True)
        sys.exit(3)


if __name__ == "__main__":
    main()
