"""fleetqd: the numpi daemon and its local administration commands.

    fleetqd init   --config C [--volume-id ID]  create state dir + volume sentinel
    fleetqd serve  --config C                   run API + controller (one process)
    fleetqd doctor --config C                   local checks, no remote contact
    fleetqd token  create|revoke|list ...        tokens are issued only on numpi
    fleetqd backup --config C DEST              online SQLite backup
    fleetqd node install NAME --i-authorize-target-NAME   shim + enroll + probe, one node
    fleetqd node probe NAME                     read-only capability probe -> node_gpus
    fleetqd node show NAME                      last probe verdict (no remote contact)
    fleetqd slurm-cache-gc TARGET [--purge sha256:DIGEST]  inspect/purge one Slurm shared-root cache

One uvicorn worker, one scheduler, one DB owner thread and one controller lock
are the only supported configuration (§1.1).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__, auth, bundles
from . import gc as gc_module
from .api.app import Runtime, create_app
from .config import DaemonConfig, load_config, sync_nodes
from .clock import ClockHealth, ClockProvider
from .db.store import SENTINEL_NAME, Store, check_sqlite_gate, check_state_volume, validate_state_dir_path
from .engine import fence
from .engine.controller import Controller, ControllerConfig
from .engine.fence import ControllerLock
from .errors import FqError

log = logging.getLogger("fleetq")


def validate_fleetctl_inventory(cfg: DaemonConfig) -> None:
    """Fail closed unless enabled routes agree with fleetctl's local topology."""
    if cfg.dev_mode:
        return
    enabled = [node for node in cfg.nodes if node.enabled]
    if not enabled:
        return
    argv = [str(cfg.fleetctl)]
    if cfg.fleet_config_home is not None:
        argv += ["--config-home", str(cfg.fleet_config_home)]
    argv += ["list", "--format", "topology", "--json"]
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=8, check=False)
        if result.returncode != 0:
            raise ValueError(f"fleetctl exited {result.returncode}")
        payload = json.loads(result.stdout)
        targets = payload.get("targets") if isinstance(payload, dict) else payload
        if not isinstance(targets, list):
            raise ValueError("expected a target list")
        inventory = {}
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("malformed target row")
            name, role, active = target.get("name"), target.get("role"), target.get("enabled")
            if not isinstance(name, str) or not name or not isinstance(role, str) or not isinstance(active, bool):
                raise ValueError("target rows require name, role and boolean enabled")
            if name in inventory:
                raise ValueError(f"duplicate target {name!r}")
            inventory[name] = (role, active)
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"fleetqd: cannot verify local fleetctl topology: {exc}") from exc
    for node in enabled:
        target = node.fleetctl_target or node.id
        record = inventory.get(target)
        expected = "workstation" if node.backend == "bare" else "login"
        if record is None or record[1] is not True or record[0] != expected:
            raise SystemExit(f"fleetqd: enabled {node.backend} node {node.id!r} requires enabled fleetctl target {target!r} with role {expected!r}")


def build_executors(cfg: DaemonConfig, store: Store | None = None) -> dict[str, Any]:
    from .transport.fleetctl import Fleetctl
    from .executors.bare import BareExecutor
    from .executors.slurm import SlurmExecutor

    transport = Fleetctl(cfg.fleetctl, config_home=cfg.fleet_config_home, concurrency=cfg.fleetctl_concurrency,
                         pycache_dir=cfg.state_dir / "pycache")

    async def permit(cluster: str, op_class: str, rpc: int, cost: dict[str, int] | None = None):
        """fleetqd's own calls draw from the same central buckets as remote fleetctl (§3.5)."""
        if store is None:
            return False, None, 300.0
        from . import budget as _budget
        permit_cost = cost or {"rpc": max(1, rpc), "sessions": 1}
        grant = await store.run(lambda c: _budget.grant_permit(
            c, cluster=cluster, op_class=op_class, cost=permit_cost, caller="fleetqd"))
        return grant["granted"], grant.get("permit_id"), grant.get("retry_after")

    return {"bare": BareExecutor(transport, cfg), "slurm": SlurmExecutor(transport, cfg, permit=permit)}


