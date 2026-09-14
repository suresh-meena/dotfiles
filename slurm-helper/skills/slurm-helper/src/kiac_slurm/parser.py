"""Static #SBATCH parser.

Knows where Slurm stops reading directives: option processing halts at the
first non-comment, non-blank line, so any later #SBATCH is silently ignored.
Directive arguments are tokenized like a command line (shlex) and values are
never shell-expanded by Slurm, which is why shell variables in #SBATCH values
are flagged downstream rather than interpreted here.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SHORT_TO_LONG = {
    "A": "--account",
    "a": "--array",
    "B": "--extra-node-list",
    "b": "--begin",
    "c": "--cpus-per-task",
    "C": "--constraint",
    "d": "--dependency",
    "D": "--chdir",
    "e": "--error",
    "F": "--nodefile",
    "G": "--gres",
    "H": "--hold",
    "i": "--input",
    "J": "--job-name",
    "k": "--no-kill",
    "L": "--licenses",
    "m": "--distribution",
    "M": "--clusters",
    "N": "--nodes",
    "n": "--ntasks",
    "O": "--overcommit",
    "o": "--output",
    "p": "--partition",
    "Q": "--quiet",
    "q": "--qos",
    "r": "--requeue",
    "s": "--oversubscribe",
    "t": "--time",
    "W": "--wait",
    "w": "--nodelist",
    "x": "--exclude",
}

# Long options that never consume the following token as a value, so
# "#SBATCH --hold foo" leaves "foo" visible to the bare-token rule (SLURM002)
# instead of silently absorbing it.
VALUELESS_LONG = frozenset(
    {
        "--contiguous",
        "--exclusive",
        "--hold",
        "--no-kill",
        "--no-requeue",
        "--overcommit",
        "--quiet",
        "--reboot",
        "--requeue",
        "--spread-job",
    }
)

_SHELL_VAR_RE = re.compile(r"\$[A-Za-z_{(]")
_SHORT_OPT_RE = re.compile(r"^-[A-Za-z]")


@dataclass
class Directive:
    line_no: int
    option: str  # normalized long form ("--mem") or raw token if unrecognized
    value: Optional[str]
    raw: str  # full original line, stripped

    def value_str(self) -> str:
        return self.value if self.value is not None else ""


@dataclass
class ParsedScript:
    path: str
    lines: List[str]
    shebang: Optional[str]
    directives: List[Directive] = field(default_factory=list)
    ignored: List[Directive] = field(default_factory=list)  # after first code line
    body_lines: List[Tuple[int, str]] = field(default_factory=list)
    parse_errors: List[Tuple[int, str]] = field(default_factory=list)
    by_option: Dict[str, List[Directive]] = field(default_factory=dict)

    def get_all(self, option: str) -> List[Directive]:
        return self.by_option.get(option, [])

    def get(self, option: str, default=None):
        """Value of the last occurrence (sbatch uses the last one)."""
        directs = self.get_all(option)
        if not directs:
            return default
        return directs[-1].value

    def has(self, option: str) -> bool:
        return bool(self.get_all(option))

    def directive_for(self, option: str) -> Optional[Directive]:
        directs = self.get_all(option)
        return directs[-1] if directs else None


def parse(path: str) -> ParsedScript:
    with open(path, "r", errors="replace") as fh:
        text = fh.read()
    return parse_text(text, path=path)


def parse_text(text: str, path: str = "<memory>") -> ParsedScript:
    lines = text.splitlines()
    script = ParsedScript(path=path, lines=lines, shebang=None)
    if lines and lines[0].startswith("#!"):
        script.shebang = lines[0].strip()

    in_header = True
    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if in_header:
            if not stripped:
                continue
            if stripped.startswith("#SBATCH"):
                script.directives.extend(_parse_directive_line(stripped, line_no, script))
                continue
            if stripped.startswith("#"):
                continue
            in_header = False
            script.body_lines.append((line_no, stripped))
        else:
            if stripped.startswith("#SBATCH"):
                script.ignored.extend(_parse_directive_line(stripped, line_no, script))
            elif stripped and not stripped.startswith("#"):
                script.body_lines.append((line_no, stripped))

    for directive in script.directives:
        script.by_option.setdefault(directive.option, []).append(directive)
    return script


def _parse_directive_line(stripped: str, line_no: int, script: ParsedScript) -> List[Directive]:
    rest = stripped[len("#SBATCH"):]
    try:
        tokens = shlex.split(rest, posix=True)
    except ValueError as exc:
        script.parse_errors.append((line_no, str(exc)))
        return [Directive(line_no=line_no, option="", value=None, raw=stripped)]

    # A '#' token starts a trailing comment; sbatch's own parser would not
    # treat it as an option either.
    for idx, token in enumerate(tokens):
        if token.startswith("#"):
            tokens = tokens[:idx]
            break

    out: List[Directive] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        value = None
        if token.startswith("--"):
            if "=" in token:
                option, value = token.split("=", 1)
            else:
                option = token
                if option not in VALUELESS_LONG:
                    value, i = _maybe_value(tokens, i)
        elif token.startswith("-") and len(token) >= 2:
            short = token[1:]
            head = short[:1]
            if head in SHORT_TO_LONG:
                # handles both "-p long" and the attached form "-plong"
                option = SHORT_TO_LONG[head]
                attached = short[1:]
                if attached:
                    value = attached
                elif option not in VALUELESS_LONG:
                    value, i = _maybe_value(tokens, i)
            else:
                # clustered/unknown short form; flagged by SLURM001
                option = token
        else:
            option = token  # bare token; flagged by SLURM002
        out.append(Directive(line_no=line_no, option=option, value=value, raw=stripped))
        i += 1
    return out


def _maybe_value(tokens: List[str], i: int) -> Tuple[Optional[str], int]:
    nxt = tokens[i + 1] if i + 1 < len(tokens) else None
    if nxt is not None and not (nxt.startswith("--") or _SHORT_OPT_RE.match(nxt)):
        return nxt, i + 1
    return None, i


def has_shell_var(value: Optional[str]) -> bool:
    return bool(value) and bool(_SHELL_VAR_RE.search(value))


# ---------------------------------------------------------------------------
# Slurm value parsing helpers (shared by rules, live discovery, and the CLI)
# ---------------------------------------------------------------------------

def parse_time(value) -> Optional[int]:
    """Seconds for a Slurm time spec, -1 for unlimited, None if unparseable.

    Accepts [DD-]HH:MM:SS, MM:SS, and bare-minutes forms.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in ("-1", "infinite", "unlimited", "UNLIMITED", "INFINITE"):
        return -1
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        hours, minutes, seconds = 0, nums[0], 0
    elif len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    else:
        hours, minutes, seconds = nums
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def fmt_time(seconds) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "infinite"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


_MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)([KkMmGgTt]?)(B|b)?$")
_MEM_FACTOR = {"": 1.0, "K": 1.0 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024}


def parse_memory(value):
    """(megabytes, problem) where problem is None, 'B-suffix', or 'invalid'."""
    if value is None:
        return None, "invalid"
    text = str(value).strip()
    match = _MEM_RE.match(text)
    if not match:
        return None, "invalid"
    number, suffix, trailing_b = match.groups()
    problem = "B-suffix" if trailing_b else None
    mb = float(number) * _MEM_FACTOR[suffix.upper()]
    return mb, problem


def canonical_memory(value) -> Optional[str]:
    """Rewrite '16GB'/'16gb' -> '16G'; None when already canonical or invalid."""
    match = _MEM_RE.match(str(value).strip())
    if not match:
        return None
    number, suffix, trailing_b = match.groups()
    if not trailing_b:
        return None
    suffix = suffix.upper() or "M"
    return f"{number}{suffix}"


def expand_hostlist(spec: str, limit: int = 4096) -> List[str]:
    """Expand 'cn[7-9]' / 'cn[1,4]' style hostlists (KIAC-scale, no zero-pad)."""
    spec = (spec or "").strip()
    if not spec:
        return []
    match = re.search(r"\[([^\]]*)\]", spec)
    if not match:
        return [spec]
    base = spec[: match.start()]
    out: List[str] = []
    for item in match.group(1).split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            lo, _, hi = item.partition("-")
            if lo.isdigit() and hi.isdigit():
                width = len(lo)
                for n in range(int(lo), int(hi) + 1):
                    out.append(f"{base}{str(n).zfill(width)}")
            else:
                out.append(base + item)
        else:
            out.append(base + item)
        if len(out) > limit:
            return out[:limit]
    return out
