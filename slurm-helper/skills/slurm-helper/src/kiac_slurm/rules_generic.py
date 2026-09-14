"""Generic Slurm semantics (pipeline stages 1-4 and 6).

Stage 1  file structure (shebang, ignored directives, tokenization)
Stage 2  shell syntax (bash -n, optional shellcheck)
Stage 3  directive semantics (time, counts, memory, output, array, dependency)
Stage 4  GPU/GRES syntax (site/type validity is resolved live in rules_kiac)
Stage 6  filesystem references (chdir, cd, output dirs, commands)
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from typing import Iterator, List, Optional, Tuple

from .diagnostics import CONF_INFERRED, Report
from .parser import (
    Directive,
    ParsedScript,
    canonical_memory,
    has_shell_var,
    parse_memory,
    parse_time,
)

KNOWN_LONG = frozenset(
    """
    --account --acctg-freq --array --begin --bb --bbf --bell --burst-buffer
    --cluster-constraint --clusters --comment --constraint --contiguous
    --core-spec --cores-per-socket --cpu-freq --cpus-per-gpu --cpus-per-task
    --chdir --deadline --delay-boot --dependency --distribution --error
    --exclude --exclusive --export --extra-node-list --get-user-env --gid
    --gpu-bind --gpu-freq --gpus --gpus-per-node --gpus-per-socket --gpus-per-task
    --gres --gres-flags --hint --hold --ignore-pbs --input --job-name
    --kill-on-invalid-dep --licenses --mail-type --mail-user --mcs-label --mem
    --mem-bind --mem-per-cpu --mem-per-gpu --mincpus --network --nice --no-kill
    --no-requeue --nodefile --nodelist --nodes --ntasks --ntasks-per-core
    --ntasks-per-gpu --ntasks-per-node --ntasks-per-socket --ntasks-per-tres
    --open-mode --output --overcommit --oversubscribe --partition --power
    --priority --profile --qos --reboot --requeue --reservation --signal
    --sockets-per-node --spread-job --switches --threads-per-core --time
    --time-min --tmp --tres-bind --tres-per-task --uid --wait --wckey --wrap
    --x11
    """.split()
)

# Options that may legitimately appear on multiple #SBATCH lines.
REPEATABLE = frozenset({"--gres", "--licenses", "--cpu-freq"})

_INT_OPTS = (
    "--ntasks",
    "--ntasks-per-node",
    "--ntasks-per-socket",
    "--ntasks-per-core",
    "--ntasks-per-gpu",
    "--cpus-per-task",
    "--cpus-per-gpu",
    "--sockets-per-node",
    "--cores-per-socket",
    "--threads-per-core",
)
_GPU_COUNT_OPTS = ("--gpus", "--gpus-per-node", "--gpus-per-socket", "--gpus-per-task")
_MEM_OPTS = ("--mem", "--mem-per-cpu", "--mem-per-gpu")

_ARRAY_RE = re.compile(r"^\d+(?:-\d+(?::\d+)?)?(?:,\d+(?:-\d+(?::\d+)?)?)*(?:%\d+)?$")
_DEP_TYPES = frozenset(
    {"after", "afterok", "afternotok", "afterany", "aftercorr", "afterburstbuffer", "singleton", "expand"}
)
_GRES_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_VALID_PCT = frozenset("AaJjNnstux%")


# ---------------------------------------------------------------------------
# Stage 1: structure
# ---------------------------------------------------------------------------

def check_structure(script: ParsedScript, rep: Report) -> None:
    if script.shebang is None:
        rep.error(
            "SLURM012",
            "no shebang on line 1; sbatch requires the first line to name an interpreter",
            suggestion="#!/bin/bash",
        )
    for line_no, msg in script.parse_errors:
        rep.error("SLURM003", f"cannot tokenize #SBATCH line: {msg}", line=line_no)
    body_start = script.body_lines[0][0] if script.body_lines else None
    for directive in script.ignored:
        where = f" (first executable line is {body_start})" if body_start else ""
        rep.error(
            "SLURM011",
            f"#SBATCH below the first executable line is ignored by sbatch{where}",
            line=directive.line_no,
            excerpt=directive.raw,
            suggestion="move the directive above the script body",
        )


# ---------------------------------------------------------------------------
# Stage 2: shell syntax
# ---------------------------------------------------------------------------

def check_shell(path: str, rep: Report, runner=None, shellcheck: str = "auto") -> None:
    from .live import Runner

    if runner is None:
        runner = Runner()
    rc, out = runner.run(["bash", "-n", path])
    if rc == 127:
        rep.info("SH001", "bash not available; shell syntax check skipped")
    elif rc == 0:
        rep.pass_("SH001", "bash syntax valid")
    else:
        rep.error(
            "SH001",
            "bash -n reports a syntax error (it reads commands without executing them)",
            excerpt=_tail(out, 3),
            suggestion="fix the reported construct",
        )

    if shellcheck == "off":
        return
    rc_v, _ = runner.run(["shellcheck", "--version"])
    if rc_v != 0:
        if shellcheck == "on":
            rep.warn("SH100", "shellcheck requested but not installed")
        return
    rc, out = runner.run(["shellcheck", "-s", "bash", path])
    if rc == 0:
        rep.pass_("SH101", "shellcheck clean")
    elif rc == 1:
        rep.warn("SH101", "shellcheck findings", excerpt=_tail(out, 6))
    else:
        rep.warn("SH101", f"shellcheck exited {rc}", excerpt=_tail(out, 3))


def _tail(text: str, n: int) -> Optional[str]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:]) if lines else None


# ---------------------------------------------------------------------------
# Stages 3-4: generic directive semantics
# ---------------------------------------------------------------------------

def check_generic(script: ParsedScript, rep: Report) -> None:
    _check_options_known(script, rep)
    _check_duplicates(script, rep)
    _check_shell_vars(script, rep)
    _check_time(script, rep)
    _check_counts(script, rep)
    _check_memory(script, rep)
    _check_output_patterns(script, rep)
    _check_array(script, rep)
    _check_dependency(script, rep)
    _check_gpus(script, rep)
    _check_task_geometry(script, rep)


def _check_options_known(script: ParsedScript, rep: Report) -> None:
    for directive in script.directives:
        if not directive.option or not directive.option.startswith("-"):
            if directive.option == "":
                continue  # pseudo-directive from a tokenization error (SLURM003)
            rep.warn(
                "SLURM002",
                f"'{directive.option}' is not an option; #SBATCH arguments must start with - or --",
                line=directive.line_no,
                excerpt=directive.raw,
            )
        elif directive.option not in KNOWN_LONG:
            rep.warn(
                "SLURM001",
                f"'{directive.option}' is not a documented sbatch option (typo?)",
                line=directive.line_no,
                excerpt=directive.raw,
                suggestion="check man sbatch",
            )


def _check_duplicates(script: ParsedScript, rep: Report) -> None:
    for option, directs in script.by_option.items():
        if len(directs) > 1 and option in KNOWN_LONG and option not in REPEATABLE:
            lines = ", ".join(str(d.line_no) for d in directs)
            rep.error(
                "SLURM004",
                f"duplicate {option} directives (lines {lines}); sbatch uses the last value",
                line=directs[-1].line_no,
                excerpt=directs[-1].raw,
                suggestion="keep exactly one",
            )


def _check_shell_vars(script: ParsedScript, rep: Report) -> None:
    for directive in script.directives:
        if has_shell_var(directive.value):
            rep.error(
                "SLURM010",
                "#SBATCH values are read by Slurm directly; the shell never expands them, "
                "so this variable will be passed literally",
                line=directive.line_no,
                excerpt=directive.raw,
                suggestion="write the literal value, or pass --mem=$VAR on the sbatch command "
                "line where the shell does expand it",
            )


def _check_time(script: ParsedScript, rep: Report) -> None:
    for option in ("--time", "--time-min"):
        for directive in script.get_all(option):
            if parse_time(directive.value) is None:
                rep.error(
                    "SLURM030",
                    f"invalid time spec '{directive.value}'",
                    line=directive.line_no,
                    excerpt=directive.raw,
                    suggestion="use minutes, MM:SS, HH:MM:SS, or D-HH:MM:SS (-1 = unlimited)",
                )


def _check_counts(script: ParsedScript, rep: Report) -> None:
    for option in _INT_OPTS:
        for directive in script.get_all(option):
            value = directive.value or ""
            if not (value.isdigit() and int(value) >= 1):
                rep.error(
                    "SLURM020",
                    f"{option} expects a positive integer, got '{value}'",
                    line=directive.line_no,
                    excerpt=directive.raw,
                )
    for directive in script.get_all("--nodes"):
        value = directive.value or ""
        match = re.match(r"^(\d+)(?:-(\d+))?$", value)
        if not match:
            rep.error(
                "SLURM020",
                f"--nodes expects N or N-M, got '{value}'",
                line=directive.line_no,
                excerpt=directive.raw,
            )
        elif match.group(2) and int(match.group(1)) > int(match.group(2)):
            rep.error(
                "SLURM031",
                f"--nodes range is inverted ({value})",
                line=directive.line_no,
                excerpt=directive.raw,
                suggestion=f"--nodes={match.group(2)}-{match.group(1)}",
            )
    for option in _GPU_COUNT_OPTS:
        for directive in script.get_all(option):
            if not _valid_gpu_count(directive.value):
                rep.error(
                    "SLURM020",
                    f"{option} expects a count like 2 or gpu:a100:2, got '{directive.value}'",
                    line=directive.line_no,
                    excerpt=directive.raw,
                )


def _valid_gpu_count(value: Optional[str]) -> bool:
    if not value:
        return False
    if value.isdigit():
        return True
    parts = value.split(":")
    if len(parts) == 2 and parts[1].isdigit() and _GRES_NAME_RE.match(parts[0]):
        return True
    if len(parts) == 3 and parts[0] == "gpu" and _GRES_NAME_RE.match(parts[1]) and parts[2].isdigit():
        return True
    return False


def _check_memory(script: ParsedScript, rep: Report) -> None:
    present = [script.directive_for(option) for option in _MEM_OPTS if script.has(option)]
    present = [d for d in present if d]
    if len(present) > 1:
        names = ", ".join(sorted({d.option for d in present}))
        rep.error(
            "SLURM023",
            f"mutually exclusive memory options are all set: {names}",
            suggestion="keep exactly one of --mem, --mem-per-cpu, --mem-per-gpu",
        )
    for directive in present:
        _mb, problem = parse_memory(directive.value)
        if problem == "invalid":
            rep.error(
                "SLURM022",
                f"{directive.option} value '{directive.value}' is not a valid memory size",
                line=directive.line_no,
                excerpt=directive.raw,
                suggestion="e.g. --mem=16G (suffixes K/M/G/T; no suffix means MB)",
            )
        elif problem == "B-suffix":
            canonical = canonical_memory(directive.value)
            rep.error(
                "SLURM021",
                "Slurm documents K/M/G/T suffixes.",
                line=directive.line_no,
                excerpt=directive.raw,
                suggestion=f"{directive.option}={canonical}",
            )


def _check_output_patterns(script: ParsedScript, rep: Report) -> None:
    for option in ("--output", "--error"):
        for directive in script.get_all(option):
            bad = _scan_percent(directive.value or "")
            if bad:
                rep.warn(
                    "SLURM040",
                    f"unknown % substitution(s) {''.join(sorted(bad))} in {option} "
                    f"(valid: %j %x %A %a %J %N %n %s %t %u %%)",
                    line=directive.line_no,
                    excerpt=directive.raw,
                )
    out = script.get("--output")
    err = script.get("--error")
    if out and err and out == err:
        rep.warn(
            "SLURM042",
            "--output and --error name the same file; stdout and stderr will clobber each other",
            suggestion="use separate filenames (e.g. %x_%j.out and %x_%j.err)",
        )


def _scan_percent(value: str) -> List[str]:
    bad = []
    i = 0
    while i < len(value):
        if value[i] == "%":
            if i + 1 < len(value):
                if value[i + 1] not in _VALID_PCT:
                    bad.append(value[i + 1])
                i += 2
                continue
            bad.append("<end>")
        i += 1
    return bad


def _check_array(script: ParsedScript, rep: Report) -> None:
    for directive in script.get_all("--array"):
        value = directive.value or ""
        valid = bool(_ARRAY_RE.match(value))
        if valid:
            for part in value.split("%")[0].split(","):
                lo, _, hi = part.partition("-")
                hi = hi.split(":")[0]
                if hi and int(lo) > int(hi):
                    valid = False
        if not valid:
            rep.error(
                "SLURM050",
                f"invalid --array spec '{value}'",
                line=directive.line_no,
                excerpt=directive.raw,
                suggestion="e.g. --array=1-10, --array=0-99:10, --array=1-100%8",
            )
            continue
        for option in ("--output", "--error"):
            pattern = script.get(option) or ""
            if "%a" not in pattern:
                rep.warn(
                    "SLURM041",
                    f"{option} has no %a; array subtasks will overwrite one file "
                    "(Slurm defines %A master id and %a task id for arrays)",
                    suggestion="e.g. logs/%x_%A_%a.out",
                )


def _check_dependency(script: ParsedScript, rep: Report) -> None:
    for directive in script.get_all("--dependency"):
        value = directive.value or ""
        for token in re.split(r"[,&]", value):
            token = token.strip()
            if not token or not _valid_dep_token(token):
                rep.error(
                    "SLURM060",
                    f"invalid dependency token '{token}'",
                    line=directive.line_no,
                    excerpt=directive.raw,
                    suggestion="types: after, afterok, afternotok, afterany, aftercorr, "
                    "afterburstbuffer, singleton — e.g. --dependency=afterok:123:124",
                )


def _valid_dep_token(token: str) -> bool:
    typ, sep, rest = token.partition(":")
    if typ not in _DEP_TYPES:
        return False
    if not sep:
        return typ == "singleton"
    for id_part in re.split(r"[:+]", rest):
        if not id_part.rstrip("?").isdigit():
            return False
    return True


def _check_gpus(script: ParsedScript, rep: Report) -> None:
    for directive in script.get_all("--gres"):
        value = directive.value or ""
        if not _valid_gres(value):
            rep.error(
                "SLURM070",
                f"invalid --gres spec '{value}'",
                line=directive.line_no,
                excerpt=directive.raw,
                suggestion="--gres=name[:type]:count — e.g. gpu, gpu:2, gpu:a100:1 "
                "(actual type strings come from the cluster)",
            )
    gres_gpu = any((d.value or "").split(":")[0] == "gpu" for d in script.get_all("--gres"))
    flags = [option for option in _GPU_COUNT_OPTS if script.has(option)]
    if gres_gpu and flags:
        rep.warn(
            "SLURM071",
            f"GPUs requested via both --gres and {'/'.join(flags)}; Slurm merges these — prefer one form",
        )


def _valid_gres(value: str) -> bool:
    parts = value.split(":")
    if not parts or not _GRES_NAME_RE.match(parts[0]):
        return False
    if len(parts) == 1:
        return True
    if len(parts) == 2:
        return parts[1].isdigit() and int(parts[1]) >= 1 or bool(_GRES_NAME_RE.match(parts[1]))
    if len(parts) == 3:
        return bool(_GRES_NAME_RE.match(parts[1])) and parts[2].isdigit() and int(parts[2]) >= 1
    return False


def _check_task_geometry(script: ParsedScript, rep: Report) -> None:
    ntasks = script.get("--ntasks")
    ntpn = script.get("--ntasks-per-node")
    nodes = script.get("--nodes")
    if not (ntasks and ntpn and nodes and ntasks.isdigit() and ntpn.isdigit()):
        return
    match = re.match(r"^(\d+)(?:-(\d+))?$", nodes)
    if not match:
        return
    hi = int(match.group(2) or match.group(1))
    if int(ntasks) != int(ntpn) * hi:
        rep.warn(
            "SLURM080",
            f"--ntasks={ntasks} but ntasks-per-node x nodes covers {int(ntpn) * hi}; "
            "nodes/tasks/cpus interact, they are not three spellings of one request",
        )


# ---------------------------------------------------------------------------
# Stage 6: filesystem references
# ---------------------------------------------------------------------------

def check_filesystem(script: ParsedScript, rep: Report) -> None:
    chdir = script.get("--chdir")
    if chdir:
        target = os.path.expanduser(chdir)
        if os.path.isabs(target) and not os.path.isdir(target):
            chdir_d = script.directive_for("--chdir")
            rep.error(
                "FS001",
                f"--chdir target does not exist on this host: {target}",
                line=chdir_d.line_no if chdir_d else None,
                excerpt=chdir_d.raw if chdir_d else None,
                suggestion="create it, or point at the recommended /storage area",
            )
    for line_no, text in script.body_lines:
        match = re.match(r"^cd\s+([^\s;&|]+)", text)
        if not match:
            continue
        target = os.path.expanduser(match.group(1))
        if target.startswith("$") or not os.path.isabs(target):
            continue
        if not os.path.isdir(target):
            rep.error(
                "FS001",
                f"cd target does not exist on this host: {target}",
                line=line_no,
                excerpt=text,
            )
    for option in ("--output", "--error"):
        for directive in script.get_all(option):
            value = directive.value or ""
            dirpart = os.path.dirname(value)
            if not dirpart or dirpart == ".":
                continue
            # sbatch requires the output directory to exist at submit time,
            # even when the filename itself uses %j-style patterns
            if not os.path.isdir(_resolve_exists_dir(dirpart, chdir, script.path)):
                rep.warn(
                    "FS003",
                    f"directory for {option} does not exist yet: {dirpart}",
                    line=directive.line_no,
                    suggestion=f"mkdir -p {dirpart} in the submission directory before "
                    "submitting (sbatch opens output files at submit time)",
                )
    for line_no, text in _iter_command_lines(script):
        words = text.split()
        if not words:
            continue
        exe = words[0]
        if "/" in exe:
            if not _resolve_exists(exe, chdir, script.path):
                rep.error(
                    "FS002",
                    f"executable path does not exist: {exe}",
                    line=line_no,
                    excerpt=text,
                )
        elif shutil.which(exe) is None:
            rep.warn(
                "FS002",
                f"command '{exe}' not found on this host; fine if a module provides it on the cluster",
                line=line_no,
                excerpt=text,
                confidence=CONF_INFERRED,
            )
        if words[0] in ("python", "python3", "python2") and len(words) > 1:
            arg = words[1]
            if arg.startswith(("./", "../", "/")) and not _resolve_exists(arg, chdir, script.path):
                rep.warn(
                    "FS002",
                    f"script argument not found: {arg}",
                    line=line_no,
                    excerpt=text,
                    confidence=CONF_INFERRED,
                )


def _resolve_exists_dir(dirpart: str, chdir: Optional[str], script_path: str) -> str:
    if os.path.isabs(dirpart):
        return dirpart
    if chdir and not chdir.startswith("$"):
        base = os.path.expanduser(chdir)
        if os.path.isdir(base):
            return os.path.join(base, dirpart)
    return os.path.join(os.path.dirname(os.path.abspath(script_path)), dirpart)


def _resolve_exists(path: str, chdir: Optional[str], script_path: str) -> bool:
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        base = os.path.expanduser(chdir) if chdir and not chdir.startswith("$") else os.path.dirname(os.path.abspath(script_path))
        expanded = os.path.join(os.path.expanduser(base) if not os.path.isabs(base) else base, expanded)
    return os.path.exists(expanded)


def _iter_command_lines(script: ParsedScript) -> Iterator[Tuple[int, str]]:
    for line_no, text in script.body_lines:
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        while True:
            match = re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S+\s+", stripped)
            if not match:
                break
            stripped = stripped[match.end():].strip()
        if not stripped:
            continue
        if stripped.startswith(("cd ", "cd\t", "module ", "module\t", "source ", ". ", "export ", "set ")):
            continue
        if any(op in stripped for op in (";", "&&", "||", "|", ">>", ">")):
            continue
        yield line_no, stripped


# ---------------------------------------------------------------------------
# Stage 6b: module resolution (only when the module command is available)
# ---------------------------------------------------------------------------

_MODULE_LOAD_RE = re.compile(r"^module\s+load\s+(.+)$")


def check_modules(script: ParsedScript, rep: Report, runner) -> None:
    loads: List[Tuple[int, str, List[str]]] = []
    for line_no, text in script.body_lines:
        match = _MODULE_LOAD_RE.match(text.strip())
        if match:
            names = shlex.split(match.group(1))
            loads.append((line_no, text, [n for n in names if not n.startswith("-")]))
    if not loads:
        return

    rc, _ = runner.run(["bash", "-lc", "type module"])
    if rc != 0:
        rep.info(
            "MOD001",
            f"module command not available here; {sum(len(n) for _, _, n in loads)} "
            "module load(s) left unchecked (verify with `module avail` on the login node)",
        )
        return
    for line_no, text, names in loads:
        for name in names:
            quoted = shlex.quote(name)
            rc, _out = runner.run(["bash", "-lc", f"module show {quoted} >/dev/null 2>&1"])
            if rc == 0:
                rep.pass_("MOD002", f"module '{name}' resolvable", line=line_no)
            else:
                rep.warn(
                    "MOD002",
                    f"module '{name}' not found by `module show`",
                    line=line_no,
                    excerpt=text,
                    suggestion="check `module avail` on the login node; never trust "
                    "module versions printed in the manual",
                )


# ---------------------------------------------------------------------------
# GPU request extraction (shared with rules_kiac and the CLI)
# ---------------------------------------------------------------------------

def iter_gpu_requests(script: ParsedScript):
    """Yield (line_no, raw, gpu_type_or_None, count_or_None) per GPU request."""
    for directive in script.get_all("--gres"):
        parts = (directive.value or "").split(":")
        if not parts or parts[0] != "gpu":
            continue
        if len(parts) == 3:
            gtype, count = parts[1], int(parts[2]) if parts[2].isdigit() else None
        elif len(parts) == 2 and parts[1].isdigit():
            gtype, count = None, int(parts[1])
        else:
            gtype, count = None, None
        yield directive.line_no, directive.raw, gtype, count
    for option in _GPU_COUNT_OPTS:
        for directive in script.get_all(option):
            value = directive.value or ""
            parts = value.split(":")
            if len(parts) == 3 and parts[0] == "gpu":
                gtype, count = parts[1], int(parts[2]) if parts[2].isdigit() else None
            elif len(parts) == 2 and parts[1].isdigit():
                gtype, count = None, int(parts[1])
            elif value.isdigit():
                gtype, count = None, int(value)
            else:
                gtype, count = None, None
            yield directive.line_no, directive.raw, gtype, count