def open_store(cfg: DaemonConfig) -> Store:
    try:
        state_dir = validate_state_dir_path(cfg.state_dir)
    except RuntimeError as exc:
        raise SystemExit(f"fleetqd: {exc}") from exc
    ok, message = check_sqlite_gate(cfg.sqlite_backport_attestation)
    if not ok:
        if not cfg.dev_mode:
            raise SystemExit(f"fleetqd: {message}")
        log.warning("dev_mode: %s", message)
    if not cfg.dev_mode:
        check_state_volume(state_dir, cfg.volume_id, cfg.state_fs_uuid, require_identity=True)
    elif not (state_dir / SENTINEL_NAME).exists():
        raise SystemExit(f"fleetqd: {cfg.state_dir} has no {SENTINEL_NAME}; run `fleetqd init` first")
    cfg.bundle_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    store.open()
    store.run_sync(lambda c: sync_nodes(c, cfg))
    return store


def build_runtime(cfg: DaemonConfig, *, executors: dict[str, Any] | None = None,
                  restored: bool = False) -> tuple[Store, Controller, Runtime]:
    validate_fleetctl_inventory(cfg)
    if cfg.dev_mode:
        clock_health = None
        clock_ok = lambda: True
    else:
        try:
            provider = ClockProvider(cfg.clock_provider)
        except (TypeError, ValueError):
            choices = " or ".join(f"'{provider.value}'" for provider in ClockProvider)
            raise SystemExit(f"fleetqd: production requires [daemon] clock_provider = {choices}") from None
        clock_health = ClockHealth(provider)
        clock_ok = clock_health.read
    store = open_store(cfg)
    ident = store.run_sync(lambda c: fence.start_controller(c, restored_from_backup=restored))
    ccfg = ControllerConfig(**{k: v for k, v in cfg.controller.items() if k in ControllerConfig.__dataclass_fields__})
    from .notify import Outbox
    outbox = Outbox(cfg.notify_url)
    from .engine.idle import IdleHistory
    shared = IdleHistory(policies={n.id: n.gpu_profile for n in cfg.nodes if n.backend == "bare"})
    from .logs import LogCache
    log_cache = LogCache(cfg.state_dir / "logs")
    controller = Controller(store, executors if executors is not None else build_executors(cfg, store), ident,
                            config=ccfg, bundle_dir=cfg.bundle_dir, notifier=outbox.enqueue,
                            shared_capacity=shared, log_cache=log_cache,
                            artifact_dir=cfg.state_dir / "artifacts", clock_ok=clock_ok)
    limits = bundles.BundleLimits(**{k: v for k, v in (cfg.limits.get("bundle") or {}).items()})
    runtime = Runtime(store=store, controller=controller, bundle_dir=cfg.bundle_dir, bundle_limits=limits,
                      log_cache=log_cache, artifact_dir=cfg.state_dir / "artifacts", ui_origin=cfg.ui_origin,
                      ui_dev_mode=cfg.dev_mode, clock_ok=clock_ok, clock_health=clock_health)
    controller.outbox = outbox
    return store, controller, runtime


