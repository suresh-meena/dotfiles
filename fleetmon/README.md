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

As an explicit alternative to the token, `hub.trusted_networks` names CIDR
ranges that are trusted boundaries in their own right, exactly like the
mesh-VPN range above: a bind inside one of them (or a wildcard bind when any
range is configured) needs no token. This exists for fleets whose dashboard
must open without a login on an already-secured lab LAN; listing a range
there is an operator statement that the network itself authenticates every
device on it. Unknown or malformed entries fail startup.

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

The hub runs local writes and maintenance on one worker, with two separate
workers for dashboard queries. Both queues are bounded, including cancelled
requests. Chart ranges sample across the selected window and apply the point
limit to each GPU. Retention also expires scheduler records that have not been
updated within the configured window; jobs still reported by the scheduler
remain current.

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

For clusters whose queue observation is owned by fleetqd, list the matching
`fleetctl` inventory target names explicitly. Fleetmon then stops its own
`squeue`/`sacct` schedule for those targets. fleetqd's existing queue and node
APIs show fleetq-managed jobs, nodes, and allocations. The read-only
`/api/v1/managed-slurm` feed supplies bounded all-user site snapshots when the
fleetqd observer has one; Fleetmon rejects incomplete, invalid, or over-age
sites and shows stale/unavailable states instead of presenting retained rows
as fresh. Old direct-poller state is marked stale during handoff.

```toml
[scheduler]
url = "http://127.0.0.1:8089"
managed_targets = ["uni-cluster-login"]
ui_origin = "https://fleetqd.example.tailnet"
```

`ui_origin` is optional. It must be the same canonical HTTPS origin configured
as fleetqd's `[daemon].ui_origin`; Fleetmon uses it only to link to explicit
fleetqd confirmation pages.

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

When `[scheduler].url` is configured, the existing scheduler cadence also
detects a stopped fleetqd. Managed sites report a stale snapshot; with no
managed sites, Fleetmon checks fleetqd's `/healthz` and sends one deduplicated
`fleetqd_unavailable` notification until the service recovers. This makes no
Slurm call and requires `FLEETMON_NOTIFY_URL` for delivery.

The hub discovers the managed inventory with `fleetctl list --json`, admits
only enabled workstation/compute targets using the direct protocol, and treats
unsupported Python, missing helpers, unreachable hosts, partial snapshots, and
data gaps as explicit states.

## Fleet queue (fleetq)

Fleetmon and fleetqd, the fleet's job scheduler, exchange data in both
directions, and neither direction can change anything:

- **Capacity feed, fleetmon → fleetq.** `GET /api/feed/v1/capacity` serves
  recent raw per-GPU samples (`contracts/fleetmon-capacity-v1.schema.json`)
  from stored and just-received polls only; no request triggers a remote call.
  fleetq uses it to decide when a GPU on a *shared* workstation has been idle
  long enough to use. Point fleetqd at it with
  `fleetmon_feed_url = "http://127.0.0.1:8088/api/feed/v1/capacity"` under
  `[daemon]`. Its idle rule needs samples no more than about 65 s apart, so
  poll shared hosts every 30 s or faster (`polling.interval_seconds`).
- **Queue view, fleetq → fleetmon.** The Queue page lists running and waiting
  jobs in dispatch order, with each job's reason, output tail and "why it is
  waiting"; Idle GPUs link the fleetq job holding a GPU; Hub Status shows the
  scheduler's per-cluster call counts. For targets listed in
  `scheduler.managed_targets`, it also shows the cached all-user Slurm queue
  snapshot and its per-site age/state; the Jobs page labels persisted rows
  stale after incomplete or old observations. Optionally set
  `scheduler.ui_origin` to the canonical HTTPS origin configured as fleetqd's
  `[daemon].ui_origin`; Queue links then open fleetqd's cancel/hold confirmation
  pages in a new tab. The service token remains read-only and Fleetmon never
  submits a mutation. Enable the integration with

  ```toml
  [scheduler]
  url = "http://127.0.0.1:8089"
  ```

  and a read-only token, created on the scheduler host and given to fleetmon
  through its environment file (never TOML):

  ```bash
  fleetqd token create --owner fleetmon --kind service --label fleetmon \
      --scopes read,read_all --out ~/.config/fleetmon/scheduler.token
  # fleetmon.env:  FLEETMON_SCHEDULER_TOKEN_FILE=/home/<you>/.config/fleetmon/scheduler.token
  ```

  Reads are cached for 2 s with a 2 s timeout; a scheduler that is down, slow,
  or refuses the token greys the page with the reason. Jobs are changed with
  `fq` (cancel, hold, modify, top), not from the dashboard.
- **Managed nodes, fleetq → fleetmon.** The read-only Nodes page uses
  fleetqd's cached `/api/v1/nodes` response to show fleetq node state and GPU
  reservations. It does not show all Slurm nodes or other users' allocations.
