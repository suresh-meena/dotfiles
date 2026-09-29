"""Configuration loading with non-bypassable safety ceilings."""

from __future__ import annotations

import math
import os
import shutil
import stat as stat_module
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from . import notify
from .slurm import scheduler_zone

MIN_POLL_INTERVAL_SECONDS = 2.0
MAX_POLL_INTERVAL_SECONDS = 24 * 60 * 60.0
MIN_SCHEDULER_INTERVAL_SECONDS = 60.0
MIN_HISTORY_INTERVAL_SECONDS = 60.0
MIN_LAUNCH_INTERVAL_SECONDS = 0.05
MAX_LAUNCH_INTERVAL_SECONDS = 60 * 60.0
MAX_SSH_CONCURRENCY = 8
MAX_REMOTE_TIMEOUT_SECONDS = 20.0
MAX_SNAPSHOT_STDOUT_BYTES = 256 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_API_ROWS = 1_000
MAX_TARGET_NAME_LENGTH = 256
MAX_TARGET_FILTERS = 1_000
MAX_MANAGED_SCHEDULER_TARGETS = 256
MAX_TRUSTED_NETWORKS = 16


class ConfigError(ValueError):
    """Raised when configuration would make the service unsafe or ambiguous."""


@dataclass(frozen=True)
class HubConfig:
    fleetctl_path: Path
    state_dir: Path
    database_path: Path
    bind_host: str = "127.0.0.1"
    bind_port: int = 8088
    auth_token: str | None = None
    trusted_networks: tuple[IPv4Network | IPv6Network, ...] = ()
    notify_url: str | None = None
    polling_enabled: bool = True
    disabled_targets: tuple[str, ...] = ()
    include_tag: str | None = None
    exclude_tag: str | None = None
    poll_interval_seconds: float = 60.0
    scheduler_interval_seconds: float = 60.0
    history_interval_seconds: float = 60.0
    inventory_interval_seconds: float = 300.0
    scheduler_timezone: str = "UTC"
    launch_interval_seconds: float = 2.0
    ssh_concurrency: int = 2
    remote_timeout_seconds: float = 20.0
    snapshot_stdout_bytes: int = MAX_SNAPSHOT_STDOUT_BYTES
    stderr_bytes: int = MAX_STDERR_BYTES
    retention_days: int = 30
    disk_reserve_bytes: int = 512 * 1024 * 1024
    backup_dir: Path | None = None
    # fleetqd's read API, for the Queue page and GPU allocation marks. Read-only:
    # fleetmon never mutates the queue, and a down scheduler only greys the page.
    scheduler_url: str | None = None
    scheduler_token: str | None = None
    # Canonical public HTTPS origin for fleetqd's human confirmation UI.
    scheduler_ui_origin: str | None = None
    # Inventory names whose Slurm observation is owned by fleetqd. They must
    # never be queried by fleetmon's legacy direct squeue/sacct poller.
    managed_scheduler_targets: tuple[str, ...] = ()

    def bind_is_trusted(self) -> bool:
        """Whether this bind sits inside a network trusted to skip the token.

        Loopback and the mesh-VPN CGNAT range are always trusted. A bind
        inside a configured ``trusted_networks`` range is trusted the same
        way: the network, not this application, is the authentication
        boundary. The wildcard bind is trusted only when explicit trusted
        networks are configured, because it spans every interface at once.
        """

        if _is_loopback_bind(self.bind_host) or _is_vpn_bind(self.bind_host):
            return True
        if not self.trusted_networks:
            return False
        if self.bind_host in {"0.0.0.0", "::"}:
            return True
        try:
            address = ip_address(self.bind_host)
        except ValueError:
            return False
        return any(address in net for net in self.trusted_networks)

    @classmethod
    def defaults(cls) -> HubConfig:
        state_dir = Path(
            os.environ.get(
                "FLEETMON_STATE_DIR",
                Path.home() / ".local" / "state" / "fleetmon",
            )
        ).expanduser()
        executable = shutil.which("fleetctl") or "/usr/bin/fleetctl"
        return cls(
            fleetctl_path=Path(executable),
            state_dir=state_dir,
            database_path=state_dir / "fleet.db",
        )

    def validate(self) -> HubConfig:
        if not isinstance(self.bind_host, str) or not self.bind_host.strip():
            raise ConfigError("bind_host must be a non-empty string")
        if not all(
            isinstance(path, Path) and path.is_absolute()
            for path in (self.fleetctl_path, self.state_dir, self.database_path)
        ):
            raise ConfigError(
                "fleetctl_path, state_dir, and database_path must be absolute paths"
            )
        normalized_state = Path(os.path.abspath(self.state_dir))
        normalized_database_parent = Path(os.path.abspath(self.database_path.parent))
        if normalized_state in {Path("/"), Path("/tmp"), Path.home()}:
            raise ConfigError("state_dir must be a dedicated application directory")
        if normalized_database_parent != normalized_state:
            raise ConfigError("database_path must be directly inside state_dir")
        numeric_fields = (
            (self.poll_interval_seconds, "poll_interval_seconds"),
            (self.scheduler_interval_seconds, "scheduler_interval_seconds"),
            (self.history_interval_seconds, "history_interval_seconds"),
            (self.inventory_interval_seconds, "inventory_interval_seconds"),
            (self.launch_interval_seconds, "launch_interval_seconds"),
            (self.remote_timeout_seconds, "remote_timeout_seconds"),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite_number(value)
            for value, _ in numeric_fields
        ):
            bad_name = next(
                name
                for value, name in numeric_fields
                if isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not _is_finite_number(value)
            )
            raise ConfigError(f"{bad_name} must be a finite number")
        integer_fields = (
            (self.bind_port, "bind_port"),
            (self.ssh_concurrency, "ssh_concurrency"),
            (self.snapshot_stdout_bytes, "snapshot_stdout_bytes"),
            (self.stderr_bytes, "stderr_bytes"),
            (self.retention_days, "retention_days"),
            (self.disk_reserve_bytes, "disk_reserve_bytes"),
        )
        for value, name in integer_fields:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{name} must be an integer")
        if self.auth_token is not None and (
            not isinstance(self.auth_token, str) or not self.auth_token.strip()
        ):
            raise ConfigError("auth_token must be a non-empty string")
        if not isinstance(self.polling_enabled, bool):
            raise ConfigError("polling_enabled must be a boolean")
        if not self.fleetctl_path.is_file() or not os.access(
            self.fleetctl_path, os.X_OK
        ):
            raise ConfigError("fleetctl_path must be an executable file")
        if (
            not MIN_POLL_INTERVAL_SECONDS
            <= self.poll_interval_seconds
            <= MAX_POLL_INTERVAL_SECONDS
        ):
            raise ConfigError(
                "poll_interval_seconds must be "
                f"{MIN_POLL_INTERVAL_SECONDS:g}..{MAX_POLL_INTERVAL_SECONDS:g}"
            )
        if not 60 <= self.inventory_interval_seconds <= MAX_POLL_INTERVAL_SECONDS:
            raise ConfigError(
                f"inventory_interval_seconds must be 60..{MAX_POLL_INTERVAL_SECONDS:g}"
            )
        if (
            not MIN_SCHEDULER_INTERVAL_SECONDS
            <= self.scheduler_interval_seconds
            <= MAX_POLL_INTERVAL_SECONDS
        ):
            raise ConfigError(
                "scheduler_interval_seconds must be "
                f"{MIN_SCHEDULER_INTERVAL_SECONDS:g}..{MAX_POLL_INTERVAL_SECONDS:g}"
            )
        if (
            not MIN_HISTORY_INTERVAL_SECONDS
            <= self.history_interval_seconds
            <= MAX_POLL_INTERVAL_SECONDS
        ):
            raise ConfigError(
                "history_interval_seconds must be "
                f"{MIN_HISTORY_INTERVAL_SECONDS:g}..{MAX_POLL_INTERVAL_SECONDS:g}"
            )
        if (
            not MIN_LAUNCH_INTERVAL_SECONDS
            <= self.launch_interval_seconds
            <= MAX_LAUNCH_INTERVAL_SECONDS
        ):
            raise ConfigError(
                "launch_interval_seconds must be "
                f"{MIN_LAUNCH_INTERVAL_SECONDS:g}..{MAX_LAUNCH_INTERVAL_SECONDS:g}"
            )
        if not 1 <= self.ssh_concurrency <= MAX_SSH_CONCURRENCY:
            raise ConfigError(f"ssh_concurrency must be 1..{MAX_SSH_CONCURRENCY}")
        if not 1 <= self.remote_timeout_seconds <= MAX_REMOTE_TIMEOUT_SECONDS:
            raise ConfigError(
                f"remote_timeout_seconds must be 1..{MAX_REMOTE_TIMEOUT_SECONDS:g}"
            )
        if not 1 <= self.snapshot_stdout_bytes <= MAX_SNAPSHOT_STDOUT_BYTES:
            raise ConfigError(
                f"snapshot_stdout_bytes must be <= {MAX_SNAPSHOT_STDOUT_BYTES}"
            )
        if not 1 <= self.stderr_bytes <= MAX_STDERR_BYTES:
            raise ConfigError(f"stderr_bytes must be <= {MAX_STDERR_BYTES}")
        if not 1 <= self.bind_port <= 65_535:
            raise ConfigError("bind_port must be 1..65535")
        if not isinstance(self.trusted_networks, tuple) or any(
            not isinstance(net, (IPv4Network, IPv6Network))
            for net in self.trusted_networks
        ):
            raise ConfigError("hub.trusted_networks must be a tuple of networks")
        if len(self.trusted_networks) > MAX_TRUSTED_NETWORKS:
            raise ConfigError(
                f"hub.trusted_networks must contain <= {MAX_TRUSTED_NETWORKS} networks"
            )
        if not self.bind_is_trusted() and (
            not self.auth_token
            or not self.auth_token.strip()
            or len(self.auth_token) < 32
        ):
            raise ConfigError(
                "non-loopback bind requires FLEETMON_AUTH_TOKEN with at least 32 "
                "characters, or the bind address must be inside hub.trusted_networks"
            )
        if not 1 <= self.retention_days <= 365:
            raise ConfigError("retention_days must be 1..365")
        if self.disk_reserve_bytes < 64 * 1024 * 1024:
            raise ConfigError("disk_reserve_bytes must be at least 64 MiB")
        if self.backup_dir is not None and (
            not isinstance(self.backup_dir, Path) or not self.backup_dir.is_absolute()
        ):
            raise ConfigError("hub.backup_dir must be an absolute path")
        if not isinstance(self.disabled_targets, tuple):
            raise ConfigError("disabled_targets must be a tuple of strings")
        if not isinstance(self.managed_scheduler_targets, tuple):
            raise ConfigError("managed_scheduler_targets must be a tuple of strings")
        if len(self.managed_scheduler_targets) > MAX_MANAGED_SCHEDULER_TARGETS:
            raise ConfigError(
                "managed_scheduler_targets must contain <= "
                f"{MAX_MANAGED_SCHEDULER_TARGETS} targets"
            )
        if self.managed_scheduler_targets and not self.scheduler_url:
            raise ConfigError("managed scheduler targets require scheduler.url")
        if self.scheduler_ui_origin is not None:
            if not self.scheduler_url:
                raise ConfigError("scheduler.ui_origin requires scheduler.url")
            if _scheduler_ui_origin(self.scheduler_ui_origin) != self.scheduler_ui_origin:
                raise ConfigError("scheduler.ui_origin must be a canonical HTTPS origin")
        for target in self.managed_scheduler_targets:
            _target_name(target, "scheduler.managed_targets")
        if len(set(self.managed_scheduler_targets)) != len(self.managed_scheduler_targets):
            raise ConfigError("managed scheduler targets must not contain duplicates")
        if len(self.disabled_targets) > MAX_TARGET_FILTERS:
            raise ConfigError(
                f"disabled_targets must contain <= {MAX_TARGET_FILTERS} targets"
            )
        for target in self.disabled_targets:
            _target_name(target, "polling.disabled_targets")
        if self.include_tag is not None:
            _target_name(self.include_tag, "polling.include_tag")
        if self.exclude_tag is not None:
            _target_name(self.exclude_tag, "polling.exclude_tag")
        try:
            scheduler_zone(self.scheduler_timezone)
        except ValueError as exc:
            raise ConfigError(f"polling.scheduler_timezone: {exc}") from exc
        return self


