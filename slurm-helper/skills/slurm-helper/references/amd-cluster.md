# AMD GPU cluster (MI210) site audit

Source: the supplied **AMD Server.pdf** ("AMD GPU Cluster", 5 pages; kept in
the project root and excluded from git because it lists private IP
addresses). Self-contained here; private connection details are omitted.
Everything below is `documented` unless marked otherwise — this manual has
never been live-verified through the skill, so `--live` on the cluster is
the authority. Cross-check: the fleet skill's
`remote-fleet-operator/references/amd-gpu-cluster.md` derives from the same
PDF.

## Hardware and software (documented)

| Component | Facts |
| --- | --- |
| Master/login node mn01 | AMD EPYC 9654, 96 cores ("Processor: 192" — undefined field), 1.5 TB RAM, 7 TB ×2 |
| Storage node sn01 | EPYC 7773X, 49 TB; its "504 TB system memory" row is inconsistent — do not use as a limit |
| GPU nodes gn01–gn03 | EPYC 9654 each, **4 × AMD Instinct MI210** (12 cluster-wide), 1.5 TB RAM, 96 cores |
| Software | Ubuntu 22.04, ROCk drivers 6.0.6, **Slurm 22.05.8**, LDAP auth on all nodes |

Slurm 22.05.8 predates the `--json` data_parser output: `--live` discovery
falls back to `scontrol show partitions/nodes` there — expected behavior,
not an error.

## Access and storage (documented)

- CSA students/faculty only; accounts by advisor approval (email to the
  admin, CC advisor, with a validity period).
- SSH from within the campus network; login node mn01.
- **Home: 20 GB** per user, under `/rhome/<username>` (paths in the
  manual's third example confirm the prefix).
- **`/scratch/<username>`: temporary, cleaned WEEKLY.** Use it for bulky
  working data, but copy anything you want to keep out of it first —
  treat "weekly purge" as a hard retention bound, not a suggestion.

## Policies (documented, enforced by the checker)

- **Policy A — Slurm only.** No jobs outside Slurm; violators have their
  accounts blocked immediately. (Same shape as KIAC's rule; the checker
  never runs workloads locally.)
- **Policy B — GPU queues are for GPU jobs.** The site states scripts
  monitor GPU queues for CPU-only jobs. Checker rule **AMD070** errors on
  a GPU-partition script that requests no GPU.
- **AMD071**: `nvidia-smi` on this site is always wrong (MI210 is AMD
  hardware; the manual's own Example 1 runs `nvidia-smi`). Use
  `rocm-smi` / `rocminfo`.

## The manual's examples and their defects

| Example | Request | Defects |
| --- | --- | --- |
| 1 — "1 node GPU job" | `--partition=jobgn01 --gres=gpu:1 --time=00:05:00 --ntasks=1` | runs `nvidia-smi` on MI210; `#!/bin/sh`; output pattern `test_job%j.out` (valid, but no separator) |
| 2 — "Multiple node GPU job" | `--partition=GPU --gres=gpu:4 --ntasks=1` | comment says "eight CPU" while requesting no CPUs; labeled multi-node but nothing requests multiple nodes |
| 3 — Python app | `--partition=GPU --cpus-per-task=48 --gres=gpu:1 --mem=256G` | **no `--time` at all** (on a 5-min-example manual, unset time is a trap), no output/error directives, `--mem=256G` vs 1.5 TB node RAM is fine but unvalidated, conda boilerplate with personal `/rhome` paths |
| Commands list | | `scancle` is a typo for `scancel` (use `scancel`; `inspect` prints the correct hint) |

## Explicit unknowns (resolve with `--live`, then `learn apply`)

- The real partition table: `GPU` and `jobgn01` appear **only in examples**
  (status `example-only` in `config/amd.yaml`); names may differ in case
  or kind, and no time limits, node ranges, or CPU/memory caps are
  documented anywhere.
- Actual GRES type strings (untyped `gpu` vs typed `mi210`) — discover live.
- Whether any accounts/QOS exist (LDAP auth suggests plain users, but
  Slurm associations were never queried).
- The CPU partition situation: Policy B implies GPU queues shouldn't host
  CPU jobs, but no CPU queue is documented — ask the admins or discover.
- Module/software stack beyond ROCk (conda lives in user `/rhome`; nothing
  cluster-provided is documented).

## Operational notes

- Storage choice is a trade-off: 20 GB home vs weekly-purged `/scratch`.
  The checker's AMD040 messages carry both facts; recommend `/scratch` for
  working data + explicit copy-out of results, home for code and small
  configs.
- First actions on this cluster should be `kiac-slurm --site amd doctor`,
  `resources --live`, then `learn apply --yes` so the real partition table
  and GRES strings replace the example-only seeds.