async def serve(cfg: DaemonConfig, *, restored_from_backup: bool = False) -> None:
    try:
        state_dir = validate_state_dir_path(cfg.state_dir)
    except RuntimeError as exc:
        raise SystemExit(f"fleetqd serve: {exc}") from exc
    if cfg.dev_mode and any(node.enabled for node in cfg.nodes):
        raise SystemExit("fleetqd serve: dev_mode cannot serve enabled remote targets")
    import uvicorn

    lock = ControllerLock(state_dir)
    lock.acquire()
    store, controller, runtime = build_runtime(cfg, restored=restored_from_backup)
    app = create_app(runtime)
    host, _, port = cfg.bind.rpartition(":")
    server = uvicorn.Server(uvicorn.Config(app, host=host or "127.0.0.1", port=int(port), workers=1,
                                           log_level="info", limit_concurrency=256, lifespan="off"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    ctl_task = asyncio.create_task(controller.run_forever())
    runtime.controller_task = ctl_task
    clock_task = asyncio.create_task(runtime.clock_health.run()) if runtime.clock_health else None
    outbox_task = asyncio.create_task(controller.outbox.run(store))
    if cfg.fleetmon_feed_url:
        from .feeds.fleetmon import poll_forever
        feed_task = asyncio.create_task(poll_forever(controller.shared_capacity, cfg.fleetmon_feed_url))
    else:
        feed_task = asyncio.create_task(asyncio.sleep(0))   # no feed: shared nodes stay unplaceable
    srv_task = asyncio.create_task(server.serve())
    await stop.wait()
    server.should_exit = True
    await controller.stop()
    tasks = [ctl_task, outbox_task, feed_task]
    if clock_task is not None:
        tasks.append(clock_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(srv_task, *tasks, return_exceptions=True)
    store.close()
    lock.release()


def cmd_init(cfg: DaemonConfig, args) -> int:
    try:
        state_dir = validate_state_dir_path(cfg.state_dir)
    except RuntimeError as exc:
        raise SystemExit(f"fleetqd init: {exc}") from exc
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        state_dir = validate_state_dir_path(state_dir)
    except RuntimeError as exc:
        raise SystemExit(f"fleetqd init: {exc}") from exc
    if not cfg.state_fs_uuid or not re.fullmatch(r"[0-9A-Fa-f-]{8,64}", cfg.state_fs_uuid):
        raise SystemExit("fleetqd init: [daemon] state_fs_uuid is required; refusing to initialize an unverified filesystem")
    try:
        state_info = state_dir.lstat()
        approved_info = (Path("/dev/disk/by-uuid") / cfg.state_fs_uuid).stat()
    except OSError as exc:
        raise SystemExit(f"fleetqd init: cannot verify state filesystem {cfg.state_fs_uuid!r}: {exc}") from exc
    if not stat.S_ISDIR(state_info.st_mode) or state_info.st_uid != os.getuid() or state_info.st_mode & 0o077:
        raise SystemExit(f"fleetqd init: state directory must be a real owner-only directory: {state_dir}")
    if not stat.S_ISBLK(approved_info.st_mode) or state_info.st_dev != approved_info.st_rdev:
        raise SystemExit(f"fleetqd init: {state_dir} is not on approved filesystem {cfg.state_fs_uuid}")
    sentinel = state_dir / SENTINEL_NAME
    try:
        sentinel_info = sentinel.lstat()
    except FileNotFoundError:
        sentinel_info = None
    if sentinel_info is not None:
        if (not stat.S_ISREG(sentinel_info.st_mode) or sentinel_info.st_uid != os.getuid()
                or sentinel_info.st_mode & 0o077):
            raise SystemExit(f"fleetqd init: existing sentinel must be an owned, private regular file: {sentinel}")
        print(f"{sentinel} already exists: {sentinel.read_text().strip()}")
        return 0
    volume_id = args.volume_id or secrets.token_hex(8)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(sentinel, flags, 0o600)
    try:
        data = (volume_id + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while creating state volume sentinel")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    print(f"initialized {state_dir}; set [daemon] volume_id = \"{volume_id}\" in the config")
    return 0


def cmd_doctor(cfg: DaemonConfig, args) -> int:
    checks: list[tuple[str, bool, str]] = []
    ok, msg = check_sqlite_gate(cfg.sqlite_backport_attestation)
    checks.append(("sqlite_wal_reset_fix", ok, msg))
    try:
        check_state_volume(cfg.state_dir, cfg.volume_id, cfg.state_fs_uuid, require_identity=not cfg.dev_mode)
        checks.append(("state_volume", True, str(cfg.state_dir)))
    except RuntimeError as exc:
        checks.append(("state_volume", False, str(exc)))
    checks.append(("fleetctl", cfg.fleetctl.exists(), str(cfg.fleetctl)))
    checks.append(("clock_provider", cfg.dev_mode or cfg.clock_provider in {p.value for p in ClockProvider},
                   cfg.clock_provider or "not configured"))
    enabled = [n.id for n in cfg.nodes if n.enabled]
    checks.append(("enabled_targets", True, ", ".join(enabled) or "none (every node and site is disabled)"))
    if cfg.db_path.exists():
        conn = sqlite3.connect(cfg.db_path)
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        checks.append(("db_quick_check", integrity == "ok", integrity))
        conn.close()
    report = {"ok": all(c[1] for c in checks), "version": __version__,
              "checks": [{"check": n, "ok": o, "detail": d} for n, o, d in checks]}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for n, o, d in checks:
            print(f"{'ok  ' if o else 'FAIL'} {n}: {d}")
    return 0 if report["ok"] else 1


def cmd_token(cfg: DaemonConfig, args) -> int:
    store = open_store(cfg)
    try:
        if args.token_cmd == "create":
            scopes = tuple(args.scopes.split(",")) if args.scopes else None
            quota = json.loads(args.quota) if args.quota else None
            token_id, token = store.run_sync(lambda c: auth.create_token(
                c, owner=args.owner, kind=args.kind, label=args.label, scopes=scopes,
                allow_clusters=args.clusters, expires_at=args.expires, quota=quota))
            out = Path(args.out).expanduser() if args.out else None
            if out:
                fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(token + "\n")
                print(f"token {token_id} written to {out} (mode 0600)")
            else:
                print(token)
        elif args.token_cmd == "revoke":
            store.run_sync(lambda c: auth.revoke_token(c, args.token_id))
            print(f"revoked {args.token_id}")
        else:
            rows = store.run_sync(lambda c: [dict(r) for r in c.execute(
                "SELECT id, owner, kind, label, scopes, allow_clusters, created_at, revoked_at FROM tokens")])
            print(json.dumps(rows, indent=2))
    finally:
        store.close()
    return 0


def cmd_node(cfg: DaemonConfig, args) -> int:
    from . import onboard
    from .transport.fleetctl import Fleetctl

    store = open_store(cfg)
    try:
        if args.node_cmd == "show":
            row = store.run_sync(lambda c: c.execute(
                "SELECT last_probe_json, last_probe_at FROM nodes WHERE id = ?", (args.name,)).fetchone())
            if row is None or row["last_probe_json"] is None:
                print(f"{args.name}: never probed", file=sys.stderr)
                return 3
            report = {"node": args.name, "probed_at": row["last_probe_at"],
                      **json.loads(row["last_probe_json"])["verdict"]}
        else:
            transport = Fleetctl(cfg.fleetctl, config_home=cfg.fleet_config_home, caller="fleetqd-onboard",
                                 pycache_dir=cfg.state_dir / "pycache")
            try:
                if args.node_cmd == "install":
                    report = asyncio.run(onboard.install(cfg, store, transport, args.name,
                                                         shim=Path(args.shim) if args.shim else onboard.SHIM_BUILD))
                else:
                    report = {"node": args.name, **asyncio.run(onboard.probe(cfg, store, transport, args.name))}
            except onboard.OnboardError as exc:
                print(f"fleetqd: {exc.code}: {exc.message}", file=sys.stderr)
                return 2
    finally:
        store.close()
    print(json.dumps(report, indent=2, sort_keys=True))
    verdict = report.get("probe", report)
    return 0 if verdict.get("fit", True) else 1


def cmd_backup(cfg: DaemonConfig, args) -> int:
    store = open_store(cfg)
    try:
        store.backup(Path(args.dest).expanduser())
    finally:
        store.close()
    print(f"backed up to {args.dest}")
    return 0


def cmd_gc(cfg: DaemonConfig, args) -> int:
    """Preview local GC; --apply expires old artifacts and removes old unreferenced bundles."""
    try:
        state_dir = validate_state_dir_path(cfg.state_dir)
    except RuntimeError as exc:
        print(f"fleetqd gc: {exc}", file=sys.stderr)
        return 2
    # Serialize with the sole controller and its bundle upload path. GC is an
    # offline maintenance command, not concurrent with fleetqd serve.
    lock = ControllerLock(state_dir)
    lock.acquire()
    store = None
    try:
        store = open_store(cfg)
        report = store.run_sync(lambda c: (gc_module.apply if args.apply else gc_module.preview)(
            c, cfg.bundle_dir, artifact_dir=cfg.state_dir / "artifacts"))
    finally:
        if store is not None:
            store.close()
        lock.release()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"fleetqd gc ({report['mode']}): {len(report['eligible'])} bundles eligible, "
              f"{len(report['removed'])} bundles removed, {len(report['eligible_temp'])} upload parts eligible, "
              f"{len(report['removed_temp'])} upload parts removed, {len(report['eligible_artifacts'])} artifacts eligible, "
              f"{len(report['purgeable_artifacts'])} artifacts purgeable, "
              f"{len(report['expired_artifacts'])} artifacts expired, {len(report['removed_artifacts'])} artifacts purged, "
              f"{len(report['eligible_events'])} event details eligible, {len(report['redacted_events'])} redacted, "
              f"{len(report['blocked']) + len(report['blocked_temp']) + len(report['blocked_artifacts']) + len(report['blocked_events'])} blocked")
        for item in report["eligible"]:
            print(f"  eligible {item['digest']} ({item['bytes']} bytes, age {item['age_seconds']}s)")
        for item in report["removed"]:
            print(f"  removed  {item['digest']} ({item['bytes']} bytes)")
        for item in report["eligible_temp"]:
            print(f"  eligible upload part {item['path']} ({item['bytes']} bytes, age {item['age_seconds']}s)")
        for item in report["removed_temp"]:
            print(f"  removed upload part {item['path']} ({item['bytes']} bytes)")
        for item in report["eligible_artifacts"]:
            print(f"  eligible artifact job {item['job_id']} ({item['files']} files, {item['bytes']} bytes, age {item['age_seconds']}s)")
        for item in report["purgeable_artifacts"]:
            print(f"  purgeable artifact job {item['job_id']} ({item['files']} files, {item['bytes']} bytes)")
        for item in report["expired_artifacts"]:
            print(f"  expired  artifact job {item['job_id']} (execution outcome retained)")
        for item in report["removed_artifacts"]:
            print(f"  purged   artifact job {item['job_id']} ({item['bytes']} bytes)")
        for item in report["eligible_events"]:
            print(f"  eligible event detail {item['event_id']} (job {item['job_id']}, {item['kind']}, age {item['age_seconds']}s)")
        for item in report["redacted_events"]:
            print(f"  redacted event detail {item['event_id']} (job {item['job_id']}, {item['kind']})")
        for item in report["blocked"]:
            print(f"  blocked  {item.get('digest', item.get('path', 'GC'))}: {item['reason']}")
        for item in report["blocked_temp"]:
            print(f"  blocked  upload part {item['path']}: {item['reason']}")
        for item in report["blocked_artifacts"]:
            print(f"  blocked  artifact job {item.get('job_id', 'GC')}: {item['reason']}")
        for item in report["blocked_events"]:
            print(f"  blocked  event {item.get('event_id', 'GC')}: {item['reason']}")
        for limitation in report["limitations"]:
            print(f"  limitation: {limitation}")
    return 1 if report["blocked"] or report["blocked_temp"] or report["blocked_artifacts"] or report["blocked_events"] else 0


def cmd_slurm_cache_gc(cfg: DaemonConfig, args) -> int:
    """Run an explicit, budgeted inspect or one-digest purge on one Slurm target."""
    target = args.target
    node = next((n for n in cfg.nodes if n.id == target), None)
    if node is None or node.backend != "slurm":
        print(f"fleetqd slurm-cache-gc: {target!r} is not a configured Slurm target", file=sys.stderr)
        return 2
    if args.purge is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", args.purge):
        print("fleetqd slurm-cache-gc: --purge requires sha256:<64 lowercase hex>", file=sys.stderr)
        return 2
    if args.purge is not None and not getattr(args, "authorized", False):
        print(f"fleetqd slurm-cache-gc: purge requires --i-authorize-target-{target}", file=sys.stderr)
        return 2
    try:
        state_dir = validate_state_dir_path(cfg.state_dir)
    except RuntimeError as exc:
        print(f"fleetqd slurm-cache-gc: {exc}", file=sys.stderr)
        return 2
    # Match local GC: the lock must be held before opening the state DB, and
    # remains held across the remote operation so serve cannot race this action.
    lock = ControllerLock(state_dir)
    try:
        lock.acquire()
    except (RuntimeError, OSError) as exc:
        print(f"fleetqd slurm-cache-gc: {exc}", file=sys.stderr)
        return 2
    store = None
    try:
        store = open_store(cfg)
        def identity(conn):
            row = conn.execute("SELECT value FROM controller_meta WHERE key='fleet_id'").fetchone()
            node_row = conn.execute("SELECT state FROM nodes WHERE id=?", (target,)).fetchone()
            return (row["value"] if row else None, fence.current_epoch(conn),
                    fence.target_dispatch_ready(conn, target),
                    bool(node_row) and node_row["state"] != "QUARANTINED")

        fleet_id, epoch, ready, not_quarantined = store.run_sync(identity)
        if not fleet_id or epoch <= 0 or not ready or not not_quarantined:
            print("fleetqd slurm-cache-gc: target is not reconciled to the current controller fence",
                  file=sys.stderr)
            return 2
        executor = build_executors(cfg, store)["slurm"]
        executor.fleet_id = fleet_id
        report = asyncio.run(executor.cache_gc(target, epoch=epoch, purge=args.purge))
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": "remote_or_store_error", "detail": str(exc)}, sort_keys=True))
        return 2
    finally:
        if store is not None:
            store.close()
        lock.release()
    if report is None:
        print(json.dumps({"ok": False, "reason": "budget_deferred_or_transport_unavailable"}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ok") and not report.get("blocked") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleetqd")
    parser.add_argument("--config", default=os.environ.get("FLEETQ_CONFIG", "~/.config/fleetq/fleetqd.toml"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--volume-id")
    p = sub.add_parser("serve")
    p.add_argument("--restored-from-backup", action="store_true",
                   help="gate dispatch until approved-target discovery and fencing complete")
    p = sub.add_parser("doctor")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("token")
    tsub = p.add_subparsers(dest="token_cmd", required=True)
    t = tsub.add_parser("create")
    t.add_argument("--owner", required=True)
    t.add_argument("--kind", choices=sorted(auth.KINDS), required=True)
    t.add_argument("--label", required=True)
    t.add_argument("--scopes")
    t.add_argument("--clusters", action="store_true", help="authorize cluster use (off by default)")
    t.add_argument("--expires")
    t.add_argument("--quota", help='JSON, e.g. {"gpus": 2}')
    t.add_argument("--out", help="write the token to this new 0600 file instead of stdout")
    t = tsub.add_parser("revoke")
    t.add_argument("token_id")
    tsub.add_parser("list")
    p = sub.add_parser("backup")
    p.add_argument("dest")
    p = sub.add_parser("gc", help="preview safe local garbage collection; --apply deletes eligible bundles")
    p.add_argument("--apply", action="store_true", help="delete only eligible unreferenced bundles")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("slurm-cache-gc", help="inspect or purge one digest in one Slurm shared-root cache")
    p.add_argument("target", help="one configured Slurm target")
    p.add_argument("--purge", help="purge one reviewed sha256:<64 lowercase hex> digest")
    p = sub.add_parser("node")
    nsub = p.add_subparsers(dest="node_cmd", required=True)
    n = nsub.add_parser("install", help="push the shim, enroll and probe exactly one workstation")
    n.add_argument("name")
    n.add_argument("--shim", help="fq-node build to install (default: build/fq-node)")
    for verb in ("probe", "show"):
        nsub.add_parser(verb).add_argument("name")
    args, extra = parser.parse_known_args(argv)
    if args.cmd == "node" and args.node_cmd == "install":
        # The literal, target-matching flag, as fleetmon's install-helper requires.
        wanted = f"--i-authorize-target-{args.name}"
        if extra != [wanted]:
            print(f"fleetqd: installing on {args.name!r} contacts that machine; pass exactly {wanted}",
                  file=sys.stderr)
            return 2
    elif args.cmd == "slurm-cache-gc":
        wanted = f"--i-authorize-target-{args.target}"
        if extra == [wanted]:
            args.authorized = True
        elif extra:
            parser.error(f"slurm-cache-gc purge authorization must be exactly {wanted}")
        else:
            args.authorized = False
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        cfg = load_config(Path(args.config).expanduser())
    except FqError as exc:
        print(f"fleetqd: {exc.message}", file=sys.stderr)
        return 2
    if args.cmd == "serve":
        asyncio.run(serve(cfg, restored_from_backup=args.restored_from_backup))
        return 0
    return {"init": cmd_init, "doctor": cmd_doctor, "token": cmd_token, "backup": cmd_backup,
            "node": cmd_node, "gc": cmd_gc, "slurm-cache-gc": cmd_slurm_cache_gc}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
