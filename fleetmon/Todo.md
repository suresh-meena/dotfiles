# Fleetmon TODO and stop point

Updated 2026-09-05. **Fleetmon is deployed and running.** All P0/P1 items,
the canary gate, the fleet rollout, the hub service on numpi, and
notifications are complete. 226 tests green on Python 3.10 and 3.14.

## Deployed state (live, verified)

- **Hub**: numpi (aarch64, the always-on machine) as `fleetmon-hub.service`
  (systemd --user, lingering enabled, survives logout). Release venv under
  `~/.local/share/fleetmon`, state at `~/.local/state/fleetmon` (0700/0600).
  Dashboard bound to the Tailscale IP `100.103.185.102:8088`. Per the
  owner's simplification decision, a bind inside the mesh-VPN CGNAT range
  (100.64.0.0/10) or on loopback needs no application token — the tailnet
  itself is the authenticated boundary; any other non-loopback bind still
  requires a ≥32-character `FLEETMON_AUTH_TOKEN` (env/0600 file only).
- **Hub self-monitoring**: the hub samples its own CPU and RSS every cycle,
  reports `hub_cpu_percent`/`hub_rss_bytes`/`hub_overloaded` in hub status,
  and posts one `hub_overloaded` notification (5-minute backoff, never a
  retry-until-success loop) after two consecutive breaches of the budgets
  (5% of one core / 250 MiB RSS). Live reading: 0.4% / 48 MiB.
- **Helpers**: installed on 11 hosts (rtx2080ti, ada1, ada2,
  automation-server, lovelace1, lovelace2, rtx3090-1, rtx3090-2, rtx4090,
  sonata, wellsfargo), wheel `fleetmon-0.1.0` SHA-256 `b7eeba17…f8fc4`,
  transferred only via explicit-target `fleetctl sync push`, staged-hash
  verified, versioned user-local venvs, atomic `current` switches.
  rtx3090-1's home filesystem (`/data`, failing ext4) was bypassed by
  installing its helper under `/data2/ayand` (uv Python + venv + wheel,
  same hash-verified steps); the hub records that absolute path. Repairing
  `/data` requires root fsck on that host.
- **Scheduler**: amd-login polled via `fleetctl exec --admin`
  (`squeue --json` + bounded sacct windows in Asia/Kolkata with the
  Slurm-22.05 compatibility field set). 58–65 jobs stored, watermark advancing.
- **Notifications**: self-hosted ntfy 2.28.0 on numpi (tailnet-only bind,
  port 8090), hub posts GPU-free events (two consecutive FREE observations,
  committed samples only, stale/partial = UNKNOWN, 5 s bounded POST with
  5-minute backoff) plus the hub-overloaded event. Live `gpu_free` events
  observed flowing.
- **Dashboard**: left sidebar lists every machine with its live state token;
  clicking navigates directly to that host (no home round-trip); collapses
  to a drawer on small screens. Host pages show per-GPU process lists
  (reverse-mapped from the stored bounded allocations, ≤4 shown per GPU),
  chart group with area fills, crosshair tooltips, per-GPU colored series,
  and 1h/6h/24h/7d range switcher; the overview table carries a CPU
  sparkline per host. Refresh cadence is 15 s on overview/host/jobs
  (owner-requested near-real-time; data itself is 60 s samples) and 30 s
  for hub status and the sidebar. All rendering stays textContent-only.
- **Fleet status**: 11 helper hosts stream `partial` (bounded) snapshots;
  rtx6000 is offline and excluded by the owner; numpi is `retired`
  (bridge role, it is the hub); amd-login is the scheduler.

## Measured acceptance results (live hub, 10 hosts + scheduler)

- Hub CPU: 0.4% of one core while polling (budget < 5%) — PASS
- Hub RSS: 49 MiB (budget < 250 MiB) — PASS
- Snapshot wall time: 1.1–1.6 s per poll (budget < 2 s) — PASS
- Dashboard queries over the tailnet: 15–55 ms (budget < 250 ms) — PASS
- 30-day DB projection: < 1 GiB on 222 GB free — PASS
- Helper CPU: ~1–1.2% of one core per minute (budget 1%) — marginal,
  self-measurement artifact; observe, then reduce process detail if it holds
- Poll error rate: ~3% during startup backoffs, settling to 0

## Work completed in this session (chronological)

1. Green baseline after the interrupted workers (web escaping test, dead
   doctor block, Python 3.14 JSON-depth compat, lint/format).
2. Database contract frozen: schema v2 fail-closed, online backup,
   operational fields stored, Slurm identifier validation, EXPLAIN QUERY
   PLAN-driven indexes.
3. Hard bounds: 256-target/protocol caps, 60 s protocol-resolution budget,
   state pruning, 2048-process scan cap with UID cache, per-snapshot
   ephemeral GPU identities for unavailable UUIDs, bounded poller cleanup.
4. Helper deployment: verified fleetctl contract, reproducible wheel +
   SHA-256, fail-closed installer with explicit-target authorization flag,
   interpreter discovery (PATH + ~/.local/bin shims with ensurepip health
   check), version-dir clearing for failed installs.
5. Hub installer: lingering check, upgrade schema backup/rollback,
   permission/ownership tests.
6. Dashboard: separate GPU/user tables, units, freshness rows, SVG charts,
   hub-status expansion, hidden-tab pause, JS DOM harness.
7. Operational cleanup: strict runtime-state parsing, auth-token file,
   optional daily backup to a separate filesystem, wheel/sdist rebuild,
   fresh polling-disabled smoke.
8. Canary gate on rtx2080ti: PASSED (admission, transport, Python discovery,
   helper install, two valid polls, failure injection, kill switch, hub
   restart, API visibility).
9. Slurm live verification on amd-login: three real Slurm 22.05 defects
   found and fixed (naive local timestamps + `scheduler_timezone`,
   compat accounting field set, JSON depth bound 32).
10. `fleetmon smoke` implemented as the full ten-phase acceptance command;
    passed end-to-end on the canary.
11. Code hygiene pass: ~106 net lines of dead/duplicate code removed.
12. Full fleet rollout (10 hosts), fleetctl + fleet config + sshpass +
    numpi-owned SSH key deployed on numpi, host-key staleness cleared.
13. Hub deployed on numpi and upgraded twice through the real upgrade path
    (schema backups created before each switch).
14. Notifications implemented and verified live; ntfy installed on numpi.

## Outstanding

- rtx6000: excluded until it comes back online (standard install procedure).
- rtx3090-1: the `/data` ext4 still needs a root fsck (the helper already
  runs from `/data2/ayand`, so this is optional cleanup, not a blocker).
- Week-long observation: helper CPU budget, notification quality, disk
  growth vs the 0.9 GiB/30-day projection, hub self-monitoring headroom.
- The wheel for the next release should be rebuilt via
  `scripts/build-helper-wheel` (the recorded SHA-256 covers fleetmon-0.1.0).

## Definition of done — status

Suite green (226 on 3.10 and 3.14), builds reproducible, P0/P1 closed,
authorized canary gate passed, fleet rolled out one host at a time, real
helper rollback tested, plan matches the software. Deferred features stay
deferred except GPU-free notifications, which the owner explicitly ordered
and which follow the plan's evidence rules.