def _is_loopback_bind(host: str) -> bool:
    """Return whether a bind host is unambiguously loopback-only."""

    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _is_vpn_bind(host: str) -> bool:
    """Return whether a bind host is inside the trusted mesh-VPN range.

    100.64.0.0/10 is the CGNAT range used by Tailscale and comparable mesh
    VPNs, where the network itself already authenticates every device. A
    bind there is treated as the plan's "encrypted trusted VPN" boundary:
    no application-level token is required. Ordinary LAN ranges are NOT
    included; they still require a token.
    """

    try:
        return ip_address(host) in ip_network("100.64.0.0/10")
    except ValueError:
        return False


def _trusted_networks_from_config(
    value: Any,
) -> tuple[IPv4Network | IPv6Network, ...]:
    """Parse ``hub.trusted_networks``: a bounded list of CIDR strings.

    Bare addresses are accepted and become single-host networks. Everything
    invalid fails closed: a mistyped range must never silently widen or
    narrow the authentication boundary.
    """

    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_TRUSTED_NETWORKS:
        raise ConfigError(
            "hub.trusted_networks must be an array of at most "
            f"{MAX_TRUSTED_NETWORKS} CIDR strings"
        )
    networks: list[IPv4Network | IPv6Network] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError("hub.trusted_networks entries must be non-empty strings")
        try:
            networks.append(ip_network(entry.strip(), strict=False))
        except ValueError as exc:
            raise ConfigError(f"hub.trusted_networks: invalid CIDR {entry!r}") from exc
    return tuple(networks)


