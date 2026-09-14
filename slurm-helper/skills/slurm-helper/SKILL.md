---
name: slurm-helper
description: >
  Create, validate, submit, and debug Slurm batch scripts on the KIAC GPU
  cluster with a bundled CLI (kiac-slurm / slurm-check) that generates
  templates, lints #SBATCH directives, and preflights against the live
  scheduler. Use when writing or fixing Slurm scripts, submitting or
  inspecting jobs, choosing partitions/GPUs/accounts, or interpreting
  pending/failed job states on KIAC. Not for the AMD MI210 cluster or
  generic fleet/SSH work (remote-fleet-operator).
---

# Slurm Helper (KIAC)

Generate and validate Slurm batch scripts for the KIAC cluster. The bundled
CLI lives in this skill's directory; run it via `bin/slurm-check` and
`bin/kiac-slurm`, or install it (`pip install <skill-dir>`, which puts
`kiac-slurm` on PATH). All examples below use `kiac-slurm`; substitute
`python3 -m kiac_slurm` with the skill's `src/` on `PYTHONPATH` if neither
entry point is on PATH.

## Source-of-truth model (read this first)

The KIAC user manual contradicts itself (node counts, `short`/`long` time
limits, storage quotas, GPU lists) and some of its examples are wrong
(`--partition=general` does not exist; `--mem=16GB` is not valid syntax).
Never copy manual values into scripts. Resolve claims by origin, newest
evidence first:

1. **Fresh live query** — partitions/nodes/GRES/MaxTime/accounts read by the
   CLI from the running scheduler this session.
2. **Dated verified facts** (`config/kiac.yaml`, `verified_live` as of
   2026-09-14) — GPU types per partition and the account/QOS matrix confirmed
   by real jobs on the cluster. Re-verify after cluster changes; a fresh live
   query supersedes them within a run.
3. **Slurm syntax** — SchedMD semantics; the checker encodes these offline.
4. **Site policy** (login node is for submission only, prefer `/storage`,
   H200 needs an account) — the manual, applied as rules.

Label every site fact in answers as `verified-live`, `documented`,
`document-conflict`, or `inferred`. See
[references/kiac-manual-audit.md](references/kiac-manual-audit.md) for the
documented conflicts and what the 2026-09-14 verification resolved.

## Verified cluster facts (as of 2026-09-14, user sureshmeena)

- **Partitions and GPU types**: `long`→A6000, `short`→A5000,
  `medium`→A5000/A6000/ADA6000, `ada`→ADA6000, `a100`→A100 (node cn6),
  `h200`→H200 (node cn10).
