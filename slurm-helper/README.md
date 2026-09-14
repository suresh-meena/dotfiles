# slurm-helper

A KIAC-aware Slurm skill: template-driven batch script generation plus a
static linter and live scheduler preflight for the KIAC GPU cluster.

The KIAC user manual contradicts itself (partition time limits, node counts,
storage quotas) and ships examples that must not be copied (`--partition=general`,
`--mem=16GB`). This skill encodes that distrust structurally: Slurm syntax is
validated offline against SchedMD semantics, site facts (partitions, GRES
types, MaxTime, accounts) come from the live controller or a dated
`verified_live` section in `config/kiac.yaml` (confirmed by real jobs on
2026-09-14), and only operational site policy comes from the manual — with
every diagnostic labeled `verified-live`, `documented`, `document-conflict`,
or `inferred`.

A critical encoded fact: `sbatch --test-only` does **not** enforce
account/partition or QOS policy (a `chiru`/`a100` request passes the dry run
and then pends forever), so the checker enforces the verified account matrix
itself (KIAC023/KIAC024) and recommends a real smoke job for new combos.

## Layout

```text
skills/slurm-helper/
├── SKILL.md              Skill entrypoint (loaded by the agent)
├── bin/kiac-slurm        CLI wrapper (uses installed entry point if present)
├── bin/slurm-check       Standalone all-bits validator wrapper
├── config/kiac.yaml      Site policy with documented disputes preserved
├── templates/            cpu / gpu / h200 / multi_gpu / array sbatch templates
├── src/kiac_slurm/       Python 3 implementation (stdlib-only; uses PyYAML when present)
├── tests/                pytest suite (44 tests, fake scheduler runner)
└── references/           Manual audit + authoritative SchedMD sources
```

## `slurm-check job.sbatch [--live] [--strict] [--json]` runs, in order:
file structure (ignored directives, shell variables in `#SBATCH`), shell
syntax (`bash -n`, optional shellcheck), generic Slurm semantics (time,
counts, memory exclusivity, arrays, dependencies, GRES syntax), KIAC policy
(partition table, verified account/QOS matrix, H200 account, storage),
filesystem and module checks, then — with `--live` — one cached discovery
sweep (`sinfo --json` with a semantic shape gate, falling back to
`scontrol`), account+QOS association lookup, and finally
`sbatch --test-only`, which validates against the scheduler without
submitting (and with the account-policy caveat flagged). Diagnostics use
stable rule IDs (`SH*`, `SLURM*`, `KIAC*`, `LIVE*`, `FS*`, `MOD*`) with
line numbers and suggested corrections.

`check` never submits; `submit` runs the same checker and requires `--yes`.

## CLI

`kiac-slurm new | check | submit | doctor | resources | account | explain |
interactive | inspect | learn` — see `--help` and `SKILL.md` for the workflow
each supports.

The skill is self-learning: live runs and pending-job reasons are recorded as
evidence (`var/observations.jsonl`), and `kiac-slurm learn log / apply`
distills that evidence into the marker-bounded `verified_live` section of
`config/kiac.yaml` behind a reviewed diff (`--yes`), so the cluster knowledge
improves over time and travels through git.

## Install

```sh
./install.sh          # symlink into ~/.codex/skills (and OpenCode)
pip install skills/slurm-helper   # optional: puts kiac-slurm on PATH
```

The skill itself needs no installation beyond the symlink; the wrappers fall
back to running `src/` directly.

## Tests

```sh
cd skills/slurm-helper && python3 -m pytest -q    # or: pytest
```

Live acceptance on KIAC (per the skill's acceptance list): a valid script
passes `--live`; a manual-style script (`general`/`16GB`), an H200 job
without an account, a bad partition, a bad time, and a shell syntax error all
fail before submission.