def _stat_dev(path: Path) -> int | None:
    """Return the filesystem device id of ``path``, or None when absent."""

    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def ensure_backup_filesystem(config: HubConfig) -> None:
    """Create the configured backup directory and fail closed on a shared fs.

    Called once at hub startup after the state directory exists so the device
    comparison sees both sides; a same-device destination is not a backup.
    """

    if config.backup_dir is None:
        return
    try:
        config.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError("hub.backup_dir must be a creatable directory") from exc
    state_dev = _stat_dev(config.state_dir)
    backup_dev = _stat_dev(config.backup_dir)
    if state_dev is not None and backup_dev is not None and state_dev == backup_dev:
        raise ConfigError(
            "hub.backup_dir must be on a separate filesystem from state_dir"
        )


def _notify_url_from_environment() -> str | None:
    value = os.environ.get("FLEETMON_NOTIFY_URL")
    if not value:
        return None
    try:
        return notify.parse_notify_url(value)
    except ValueError as exc:
        raise ConfigError(f"FLEETMON_NOTIFY_URL: {exc}") from exc


def _auth_token_from_environment(prefix: str = "FLEETMON_AUTH_TOKEN") -> str | None:
    """Read a token from the environment or a token file.

    Tokens never come from TOML, argv, or logs. Exactly one source may be
    configured; the file must be owner-only (mode 0600) or startup fails.
    Used for the dashboard's own token and for the scheduler read token.
    """

    token = os.environ.get(prefix)
    token_file = os.environ.get(f"{prefix}_FILE")
    if token and token_file:
        raise ConfigError(f"set only one of {prefix} or {prefix}_FILE")
    if token_file:
        return _read_token_file(token_file, f"{prefix}_FILE")
    return _optional_string(token)


