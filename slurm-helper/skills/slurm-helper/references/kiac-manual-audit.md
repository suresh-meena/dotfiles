# KIAC manual audit: what the documents say, and what must not be assumed

Sources: the KIAC user manual ("KIAC Cluster Server: Comprehensive User
Manual", PDF `Instructions and guidelines.pdf` in the project root — kept
out of git) and live verification on 2026-09-14 for user `sureshmeena`.
Labels: `documented`, `document-conflict`, `verified-live (2026-09-14)`.

## Documented conflicts and their consequences

| Area | What the manual says | Skill behavior |
| --- | --- | --- |
| Compute topology | Opens with 10 compute nodes; later says 11 and wavers between one and two master nodes (also "CDS" vs "SERC" Data Centre); node names are `cn1`–`cn10`. | Never hard-code node counts; query Slurm. |
| Partition table | `long`: cn1,cn4/48 h; `short`: cn2,cn5/24 h; `medium`: cn3/24 h; `a100`: cn6/24 h; `ada`: cn7–9/48 h; `h200`: cn10/24 h. | Seeded into `config/kiac.yaml`, limits marked unverified. |
| Time limits | A later section states `short` is 12 h and `long` is 24 h — contradicting the table. | Live `MaxTime` decides; both candidates kept as `disputed`. |
| Storage | Recommends `/storage`; one note says 500 GB home + 300 GB storage (expandable); the final page says 200 GB per user. Three figures, one path. | Recommend `/storage`; never encode a quota; writability not assumed. |
| Example partition | The basic example uses `--partition=general`, absent from the manual's own table. | `general` rejected offline (KIAC011 error) unless found live. |
| Memory example | `--mem=16GB`; SchedMD documents K/M/G/T suffixes. | Linter rewrites to `16G` (SLURM021). |
| H200 | Batch jobs need `--partition=h200` and a group `--account` ("faculty iisc mail id until astrick" — not a usable string); the interactive example adds node `cn10` and `h200_qos`. | Account mandatory (KIAC020); QOS resolved by verification: `h200_qos` required for batch too (KIAC024). |
| GPU names | Infrastructure lists A5000, A6000, A100, ADA6000, H200, but the GRES section validates only A5000/A6000/A100 as typed values. | Per-partition verified types (KIAC051) + live discovery (LIVE020). |
| Modules | Text cites `python/3.8.5`/`cuda/11.2`; screenshots show a different Conda/CUDA setup. Software lives in `/apps/software`, modulefiles in `/apps/modulefiles`. | Never hard-code module versions; `check` resolves `module load` lines via `module show` when available (MOD002). |
| Cancellation | Gives `scancel <job_id>` correctly, then separately says `kill <JOB ID>`. `kill` signals a Unix PID/process group, not a Slurm job. | Always `scancel <jobid>`; `inspect` prints this hint. |
| Account lifecycle | Inactive 90 days → blocked; +90 more → account and data deleted. Violations escalate: warning → privileges revoked → suspension. | Operational guidance only; surfaced in this reference, not lint rules. |

## Verified live, 2026-09-14 (user sureshmeena)

Resolved by scontrol/sacctmgr queries plus real jobs:

- Partitions exist as documented: `long short medium ada a100 h200`.
- GPU types per partition: `long`→a6000, `short`→a5000,
  `medium`→a5000/a6000/ada6000, `ada`→ada6000, `a100`→a100 (smoke job ran
  on cn6), `h200`→h200 (job 58822 ran on cn10).
- This user's associations: `research`, `chiru`. `freerun` is NOT
  associated ("Invalid account or account/partition combination").
- `a100` allows `research` (and `freerun`) — `chiru` is rejected at
  scheduling time even though `sbatch --test-only` accepts it.
- `h200` requires `--qos=h200_qos` (default `normal` pends with "h200
  allows h200_qos not normal"); working combo: `--account=chiru
  --qos=h200_qos --partition=h200`.
- **`sbatch --test-only` does not enforce account-partition or QOS policy.**
  It checks syntax and allocation shape only. Every "newly granted
  account/partition" claim needs a real 5-minute smoke job.
- Compute nodes lack `uv` on PATH (fixed by user-level install; the
  launcher also checks `$HOME/.local/bin/uv`). System Python has no Torch —
  use the experiment virtualenv.
- A two-GPU H200 job (job 58822) provisioned an isolated `sm90` venv
  profile via `--export=ALL,...` env vars; long-running training must fit
  the 24 h h200 MaxTime or checkpoint and resubmit.

## Explicit unknowns (remain until queried again)

- Actual `long`/`short` MaxTime (the live sweep was not captured for them;
  still `disputed` offline).
- Whether `research`+`h200` or other account/QOS combos work for other
  users (the matrix above is this user's associations).
- Storage quotas (three conflicting manual figures; none verified).
- Current module names on the cluster.
- `long`/`short`/`medium`/`ada` account permissions are only
  `--test-only`-verified for `research` and `chiru`; no smoke job confirmed
  them — treat a first real submission there as the validation.

## Operational guidance from the manual (policy, not lint rules)

Credentials are private; avoid idling on allocated resources; all jobs
through Slurm with realistic requests; users are responsible for backups
and cleanup; sensitive data needs explicit permission; activity may be
monitored; maintenance is announced (plan jobs around it); avoid piling up
login-node sessions (multiple VS Code/Cursor windows); issues go to
server.kiac@iisc.ac.in; inactive accounts are blocked/deleted on the
90+90-day schedule above.

The CLI resolves partitions/GRES/times/accounts live; the rest it surfaces
as unknown rather than guessing. `sbatch --test-only` validates the request
against the scheduler but executes nothing — it catches neither
account/QOS policy (above) nor application failures (failing imports,
corrupted datasets, CUDA runtime errors).

## Known checker limitations

- A `#SBATCH` line inside a here-document body is reported as an ignored
  directive (SLURM011). That matches sbatch's real behavior (it does stop at
  the first executable line and would ignore such a directive too), but the
  excerpt can be confusing; the fix is moving heredocs below the directives.
- `FS*` findings are host-relative: paths are checked on the machine running
  the checker, which is not the machine the job runs on. Re-check on the
  login node before treating an `FS*` error as a script bug.
- The valueless-flag set (`--hold`, `--requeue`, ...) is conservative; a
  rarely used flag followed by a bare token may still absorb it.

