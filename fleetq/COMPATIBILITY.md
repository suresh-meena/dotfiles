# fleetq compatibility and evidence matrix

The entries below describe the implemented interface or required capability.
They do not claim that a particular host or university site has passed a
canary. Record host/site versions and probe results in each target's approved
evidence file before setting `enabled = true`.

| Component | Implemented contract | Deployment evidence |
|---|---|---|
| Python | 3.11+ for daemon, client, and node shim. The daemon installs pinned, hash-checked dependencies. | Record exact interpreter version and a successful locked installation on numpi and each shim target. |
| SQLite | WAL with `synchronous=FULL`; startup requires a version with the WAL-reset fix or an explicit distribution backport attestation. | Record linked `sqlite3.sqlite_version`, storage filesystem UUID, local SSD properties, backup/restore result, and advisory if attested. |
| Workstation service | systemd user units with linger, cgroup v2 memory control, a working user bus, and durable local control storage. | Run the disposable unit, logout, memory/OOM, cancellation, cgroup-empty, and reboot/restore checks on an approved target. |
| NVIDIA GPU | Whole GPU UUID allocation and bounded `nvidia-smi` snapshots; shared placement additionally needs complete process visibility and a measured per-GPU baseline. | Record driver, GPU UUID/PCI map, process namespace visibility, MIG/MPS/compute mode, baseline rationale, and post-launch contention observations. |
| Slurm | One-shot `squeue --json` with the established fixed `--parsable2 --Format` fallback; bounded `sbatch --parsable`, exact-ID cancellation, and optional accounting. No resident helper on login nodes. | Record Slurm version, command fields, controller identity, account/partition/QOS/GRES mapping, no-requeue behavior, query visibility, durable compute-visible control root, and approved RPC/session/byte budget. |
| fleetctl | `fleetctl.result/v1` subprocess envelope and centrally redeemed managed-cluster permits. | Use the matching fleetctl release and test host keys, aliases, credentials, route, permit authority, and timeout/uncertainty behavior. |
| fleetmon | `fleetmon.capacity/v1` workstation feed and `fleetq.managed-slurm/v1` cached queue handoff. | Configure each managed inventory target exactly once, verify freshness/stale display, and confirm no second direct Slurm poller. |
| Browser | Human-token UI behind one canonical tailnet HTTPS origin. | Verify Serve/Host/Origin behavior, session cookie, CSRF, and browser confirmation flow in the actual deployment. |

Production site approval, a disposable local systemd/Slurm exercise, and
user-initiated canaries remain separate from repository tests. See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) and the phase gates in
[REFINED_PLAN.md](REFINED_PLAN.md#12-delivery-phases-and-release-gates).
