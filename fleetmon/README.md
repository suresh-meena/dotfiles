# Fleetmon

Fleetmon is a small, read-only fleet usage monitor. One hub stores bounded
snapshots in SQLite and serves the dashboard; direct compute hosts only run
the stateless `fleetmon snapshot` helper. There are no remote Fleetmon daemons.

## Quick start

The supported runtime is Python 3.10 or newer. From this checkout:

```text
python3.10 -m venv .venv
.venv/bin/python -m pip install -e '.[hub]'
.venv/bin/fleetmon hub --once
```

`hub --once` performs one telemetry collection cycle without opening the web
listener. Collection does not change workloads or fleet inventory, but it may
contact every admitted direct and Slurm target. For a no-contact startup check,
set `[polling] enabled = false` in a temporary config. The normal hub is started
with `fleetmon hub`; `fleetmon status`, `fleetmon doctor`, and `fleetmon
maintain` are local operational commands. The NVML extra belongs only on helper
hosts that need NVIDIA metrics; the hub installer does not install it.

## Web dashboard

The default listener is `127.0.0.1:8088`. The simplest deployment binds the
dashboard to the hub's mesh-VPN (Tailscale) address, for example
`bind_host = "100.103.185.102"`: the CGNAT range 100.64.0.0/10 is treated as
the trusted encrypted-VPN boundary, so no application login is needed — every
device that can reach the address is already an authenticated tailnet member.
The same no-token rule applies to the loopback default.

Any other non-loopback bind (a LAN address, `0.0.0.0`) requires a token of at
least 32 characters before the hub starts. Browser access then uses HTTP
Basic with the fixed username `fleetmon` and that token as the password; API
clients may send the same token as a Bearer header. Put Fleetmon behind a
maintained TLS reverse proxy before exposing such a bind beyond a trusted
network.

The token never belongs in TOML, command arguments, logs, or this repository.
Supply it in one of two ways, and set only one:

- `FLEETMON_AUTH_TOKEN` in the environment, for example sourced by the
  `EnvironmentFile=-%h/.config/fleetmon/fleetmon.env` line in the systemd user
  unit from a mode-`0600` env file; or
- `FLEETMON_AUTH_TOKEN_FILE` pointing at an absolute path to a mode-`0600`,
  owner-owned file whose single line is the token. Startup fails closed if the
  file is missing, group/world accessible, or both variables are set.

Health endpoints (`/healthz`, `/readyz`) expose readiness only. API responses
are bounded and paginated (`limit` 1–500, `offset` non-negative); time ranges
must be timezone-aware and are capped at 30 days. Chart series are limited to
2,000 points. Browser rendering uses `textContent`, with no CDN or npm build
step.

## User service installation

The local hub installer creates each isolated venv at its final path under
`~/.local/share/fleetmon/releases/` and atomically switches the
`~/.local/share/fleetmon/current` symlink. This avoids broken absolute venv
shebangs during upgrades. It also places a systemd user unit at
`~/.config/systemd/user/fleetmon-hub.service`. It never uses root and does not
enable or start a service implicitly:

```text
./scripts/install-hub --dry-run
./scripts/install-hub
systemctl --user daemon-reload
systemctl --user enable --now fleetmon-hub.service
```

Review the unit before enabling it. To explicitly replace an existing local
installation, use `./scripts/install-hub --replace`; old releases remain under
`releases/` until you remove them deliberately. Application data lives
separately under `~/.local/state/fleetmon`.

Non-interactive service startup requires a working systemd user manager. If it
is not available, the installer stops before creating files. The unit uses the
loopback default and bounded restart behavior; it does not depend on an
interactive shell or SSH agent. It is `Type=simple`, which provides no systemd
readiness notification; application-level readiness is exposed at `/readyz`
after inventory, DB migration, and the web listener have started.

## Helper installation

`./scripts/install-helper --dry-run TARGET` performs local fleetctl admission
checks and prints the intended one-target operation. It never contacts or
changes the target. The real helper installer intentionally exits with a
clear error for now: a released/reproducible helper artifact, remote Python
selection, canary validation, and atomic rollback contract still need to be
implemented and tested against the installed `fleetctl` interface.

This fail-closed behavior is deliberate. No command in Fleetmon uses raw
`ssh`/`scp`, root, system Python, a remote service, or an implicit all-host
selector for helper deployment.

## Configuration

For a responsive dashboard with bounded helper overhead, use a two-second live poll and keep the slower history/scheduler work at one-minute intervals:

```toml
[polling]
interval_seconds = 2
scheduler_interval_seconds = 60
history_interval_seconds = 60
```

The dashboard refreshes live host and GPU cards every two seconds, pauses while its tab is hidden, and keeps the last two readings plus one sample per minute for chart history. The **Idle GPUs** page uses the same freshness rules as alerts, so stale or unavailable readings are shown as unknown rather than advertised as free.

Helpers cap process inspection and reuse the username cache, which keeps peak RSS bounded on compute nodes while retaining the top memory consumers needed for triage.

Non-secret configuration is read from
`~/.config/fleetmon/config.toml` (or `FLEETMON_CONFIG`). Defaults are safe for a
single-user hub: loopback binding, 60-second polling, at most two concurrent
remote polls, bounded output, and a 30-day retention window. Invalid values,
unknown keys, relative executable paths, and unsafe resource limits fail
closed. The authentication token is environment- or credential-file-only (see
the dashboard section above); it must not be placed in TOML or command
arguments.

Setting `[hub] backup_dir` enables one daily SQLite online backup to that
directory. It must be an absolute path on a different filesystem than
`state_dir`; the hub fails to start otherwise. The seven newest `fleet-*.db`
backups are kept, and `/api/hub-status` reports `backup_ok`, `backup_error`,
or `backup_pending`. When unset, hub status keeps reporting `backup_disabled`.

Scheduler targets (login nodes) are polled with `squeue`/`sacct` through
`fleetctl exec --admin` and never receive the helper. `[polling]
scheduler_timezone` (default `UTC`) must be set to the login node's timezone
whenever it is not UTC: Slurm 22.05 `sacct` only accepts naive local
timestamps, so the hub converts its UTC watermark windows into that zone
before querying. Older schedulers that reject the full accounting field set
fall back once to a bounded Slurm-22.05-compatible field set.

## Notifications

Set `FLEETMON_NOTIFY_URL` (environment or the 0600 `fleetmon.env` file, never
TOML) to an HTTP(S) topic endpoint such as a self-hosted ntfy server, and the
hub posts one JSON message per GPU-free transition. A GPU is free only after
two consecutive valid observations with zero utilization and zero compute
processes, computed from samples the hub has already stored — notifications
add zero remote polls. Stale hosts, NVML errors, and missing GPU data are
UNKNOWN: they never alert and they reset the consecutive chain. Delivery is
bounded (5 s timeout, one POST per transition); failures back off five
minutes instead of retrying until success. A companion ntfy server on the
hub host, bound to the tailnet address, keeps the whole channel private.

The hub discovers the managed inventory with `fleetctl list --json`, admits
only enabled workstation/compute targets using the direct protocol, and treats
unsupported Python, missing helpers, unreachable hosts, partial snapshots, and
data gaps as explicit states.
