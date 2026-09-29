# fleetq

`fleetqd` is the single scheduler on numpi; `fq` is its Python 3.11+ client.
The daemon owns job state and calls `fleetctl` as its only remote transport.
Workstation jobs use a per-attempt `fq-node` runner. Slurm sites receive a
job-scoped batch wrapper, not a resident fleetq daemon. The design and release
gates are in [REFINED_PLAN.md](REFINED_PLAN.md).

## Build and local setup

Run `python3 scripts/build.py` in this directory to regenerate the stdlib-only
`bin/fq` and `build/fq-node` artifacts. `scripts/install-daemon --dry-run`
shows the release paths. Installation creates an immutable release and user
unit, but does not start it. Python's linked SQLite must carry the WAL-reset
fix, or its distribution backport must be recorded in the daemon config.
The installer uses the uv-generated `requirements-daemon.lock` with pip's
`--require-hashes` and does not resolve unpinned runtime packages.

The config is `~/.config/fleetq/fleetqd.toml` by default:

```toml
[daemon]
state_dir = "/home/YOU/.local/state/fleetq"
fleetctl = "/home/YOU/.local/bin/fleetctl"
bind = "127.0.0.1:8089"
# Production requires an explicit, supported host clock provider.
clock_provider = "systemd-timesyncd"
fleetmon_feed_url = "http://127.0.0.1:8088/api/feed/v1/capacity"
# Set only after configuring a canonical tailnet HTTPS Serve origin.
# ui_origin = "https://numpi.YOUR-TAILNET.ts.net"

[[node]]
id = "example-workstation"
backend = "bare"
mode = "exclusive"
enabled = false
control_root = "/approved/local/fleetq-root"
# Optional: use an absolute Python executable when the host's python3 is too old.
# node_python = "/data/home/USER/.local/share/uv/python/.../bin/python3.12"
```

Record the state filesystem's UUID from
`findmnt -no UUID -T /home/YOU/.local/state/fleetq` as `state_fs_uuid` under
`[daemon]` first. `fleetqd --config CONFIG init` then verifies the filesystem,
creates a volume sentinel, and prints the `volume_id` to put under `[daemon]`.
Keep the state directory on the approved SSD. Production startup
checks both identities, ownership, permissions, and the database path.
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

Every target starts disabled. A production config with `enabled = true` must
name an absolute `evidence_file`. The record binds a retained raw capture to
the exact target configuration, reviewer, approval time, and required checks.
For a draft, save the bounded probe/profile output in a private directory and
run `scripts/draft-target-evidence CONFIG TARGET CAPTURE_FILE > RECORD.json`.
Review each check, fill `command_profile_version`, `reviewer`, and
`approved_at`, then set `approved` and verified checks to `true`. Place the
record beside its raw capture and set `evidence_file` in the node config.
Changing the target profile invalidates the approval digest. A draft never
enables dispatch by itself.
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
Its evidence must include site automation and budget approval, account/QOS,
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
enabled = false # enable only with retained, reviewed evidence
evidence_file = "/private/path/site-evidence.json"
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

Fleetmon supplies the read-only capacity feed. Fleetqd supplies cached queue,
node, and status data; reading those APIs does not contact a node. Shared GPU
dispatch waits for independent idle samples and an immediate node gate. A
managed Slurm target must have one budgeted queue observer on fleetqd; do not
enable duplicate direct polling in fleetmon.

## Release checks

The code's presence is not a phase exit. Follow the evidence table in
[REFINED_PLAN.md](REFINED_PLAN.md#12-delivery-phases-and-release-gates): offline
crash and security checks, a disposable local systemd and Slurm environment,
then an approved workstation canary and a separately initiated site job.
Record results with versions, target, time, and digest in the target evidence.