def _read_token_file(path_text: str, name: str = "FLEETMON_AUTH_TOKEN_FILE") -> str:
    """Read a token from an owner-only file, failing closed on any doubt."""

    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    try:
        stat_result = os.stat(path)
    except OSError as exc:
        raise ConfigError(f"{name} must be an existing file") from exc
    if not stat_module.S_ISREG(stat_result.st_mode):
        raise ConfigError(f"{name} must be a regular file")
    if stat_result.st_uid not in {os.geteuid(), 0}:
        raise ConfigError(f"{name} must be owned by the service user")
    if stat_result.st_mode & 0o077:
        raise ConfigError(f"{name} must not be group or world accessible")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{name} must be readable UTF-8 text") from exc
    if not value or any(ord(char) < 32 or ord(char) == 0x7F for char in value):
        raise ConfigError("token file must contain one non-empty printable token")
    return value


def _is_finite_number(value: int | float) -> bool:
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _unknown_keys(table: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown {context} keys: {', '.join(unknown)}")


def load_config(path: Path | None = None) -> HubConfig:
    """Load TOML config, rejecting unknown keys and unsafe limit overrides."""

    defaults = HubConfig.defaults()
    auth_token = _auth_token_from_environment()
    config_path = path or Path(
        os.environ.get(
            "FLEETMON_CONFIG",
            Path.home() / ".config" / "fleetmon" / "config.toml",
        )
    )
    config_path = config_path.expanduser()
    if not config_path.exists():
        return defaults.validate()

    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("invalid TOML configuration") from exc
    _unknown_keys(raw, {"hub", "polling", "retention", "scheduler"}, "top-level")
    hub = _table(raw, "hub")
    scheduler = _table(raw, "scheduler")
    _unknown_keys(scheduler, {"url", "managed_targets", "ui_origin"}, "scheduler")
    scheduler_url = _scheduler_url(scheduler.get("url"))
    scheduler_ui_origin = _scheduler_ui_origin(scheduler.get("ui_origin"))
    if scheduler_ui_origin and not scheduler_url:
        raise ConfigError("scheduler.ui_origin requires scheduler.url")
    managed_scheduler_targets = scheduler.get("managed_targets", [])
    if (
        not isinstance(managed_scheduler_targets, list)
        or len(managed_scheduler_targets) > MAX_MANAGED_SCHEDULER_TARGETS
    ):
        raise ConfigError("scheduler.managed_targets must be an array of target names")
    managed_scheduler_targets = tuple(
        _target_name(value, "scheduler.managed_targets")
        for value in managed_scheduler_targets
    )
    if len(set(managed_scheduler_targets)) != len(managed_scheduler_targets):
        raise ConfigError("scheduler.managed_targets must not contain duplicates")
    if managed_scheduler_targets and not scheduler_url:
        raise ConfigError("scheduler.managed_targets requires scheduler.url")
    polling = _table(raw, "polling")
    retention = _table(raw, "retention")
    _unknown_keys(
        hub,
        {
            "fleetctl_path",
            "state_dir",
            "database_path",
            "bind_host",
            "bind_port",
            "trusted_networks",
            "backup_dir",
        },
        "hub",
    )
    _unknown_keys(
        polling,
        {
            "enabled",
            "disabled_targets",
            "include_tag",
            "exclude_tag",
            "interval_seconds",
            "scheduler_interval_seconds",
            "history_interval_seconds",
            "inventory_interval_seconds",
            "launch_interval_seconds",
            "ssh_concurrency",
            "remote_timeout_seconds",
            "snapshot_stdout_bytes",
            "stderr_bytes",
            "scheduler_timezone",
        },
        "polling",
    )
    _unknown_keys(retention, {"days", "disk_reserve_bytes"}, "retention")

    state_dir = Path(
        _string(hub.get("state_dir", str(defaults.state_dir)), "hub.state_dir")
    ).expanduser()
    database_path = Path(
        _string(
            hub.get("database_path", str(state_dir / "fleet.db")),
            "hub.database_path",
        )
    ).expanduser()
    disabled = polling.get("disabled_targets", [])
    if (
        not isinstance(disabled, list)
        or len(disabled) > MAX_TARGET_FILTERS
        or not all(isinstance(v, str) for v in disabled)
    ):
        raise ConfigError("polling.disabled_targets must be an array of strings")
    backup_dir = None
    if hub.get("backup_dir") is not None:
        backup_dir = Path(_string(hub.get("backup_dir"), "hub.backup_dir")).expanduser()
    trusted_networks = _trusted_networks_from_config(hub.get("trusted_networks"))

    return HubConfig(
        fleetctl_path=Path(
            _string(
                hub.get("fleetctl_path", str(defaults.fleetctl_path)),
                "hub.fleetctl_path",
            )
        ).expanduser(),
        state_dir=state_dir,
        database_path=database_path,
        bind_host=_string(hub.get("bind_host", defaults.bind_host), "hub.bind_host"),
        bind_port=_integer(hub.get("bind_port", defaults.bind_port), "hub.bind_port"),
        auth_token=auth_token,
        trusted_networks=trusted_networks,
        notify_url=_notify_url_from_environment(),
        polling_enabled=_boolean(
            polling.get("enabled", defaults.polling_enabled), "polling.enabled"
        ),
        disabled_targets=tuple(
            _target_name(value, "polling.disabled_targets") for value in disabled
        ),
        include_tag=_optional_string(polling.get("include_tag")),
        exclude_tag=_optional_string(polling.get("exclude_tag")),
        scheduler_timezone=_string(
            polling.get("scheduler_timezone", defaults.scheduler_timezone),
            "polling.scheduler_timezone",
        ),
        poll_interval_seconds=_number(
            polling.get("interval_seconds", defaults.poll_interval_seconds),
            "polling.interval_seconds",
        ),
        scheduler_interval_seconds=_number(
            polling.get(
                "scheduler_interval_seconds", defaults.scheduler_interval_seconds
            ),
            "polling.scheduler_interval_seconds",
        ),
        history_interval_seconds=_number(
            polling.get("history_interval_seconds", defaults.history_interval_seconds),
            "polling.history_interval_seconds",
        ),
        inventory_interval_seconds=_number(
            polling.get(
                "inventory_interval_seconds", defaults.inventory_interval_seconds
            ),
            "polling.inventory_interval_seconds",
        ),
        launch_interval_seconds=_number(
            polling.get("launch_interval_seconds", defaults.launch_interval_seconds),
            "polling.launch_interval_seconds",
        ),
        ssh_concurrency=_integer(
            polling.get("ssh_concurrency", defaults.ssh_concurrency),
            "polling.ssh_concurrency",
        ),
        remote_timeout_seconds=_number(
            polling.get("remote_timeout_seconds", defaults.remote_timeout_seconds),
            "polling.remote_timeout_seconds",
        ),
        snapshot_stdout_bytes=_integer(
            polling.get("snapshot_stdout_bytes", defaults.snapshot_stdout_bytes),
            "polling.snapshot_stdout_bytes",
        ),
        stderr_bytes=_integer(
            polling.get("stderr_bytes", defaults.stderr_bytes),
            "polling.stderr_bytes",
        ),
        retention_days=_integer(
            retention.get("days", defaults.retention_days), "retention.days"
        ),
        disk_reserve_bytes=_integer(
            retention.get("disk_reserve_bytes", defaults.disk_reserve_bytes),
            "retention.disk_reserve_bytes",
        ),
        backup_dir=backup_dir,
        scheduler_url=scheduler_url,
        scheduler_token=(
            _auth_token_from_environment("FLEETMON_SCHEDULER_TOKEN") if scheduler_url else None
        ),
        scheduler_ui_origin=scheduler_ui_origin,
        managed_scheduler_targets=managed_scheduler_targets,
    ).validate()


def _scheduler_url(value: Any) -> str | None:
    """fleetqd's base URL: plain http(s), no credentials, no path beyond the root."""

    if value is None:
        return None
    text = _string(value, "scheduler.url").rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("scheduler.url must be an http(s) URL like http://127.0.0.1:8089")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ConfigError("scheduler.url takes no credentials, path, query or fragment")
    return text


def _scheduler_ui_origin(value: Any) -> str | None:
    """Accept only a canonical HTTPS origin for browser action confirmation."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError("scheduler.ui_origin must be an HTTPS origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("scheduler.ui_origin must be a canonical HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not parsed.netloc
        or "%" in host
    ):
        raise ConfigError("scheduler.ui_origin must be a canonical HTTPS origin")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigError("scheduler.ui_origin host must use canonical ASCII or IDNA") from exc
    host = host.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc += f":{port}"
    return f"https://{netloc}"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("optional string settings must be non-empty strings")
    value = value.strip()
    if len(value) > MAX_TARGET_NAME_LENGTH or any(ord(char) < 32 for char in value):
        raise ConfigError(
            "optional string settings are too long or contain control characters"
        )
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _target_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must contain non-empty strings")
    value = value.strip()
    if len(value) > MAX_TARGET_NAME_LENGTH or any(ord(char) < 32 for char in value):
        raise ConfigError(f"{name} contains an invalid target or tag")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    if not _is_finite_number(value):
        raise ConfigError(f"{name} must be finite")
    return float(value)
