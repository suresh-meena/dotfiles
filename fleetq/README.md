# fleetq

`fleetqd` is the single scheduler on numpi; `fq` is its Python 3.11+ client.
The daemon owns job state and calls `fleetctl` as its only remote transport.
Workstation jobs use a per-attempt `fq-node` runner. Slurm sites receive a
job-scoped batch wrapper, not a resident fleetq daemon. The design and release
gates are in [REFINED_PLAN.md](REFINED_PLAN.md).

## Build and local setup

Run `python3 scripts/build.py` in this directory to regenerate the stdlib-only
`bin/fq` and `build/fq-node` artifacts; `python3 scripts/build.py --check`
fails if they are stale. `scripts/install-daemon --dry-run`
shows the release paths. Installation creates an immutable release and user
unit, but does not start it. Python's linked SQLite must carry the WAL-reset
fix, or its distribution backport must be recorded in the daemon config.
The installer uses the uv-generated `requirements-daemon.lock` with pip's
`--require-hashes` and does not resolve unpinned runtime packages.

`scripts/test` runs the suite with no network route. `ruff check .` looks for
unused imports and variables, likely bugs, and blocking calls in async code;
the rules are in `pyproject.toml`. From the repository root, `ruff check
fleetmon fleetq shared/fleet-dotfiles/bin/fleetctl shared/fleet-dotfiles/tests`
checks all three fleet tools with their own configs.

The config is `~/.config/fleetq/fleetqd.toml` by default:

```toml
[daemon]
state_dir = "/home/YOU/.local/state/fleetq"
fleetctl = "/home/YOU/.local/bin/fleetctl"
bind = "127.0.0.1:8089"
# Production requires an explicit, supported host clock provider.
clock_provider = "systemd-timesyncd"
fleetmon_feed_url = "http://127.0.0.1:8088/api/feed/v1/capacity"

[[node]]
id = "example-workstation"
backend = "bare"
mode = "exclusive"
enabled = false
control_root = "/approved/local/fleetq-root"
# Optional: use an absolute Python executable when the host's python3 is too old.
# node_python = "/data/home/USER/.local/share/uv/python/.../bin/python3.12"
```

`fleetqd --config CONFIG init` creates the owner-only state directory and an
init marker in it. The service refuses to start, and never creates a database,
where that marker is missing. Production startup also checks ownership,
permissions, and the database path.
`fleetqd --config CONFIG doctor --json` checks the local prerequisites.
The user service runs `doctor` before every start. Backups use the online
SQLite backup command, `fleetqd --config CONFIG backup DEST`. The destination
parent must already exist, and the command refuses an existing destination or
symlink rather than replacing it.

