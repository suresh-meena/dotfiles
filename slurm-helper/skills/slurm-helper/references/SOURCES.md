# Authoritative sources

Prefer these over tutorials when answering Slurm questions. Everything in
the checker's offline rules derives from the SchedMD pages below.

| Resource | Used for |
| --- | --- |
| [sbatch](https://slurm.schedmd.com/sbatch.html) | Directive parsing and first-executable-line rule, memory suffixes (K/M/G/T), filename patterns, `--test-only`, client RPC caution |
| [GRES guide](https://slurm.schedmd.com/gres.html) | `--gres=name[:type]:count` semantics; types are admin-configured |
| [CPU Management](https://slurm.schedmd.com/cpu_management.html) | nodes × tasks × cpus-per-task interaction |
| [sinfo](https://slurm.schedmd.com/sinfo.html) | Partition/node/GRES discovery (JSON where supported) |
| [scontrol](https://slurm.schedmd.com/scontrol.html) | Partition MaxTime, node details, job records |
| [squeue](https://slurm.schedmd.com/squeue.html) + [Job Reason Codes](https://slurm.schedmd.com/job_reason_codes.html) | Pending-job diagnostics (`Resources`, `Priority`, `Dependency`, association/QOS limits) |
| [sacct](https://slurm.schedmd.com/sacct.html) / [sacctmgr](https://slurm.schedmd.com/sacctmgr.html) | Finished-job history; account/QOS associations |
| [Job Arrays](https://slurm.schedmd.com/job_array.html) | `%A`/`%a` naming, array spec syntax |
| [GNU Bash manual — The Set Builtin](https://www.gnu.org/software/bash/manual/html_node/The-Set-Builtin.html) | `bash -n` syntax validation |
| [ShellCheck](https://www.shellcheck.net/wiki/SC2148) | Optional shell static analysis (auto-detected, never required) |
| [kill(2) man page](https://man7.org/linux/man-pages/man2/kill.2.html) | Why `kill <jobid>` is wrong and `scancel` is right |

Version caveat: option sets and JSON output vary across Slurm releases; the
CLI prefers structured output when the installed version supports it and
falls back to stable `scontrol` text otherwise.
