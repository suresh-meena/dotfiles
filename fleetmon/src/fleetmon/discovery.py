"""Strict, machine-readable fleetctl inventory discovery."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
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


def _protocol_requests(
    targets: list[Target], resolution_budget: float
) -> Iterator[tuple[list[str], str]]:
    """Yield the bounded protocol-resolution plan for discovery."""

    protocol_names = sorted({target.protocol for target in targets})
    if len(protocol_names) > MAX_PROTOCOLS:
        raise ValueError("inventory protocol count exceeds limit")
    deadline = time.monotonic() + max(0.0, float(resolution_budget))
    for name in protocol_names:
        if time.monotonic() >= deadline:
            break
        yield ["protocol", "show", name, "--json"], name


async def discover_async(
    fleetctl: str = "fleetctl",
    resolution_budget: float = MAX_RESOLUTION_SECONDS,
) -> Inventory:
    """Run fleetctl list and resolve each distinct protocol exactly once.

    Subprocess output is bounded. Total protocol-resolution time is bounded
    too; protocols that cannot be resolved within the budget are left
    unadmitted, and the inventory itself is never a failure.
    """

    async def run(argv: list[str]) -> Any:
        result = await run_command(
            [fleetctl, *argv],
            timeout=MAX_DISCOVERY_TIMEOUT_SECONDS,
            stdout_limit=MAX_DISCOVERY_STDOUT_BYTES,
            stderr_limit=MAX_DISCOVERY_STDERR_BYTES,
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