Production startup requires `[daemon] clock_provider`; `chrony` selects the
Chrony health probe, and `systemd-timesyncd` requires the service to be active
and `timedatectl` to report `NTPSynchronized=yes`. Startup rejects a missing or
unsupported provider. The cached probe fails closed when its sample is missing,
stale, or unhealthy. While clock health is bad, fleetq blocks new dispatch and
expiring-token authentication; cancellation and recovery operations remain
available. Configure and validate the chosen provider on the deployment host
before production use.
See [clock recovery](OPERATIONS.md#clock-provider-health-and-recovery).

After replacing the database with a backup, the first daemon start must use
`fleetqd --config CONFIG serve --restored-from-backup`. This persists the global
admission and dispatch gate, discovers and fences enabled or previously active
approved targets, raises the controller epoch above any target fence it finds,
and keeps new admission and dispatch blocked while a target is unreachable or
quarantined. Review
the recovery inventory and `/readyz` before resuming. The systemd drop-in
procedure for this first start is in [OPERATIONS.md](OPERATIONS.md#state-volume-or-database-failure-and-restore).

For an upgrade, use an administrator token to run `fq admin accept-off` and
`fq admin pause`, then let in-flight mutation calls reach a recorded boundary
and stop `fleetq.service`. `scripts/install-daemon` refuses to switch a release
while that service is active; it checks the new release locally and takes a
verified SQLite backup before switching. Restart the service, inspect
`/readyz` and per-target reconciliation, then run `fq admin resume` and
`fq admin accept-on`. A paused controller still observes and cancels work.

## Enabling a target

Every target starts disabled. Set `enabled = true` in its `[[node]]` to turn it
on; run `fleetqd node probe NAME` first and read the capability facts it records.
An enabled Slurm site must carry a complete budget (below).
Before production runtime opens the state database, it also checks the local
fleetctl topology: a bare node must name an enabled workstation target, and a
Slurm site must name an enabled login target. Enabled nodes cannot share one
fleetctl target. `dev_mode` cannot serve enabled remote nodes.
Every fleetq exec and file transfer also pins the expected inventory role in
the fleetctl call, so a later inventory edit cannot silently reroute a bare
operation to a compute node or a Slurm operation to a workstation.

Workstation onboarding is explicit: build the shim, then run
`fleetqd --config CONFIG node install NAME --i-authorize-target-NAME` for one
approved machine. `fleetqd node probe NAME` records capability facts; inspect
the verdict before approval. Cluster onboarding does not install a shim.
Before enabling one, confirm site automation and budget approval, account/QOS,
no-requeue, controller identity, durable control storage, query/cancel behavior,
and visibility. University production calls require the site's own approval.

An enabled managed Slurm node must define all three class rates, a class
`burst` of at least 8 (fleetctl's bounded exec envelope), and explicit session
and byte dimensions. Replace every example value with site-owner-approved
limits; these placeholders are not a recommended policy. The byte ceiling and
burst must cover the largest authorized transfer, and the session reserve must
cover one full action operation.

```toml
[controller]
collect_max_bytes = 16777216 # match the site's approved artifact policy
collect_max_files = 1000

[[node]]
id = "approved-managed-site"
backend = "slurm"
enabled = false # enable only after the site approves automation
fleetctl_target = "approved-fleetctl-alias"
[node.budget]
monitor_per_minute = 1 # replace with approved RPC allowance
action_per_minute = 1
transfer_per_minute = 1
burst = 8
max_sessions_per_operation = 2
max_bytes_per_transfer = 67108864 # replace with approved transfer ceiling
sessions_per_minute = 2
sessions_burst = 4
action_session_reserve = 2
bytes_per_minute = 67108864
bytes_burst = 67108864
```

The `action_session_reserve` is held back from monitor and transfer permits;
action permits may consume it. Fleetqd rejects enabled site policies whose
dimension rates, burst limits, per-operation caps, or reserve are missing or
inconsistent.
For an enabled Slurm site, the transfer ceiling and byte burst must also
cover `collect_max_bytes` plus the bounded per-file transfer overhead.

## Clients and monitoring

Create a token locally on numpi with `fleetqd token create --owner NAME
--kind human --label LABEL --out FILE`. Agents use `--kind agent`; cluster
permission is separate and defaults off. Put the token in a private 0600 file
and set `FQ_TOKEN_FILE` and `FQ_URL` for `fq`. The `fq whoami` command confirms
which owner and scopes the client will use. `fq --json submit` requires a
stable idempotency key for retryable client calls.
`FQ_URL` must be HTTPS; plaintext HTTP is accepted only on literal loopback
(`127.0.0.1` or `::1`) for a local proxy or test daemon.
`fq fetch` verifies every artifact's recorded SHA-256 and refuses to replace
an existing destination by default, including a symlink at that path. Pass
`--overwrite` only when replacing existing files is intentional.

Install the client on any machine with Python 3.11+:

```bash
install -m 0755 bin/fq ~/.local/bin/fq
```

`fq --help` lists every command with the exit-code contract, environment and
examples; `fq COMMAND --help` documents each option. `--json` may go before
or after the command, and the job's own command always follows a bare `--`.

Fleetmon supplies the read-only capacity feed. Fleetqd supplies cached queue,
node, and status data; reading those APIs does not contact a node. Shared GPU
dispatch waits for independent idle samples and an immediate node gate. A
managed Slurm target must have one budgeted queue observer on fleetqd; do not
enable duplicate direct polling in fleetmon.

Read cached GPU utilization, VRAM, CPU and RAM from any client:

```bash
fq usage                 # all machines in Fleetmon's monitored inventory
fq usage rtx4090         # one machine, by its Fleetmon/fleetctl target name
fq usage --idle          # only GPUs Fleetmon proves idle right now
fq --json usage rtx4090  # fq.usage/v1; fractions 0–1 and memory in bytes
```

The table lists every GPU and reports used/total memory in GiB and sample age.
Missing measurements remain unknown (`-`); stale or unreachable hosts retain
their last measurements with an explicit state. These are measured usage
values, separate from the fleetq reservations shown by `fq nodes`. The FREE
column and `--idle` use Fleetmon's own verdict (two fresh, consecutive idle
samples, the rule behind its Idle GPUs page); a GPU that is not proven idle or
busy shows `?`, and a stale host never shows idle. `--idle` refuses (exit 7)
when Fleetmon or fleetqd is too old to send verdicts, rather than reporting
that no GPU is free. A machine whose GPUs Fleetmon cannot read (NVML failed,
for example after a driver update without a reboot) shows `unreadable` and a
note with the reason, also under `--idle`, so its GPUs are not silently
missing from the counts. Slurm login
targets do not provide compute-node GPU telemetry through this feed.

Configure the existing `[daemon] fleetmon_feed_url` capacity URL on numpi and
upgrade both Fleetmon and fleetqd. Fleetqd derives `/api/feed/v1/usage` from
that URL and refreshes its in-memory telemetry cache every 10 seconds,
independently of scheduling's capacity feed. `/api/v1/usage[?machine=NAME]`
requires the `read` scope and only reads this cache: CLI/API requests never
contact a fleet machine or trigger collection. Fleetmon's collection cadence
is unchanged. A source outage or a feed older than 30 seconds marks cached
measurements stale; individual samples also expire using Fleetmon's freshness
limit. Restarting fleetqd empties the cache until its first valid feed arrives.
An unconfigured feed or empty startup cache returns an explicit error.

## Release checks

The code's presence is not a phase exit. Follow the evidence table in
[REFINED_PLAN.md](REFINED_PLAN.md#12-delivery-phases-and-release-gates): offline
crash and security checks, a disposable local systemd and Slurm environment,
then an approved workstation canary and a separately initiated site job.
Record results with versions, target, time, and digest in the target evidence.
