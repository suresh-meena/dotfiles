"""Strict, machine-readable fleetctl inventory discovery."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .poller import run_command

DIRECT_ROLES = {"workstation", "compute"}
SCHEDULER_KINDS = {"slurm", "scheduler", "scheduler-backed"}
MAX_TARGETS = 256
MAX_TARGET_TAGS = 64
MAX_PROTOCOLS = 256
MAX_NAME_LENGTH = 256
MAX_DISCOVERY_STDOUT_BYTES = 1024 * 1024
MAX_DISCOVERY_STDERR_BYTES = 64 * 1024
MAX_DISCOVERY_TIMEOUT_SECONDS = 20.0
MAX_RESOLUTION_SECONDS = 60.0


@dataclass(frozen=True)
class Target:
    name: str
    enabled: bool = True
    role: str = ""
    protocol: str = ""
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Protocol:
    name: str
    kind: str


@dataclass
class Inventory:
    targets: list[Target]
    protocols: dict[str, Protocol]

    @property
    def direct_targets(self) -> list[Target]:
        return [
            target
            for target in self.targets
            if target.enabled
            and target.role in DIRECT_ROLES
            and self.protocols.get(target.protocol, Protocol("", "")).kind == "direct"
        ]

    @property
    def scheduler_targets(self) -> list[Target]:
        return [
            target
            for target in self.targets
            if target.enabled
            and target.role == "login"
            and self.protocols.get(target.protocol, Protocol("", "")).kind
            in SCHEDULER_KINDS
        ]


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {what} JSON")
    return value


def _identifier(value: Any, what: str) -> str:
    """Validate values that will become a fleetctl argument or a UI value."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_NAME_LENGTH
        or value != value.strip()
        or value.startswith("-")
        or any(
            char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value
        )
    ):
        raise ValueError(f"invalid {what}")
    return value


def parse_inventory(data: Any) -> list[Target]:
    if isinstance(data, list):
        rows = data
    else:
        root = _object(data, "inventory")
        rows = root.get("targets", root.get("hosts", root.get("items")))
    if not isinstance(rows, list):
        raise ValueError("inventory targets must be a list")
    if len(rows) > MAX_TARGETS:
        raise ValueError("inventory target count exceeds limit")

    targets: list[Target] = []
    seen: set[str] = set()
    for value in rows:
        row = _object(value, "target")
        name = row.get("name", row.get("target"))
        role = row.get("role")
        protocol = row.get("protocol", row.get("protocol_name"))
        enabled = row.get("enabled", True)
        tags = row.get("tags", [])
        if not isinstance(enabled, bool):
            raise ValueError("target enabled must be boolean")
        try:
            name = _identifier(name, "target name")
            role = _identifier(role, "target role")
            protocol = _identifier(protocol, "target protocol")
        except ValueError:
            raise ValueError("target missing name, role, or protocol") from None
        if name in seen:
            raise ValueError("duplicate target name")
        if (
            not isinstance(tags, list)
            or len(tags) > MAX_TARGET_TAGS
            or any(
                not isinstance(tag, str)
                or not tag
                or len(tag) > MAX_NAME_LENGTH
                or tag != tag.strip()
                or any(
                    char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
                    for char in tag
                )
                for tag in tags
            )
        ):
            raise ValueError("target tags must be strings")
        seen.add(name)
        targets.append(Target(name, enabled, role, protocol, frozenset(tags)))
    return targets


def parse_protocol(data: Any, name: str = "") -> Protocol:
    root = _object(data, "protocol")
    protocol_name = root.get("name", name)
    kind = root.get("kind", root.get("type"))
    try:
        protocol_name = _identifier(protocol_name, "protocol name")
        kind = _identifier(kind, "protocol kind")
    except ValueError:
        raise ValueError("protocol missing name or kind") from None
    if name and protocol_name != _identifier(name, "protocol name"):
        raise ValueError("protocol name does not match requested protocol")
    return Protocol(protocol_name, kind)