- **Accounts**: this user holds `research` and `chiru` (not `freerun`).
- **Account/partition policy**: `a100` allows `research` only — `chiru`
  passes `sbatch --test-only` and then **stays pending** ("Job's account not
  permitted to use this partition (a100 allows research,freerun_not_chiru)").
  `h200` requires `--account=chiru --qos=h200_qos`; with the default `normal`
  QOS the job pends ("Job's QOS not permitted to use this partition (h200
  allows h200_qos not normal)").
- The checker enforces this matrix offline (KIAC023/KIAC024) because
  `--test-only` does not.

**Critical caveat**: `sbatch --test-only` validates syntax and allocation
shape but does **not** enforce account-partition or QOS policy. A green
`--live` check is necessary, not sufficient: validate any newly granted
account/partition/QOS combo with a real 5-minute smoke job before relying on
it (`sbatch --account=... --partition=... --gres=gpu:1 --time=00:05:00
--wrap='hostname; nvidia-smi -L; sleep 5'`).

## Hard rules

- Never invent a partition, GPU GRES type, account, QOS, module version,
  memory limit, storage quota, or node mapping. Discover or refuse.
- H200 jobs need `--partition=h200`, a group `--account`, and (verified)
  `--qos=h200_qos` — KIAC020/KIAC024 are hard errors. `new -t h200` fills
  the QOS automatically.
- On `a100`, only `research` is permitted; `chiru`+`a100` passes
  `--test-only` and then pends forever (KIAC023).
- Memory units are K/M/G/T: write `--mem=16G`, never the manual's `16GB`.
- All `#SBATCH` directives go before the first executable line; later ones
  are silently ignored by sbatch.
- Run workloads only through Slurm (`sbatch`/`srun`), never on the login
  node. Cancel with `scancel <jobid>`, never `kill <jobid>`.
- `check` never submits. `submit` runs the same checker first and needs
  explicit user intent (`--yes`).
- Do not assume `/storage` is writable (or that system Python has your
  libraries — compute nodes lack `uv` and Torch in the system interpreter;
  use the experiment virtualenv, and install `uv` in the user account if a
  launcher needs it).

## Task workflows

**New script.** `kiac-slurm new -t {cpu,gpu,h200,multi_gpu,array}` with
`--partition --time --cpus --mem --gres --account --module --command ...`.
The generator refuses to guess a partition or the H200 account; supply them
from `resources`/`account` output, not memory. It self-checks the generated
file and prints provenance notes. Templates accept `%j`/`%x`/`%A`/`%a`
filename patterns; arrays must keep `%a` in output names.

**Validate (the decisive preflight).** `slurm-check job.sbatch` runs the
full offline lint (shell syntax via `bash -n` + optional shellcheck,
directive semantics, KIAC policy incl. the verified account matrix,
filesystem and module checks). Add `--live` on the login node to query
partitions/GRES/accounts and finish with `sbatch --test-only` — a
scheduler-side validation that submits nothing but **cannot be trusted for
account/QOS policy** (see the caveat above; LIVE002 reminds you).
`--strict` escalates warnings; `--json` gives stable editor/CI diagnostics.
Interpret levels: ERROR blocks submission, WARN needs a decision, PASS may
carry a confidence label. `--test-only` also cannot catch application
failures (bad imports, dataset errors, CUDA faults) — only execution can.

**Fixing an existing script.** Check first, apply the fixes the diagnostics
suggest, re-check, and only then talk about submitting. Filesystem findings
(`FS*`) are host-relative: a path error found off-cluster may simply mean
the script belongs on the login node — re-check there before treating it as
a script bug. When the requested walltime exceeds the partition's MaxTime,
the fix is checkpoint-and-resubmit (or another partition), never a longer
request.

**Site facts.** `kiac-slurm resources` prints live partitions with
MaxTime/nodes/GRES (falls back to the documented table plus the verified
GPU/account matrix, disputes marked); `kiac-slurm doctor` checks tools,
cluster reachability, accounts+QOS, module system, and storage;
`kiac-slurm account` lists your Slurm associations with QOS and the
verified account/partition policy. Run `resources`/`doctor` before
site-specific decisions unless cluster state was fetched this session (it is
cached ~5 min; `--no-cache` forces a refresh).

**Interactive debugging.** `kiac-slurm interactive --partition <p>
--gres gpu:1 ...` constructs an `srun --pty bash` (or `--salloc`) command
and prints it by default; `--execute` runs it. For H200 it requires
`--account` and adds the verified `--qos=h200_qos` automatically.

**Job inspection.** `kiac-slurm inspect <jobid>` explains pending
(the scheduler's `Reason`, not guesses), running, or finished jobs via
`squeue`/`scontrol`/`sacct`, with hints for OUT_OF_MEMORY, TIMEOUT,
account/QOS permission errors, etc. Pending forever with no reason shown →
suspect the account/partition policy above even if `--test-only` passed.

## Self-learning loop

The skill records evidence and turns it into config, with a human approving
the final write:

- **Automatic**: every `--live` run diffs the scheduler against
  `config/kiac.yaml` and appends observations (new partitions, changed
  MaxTimes — marking which documented dispute they resolve — changed GRES
  types) to `var/observations.jsonl`. `inspect` on a pending job also
  parses the scheduler's reason: an `(a100 allows research,...)` rejection
  or a QOS error becomes account-policy evidence — the exact signal that
  exposed the `--test-only` blind spot. Failed `submit`s are logged too.
  Recording never breaks a preflight; disable with `KIAC_SLURM_LEARN=off`.
- **Review**: `kiac-slurm learn log` shows deduplicated evidence with
  counts and sources; `learn note "<text>"` records manual findings
  (checker false positives, quota facts, ops quirks).
- **Apply**: `kiac-slurm learn apply` merges the log + a fresh live sweep
  into a new `verified_live` section, prints a unified diff, and only
  rewrites the marker-bounded block in `kiac.yaml` with `--yes` (backup at
  `kiac.yaml.bak`). Applied facts immediately power offline checks — e.g.
  a learned MaxTime makes disputed `short`/`long` limits enforceable
  offline (KIAC033).

After applying, commit `config/kiac.yaml` so every machine inherits the
learning; `var/` (the raw log) is machine-local and gitignored. Account
permissions are per-user associations — treat evidence gathered under one
username accordingly, and re-verify after cluster changes.

## References

- [references/kiac-manual-audit.md](references/kiac-manual-audit.md) — what
  the KIAC manual says, where it conflicts, what the 2026-09-14 live
  verification resolved, and what remains unverified. Read before overriding
  any checker verdict "because the manual says so".
- [references/SOURCES.md](references/SOURCES.md) — authoritative SchedMD
  pages (sbatch, GRES, sinfo/scontrol/squeue/sacct, job arrays, reason
  codes). Prefer these over random tutorials when answering Slurm syntax
  questions.
