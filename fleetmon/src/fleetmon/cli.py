"""Command-line entry point for helper and hub operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from .config import ConfigError, HubConfig, load_config
from .snapshot import snapshot_json
from .state import MAX_STATE_BYTES, HubLock, check_json_depth, reject_json_constant

if TYPE_CHECKING:
    from .service import HubRuntime

MAX_RUNTIME_STATE_DEPTH = 8
MAX_TARGET_NAME_BYTES = 256


def _check_runtime_state(value: object) -> None:
    check_json_depth(
        value,
        limit=MAX_RUNTIME_STATE_DEPTH,
        depth_message="runtime state depth exceeds limit",
        nonfinite_message="runtime state contains a non-finite number",
    )


def _valid_target_argument(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_TARGET_NAME_BYTES
        and value == value.strip()
        and not value.startswith("-")
        and not any(
            char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value
        )
    )


def _config() -> HubConfig:
    path = (
        Path(os.environ["FLEETMON_CONFIG"])
        if os.environ.get("FLEETMON_CONFIG")
        else None
    )
    return load_config(path)


def _print_json(value: object) -> None:
    print(
        json.dumps(
            value,
            default=str,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    )


def _snapshot(_: argparse.Namespace) -> int:
    # Stdout is exactly one bounded JSON document; diagnostics use stderr.
    try:
        sys.stdout.buffer.write(snapshot_json())
        sys.stdout.buffer.write(b"\n")
        return 0
    except Exception as exc:
        print(f"snapshot failed: {type(exc).__name__}", file=sys.stderr)
        return 1


def _fleetctl_doctor(config: HubConfig) -> str | None:
    """Run the mandatory local fleetctl health check without retaining output."""

    if not config.fleetctl_path.exists() or not os.access(
        config.fleetctl_path, os.X_OK
    ):
        return "fleetctl_missing"
    try:
        completed = subprocess.run(
            [str(config.fleetctl_path), "doctor"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "fleetctl_unavailable"
    except OSError:
        return "fleetctl_unavailable"
    return None if completed.returncode == 0 else "fleetctl_unhealthy"


def _doctor(args: argparse.Namespace) -> int:
    if args.target and not _valid_target_argument(args.target):
        _print_json({"ok": False, "error": "invalid_target"})
        return 2
    try:
        config = _config()
    except (ConfigError, OSError):
        _print_json({"ok": False, "error": "invalid_config"})
        return 2

    result: dict[str, object] = {
        "ok": True,
        "fleetctl": str(config.fleetctl_path),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    doctor_error = _fleetctl_doctor(config)
    if doctor_error:
        result.update(ok=False, error=doctor_error)

    if args.target:
        result["target"] = args.target
        try:
            from .discovery import admitted_targets, discover

            inventory = discover(str(config.fleetctl_path))
            matches = [
                target for target in inventory.targets if target.name == args.target
            ]
            if not matches:
                result.update(ok=False, error="unknown_target")
            else:
                target = matches[0]
                protocol = inventory.protocols.get(target.protocol)
                if protocol is None:
                    result.update(ok=False, error="invalid_protocol")
                    _print_json(result)
                    return 1
                result["role"] = target.role
                result["protocol_kind"] = protocol.kind
                result["admitted"] = any(
                    item.name == target.name for item in admitted_targets(inventory)
                )
                if not result["admitted"]:
                    result.update(ok=False, error="target_not_admitted")
        except Exception:
            result.update(ok=False, error="inventory_unavailable")

    _print_json(result)
    return 0 if result["ok"] else 1


def _status(_: argparse.Namespace) -> int:
    try:
        config = _config()
        state = _read_runtime_state(config.state_dir / "runtime.json")
        if not config.database_path.exists():
            _print_json(
                {
                    "config": "ok",
                    "hub": "not_initialized",
                    "hosts": [],
                    "state": state,
                }
            )
            return 0
        database = sqlite3.connect(
            f"file:{quote(str(config.database_path), safe='/')}?mode=ro",
            uri=True,
            timeout=2,
        )
        database.row_factory = sqlite3.Row
        try:
            hosts = [
                dict(row)
                for row in database.execute(
                    "SELECT * FROM hosts ORDER BY target LIMIT 1000"
                )
            ]
        finally:
            database.close()
        _print_json(
            {
                "config": "ok",
                "hub": "initialized",
                "hosts": hosts,
                "state": state,
            }
        )
        return 0
    except (ConfigError, OSError, sqlite3.Error):
        _print_json({"config": "error", "error": "invalid_config_or_database"})
        return 1
    except Exception:
        _print_json({"config": "error", "error": "status_unavailable"})
        return 1


def _read_runtime_state(path: Path) -> dict[str, object]:
    """Read status state without creating directories or writing anything.

    Rejects NaN/Infinity, excessive nesting, and malformed documents the same
    way ``OperationalState`` does; the return value is always a plain dict.
    """

    try:
        if not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
            return {}
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_json_constant,
        )
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    try:
        _check_runtime_state(value)
    except ValueError:
        return {}
    return value


def _maintain_backup(args: argparse.Namespace) -> int:
    from .database import Database

    try:
        state_dir = Path(args.state_dir).resolve(strict=True)
    except OSError:
        _print_json({"ok": False, "error": "state_dir_missing"})
        return 1
    backups = state_dir / "backups"
    try:
        backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = backups / f"fleet-{timestamp}.db"
        database = Database(state_dir / "fleet.db")
        try:
            database.backup(destination)
        finally:
            database.close()
        os.chmod(destination, 0o600)
        _print_json({"ok": True, "path": str(destination)})
        return 0
    except Exception:
        _print_json({"ok": False, "error": "backup_failed"})
        return 1


def _maintain(args: argparse.Namespace) -> int:
    if args.action == "backup":
        return _maintain_backup(args)
    runtime = None
    lock = None
    try:
        config = _config()
        lock = HubLock(config.state_dir / "hub.lock")
        lock.acquire()
        from .service import HubRuntime

        runtime = HubRuntime(config)
        deleted = runtime.retain()
        _print_json({"ok": True, "deleted": deleted})
        return 0
    except RuntimeError:
        _print_json({"ok": False, "error": "hub_running"})
        return 1
    except Exception:
        _print_json({"ok": False, "error": "maintenance_failed"})
        return 1
    finally:
        if runtime is not None:
            runtime.close()
        if lock is not None:
            lock.release()


async def _serve_hub(runtime: HubRuntime) -> int:
    import uvicorn

    # Inventory must be valid before readiness or the listener comes up.
    doctor_error = await asyncio.to_thread(_fleetctl_doctor, runtime.config)
    if doctor_error:
        raise RuntimeError(f"fleetctl doctor failed: {doctor_error}")
    await runtime.refresh_inventory_async()
    poll_task: asyncio.Task[None] | None = None
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                runtime.create_app(),
                host=runtime.config.bind_host,
                port=runtime.config.bind_port,
                workers=1,
                limit_concurrency=32,
                backlog=64,
                access_log=False,
                log_level="info",
            )
        )
        poll_task = asyncio.create_task(runtime.run_forever())
        await server.serve()
    finally:
        if poll_task is not None:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
    return 0


def _hub(args: argparse.Namespace) -> int:
    runtime = None
    lock = None
    try:
        config = _config()
        lock = HubLock(config.state_dir / "hub.lock")
        lock.acquire()
        from .service import HubRuntime

        runtime = HubRuntime(config)
        if args.once:
            doctor_error = _fleetctl_doctor(config)
            if doctor_error:
                _print_json({"ok": False, "error": doctor_error})
                return 1
            results = asyncio.run(runtime.run_once())
            _print_json({"ok": True, "results": results})
            return 0
        return asyncio.run(_serve_hub(runtime))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"hub startup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            runtime.close()
        if lock is not None:
            lock.release()


def _smoke(args: argparse.Namespace) -> int:
    if not _valid_target_argument(args.target):
        _print_json({"ok": False, "error": "invalid_target"})
        return 2
    try:
        config = _config()
    except (ConfigError, OSError):
        _print_json({"ok": False, "error": "invalid_config"})
        return 2
    doctor_error = _fleetctl_doctor(config)
    if doctor_error:
        _print_json({"ok": False, "error": doctor_error})
        return 1
    try:
        from .smoke import run_smoke

        result = run_smoke(config, args.target)
    except Exception:
        _print_json({"ok": False, "error": "smoke_failed"})
        return 1
    _print_json(result)
    return 0 if result.get("ok") else 1


def _installer_unavailable(_: argparse.Namespace) -> int:
    """Fail closed when installer scripts are not part of an installed wheel."""

    _print_json(
        {
            "ok": False,
            "error": "installer_unavailable",
            "message": "run the separately distributed installer after reviewing it",
        }
    )
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleetmon")
    parser.add_argument(
        "--verbose", action="store_true", help="enable diagnostic logging"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("snapshot").set_defaults(func=_snapshot)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--target")
    doctor.set_defaults(func=_doctor)

    subparsers.add_parser("status").set_defaults(func=_status)
    maintain = subparsers.add_parser("maintain")
    maintain.add_argument(
        "action",
        nargs="?",
        choices=["retain", "backup"],
        default="retain",
        help="maintenance action (default: retain)",
    )
    maintain.add_argument("--state-dir", help="state directory override for backup")
    maintain.set_defaults(func=_maintain)

    hub = subparsers.add_parser("hub")
    hub.add_argument(
        "--once",
        action="store_true",
        help="run one collection pass without starting the web server",
    )
    hub.set_defaults(func=_hub)

    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("target")
    smoke.set_defaults(func=_smoke)

    install_helper = subparsers.add_parser("install-helper")
    install_helper.add_argument("--dry-run", action="store_true")
    install_helper.add_argument("target")
    install_helper.set_defaults(func=_installer_unavailable)

    install_hub = subparsers.add_parser("install-hub")
    install_hub.add_argument("--dry-run", action="store_true")
    install_hub.add_argument("--replace", action="store_true")
    install_hub.add_argument("--python")
    install_hub.add_argument("--source")
    install_hub.set_defaults(func=_installer_unavailable)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args))