def _run_bounded(argv: Sequence[str]) -> tuple[str, str]:
    """Run fleetctl while retaining only bounded stdout/stderr.

    ``subprocess.run(..., capture_output=True)`` buffers an entire response
    before the caller can validate it. Inventory is externally supplied data,
    so read both pipes incrementally and kill the process group at the first
    limit or timeout.
    """

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    streams = {
        process.stdout.fileno(): (process.stdout, MAX_DISCOVERY_STDOUT_BYTES),
        process.stderr.fileno(): (process.stderr, MAX_DISCOVERY_STDERR_BYTES),
    }
    for fd in streams:
        selector.register(fd, selectors.EVENT_READ)
    buffers = {fd: bytearray() for fd in streams}
    deadline = time.monotonic() + MAX_DISCOVERY_TIMEOUT_SECONDS
    failed = ""
    try:
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failed = "fleetctl discovery timed out"
                break
            events = selector.select(remaining)
            if not events:
                failed = "fleetctl discovery timed out"
                break
            for key, _ in events:
                fd = key.fd
                stream, limit = streams[fd]
                chunk = os.read(fd, min(65_536, limit - len(buffers[fd]) + 1))
                if not chunk:
                    selector.unregister(fd)
                    streams.pop(fd)
                    continue
                buffers[fd].extend(chunk)
                if len(buffers[fd]) > limit:
                    failed = "fleetctl discovery output exceeds limit"
                    break
            if failed:
                break
    finally:
        selector.close()
        if failed or process.poll() is None:
            with suppress(ProcessLookupError, OSError):
                os.killpg(process.pid, 9)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    if failed:
        raise RuntimeError(failed)
    if process.returncode != 0:
        raise RuntimeError("fleetctl discovery failed")
    stdout_fd, stderr_fd = list(buffers)
    try:
        return bytes(buffers[stdout_fd]).decode("utf-8"), bytes(
            buffers[stderr_fd]
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid fleetctl UTF-8") from exc


def _protocol_requests(
    targets: list[Target], resolution_budget: float
) -> Iterator[tuple[list[str], str]]:
    """Yield the bounded protocol-resolution plan shared by both discoverers."""

    protocol_names = sorted({target.protocol for target in targets})
    if len(protocol_names) > MAX_PROTOCOLS:
        raise ValueError("inventory protocol count exceeds limit")
    deadline = time.monotonic() + max(0.0, float(resolution_budget))
    for name in protocol_names:
        if time.monotonic() >= deadline:
            break
        yield ["protocol", "show", name, "--json"], name


def discover(
    fleetctl: str = "fleetctl",
    runner: Callable[[Sequence[str]], Any] | None = None,
    resolution_budget: float = MAX_RESOLUTION_SECONDS,
) -> Inventory:
    """Run fleetctl list and resolve each distinct protocol exactly once.

    Total protocol-resolution time is bounded; protocols that cannot be
    resolved within the budget are left unadmitted, and the inventory itself
    is never a failure.
    """

    def run(argv: Sequence[str]) -> Any:
        if runner is not None:
            return runner(argv)
        stdout, _stderr = _run_bounded([fleetctl, *argv])
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid fleetctl JSON") from exc

    targets = parse_inventory(run(["list", "--json"]))
    protocols = {
        name: parse_protocol(run(argv), name)
        for argv, name in _protocol_requests(targets, resolution_budget)
    }
    return Inventory(targets, protocols)


async def discover_async(
    fleetctl: str = "fleetctl",
    resolution_budget: float = MAX_RESOLUTION_SECONDS,
) -> Inventory:
    """Asynchronous hub discovery with bounded subprocess output."""

    async def run(argv: list[str]) -> Any:
        result = await run_command(
            [fleetctl, *argv],
            timeout=20,
            stdout_limit=1024 * 1024,
            stderr_limit=64 * 1024,
        )
        if result.returncode != 0 or result.timed_out or result.overflow:
            raise RuntimeError("fleetctl discovery failed")
        try:
            return json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid fleetctl JSON") from exc

    targets = parse_inventory(await run(["list", "--json"]))
    protocols: dict[str, Protocol] = {}
    for argv, name in _protocol_requests(targets, resolution_budget):
        protocols[name] = parse_protocol(await run(argv), name)
    return Inventory(targets, protocols)


def admitted_targets(
    inventory: Inventory,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
) -> list[Target]:
    include = include_tags or set()
    exclude = exclude_tags or set()
    return [
        target
        for target in inventory.direct_targets
        if include <= target.tags and not target.tags.intersection(exclude)
    ]
