"""A fake Slurm that runs real job processes locally (REFINED_PLAN §13.2).

sbatch/squeue/sacct/scancel in tests/fakes/bin call into this module. There is
no daemon: every command first advances the simulation to "now" under a lock.

State lives in $FAKE_SLURM_DIR:
    cluster.json   configuration (see DEFAULTS)
    state.json     jobs, guarded by state.lock (flock)
    spool/<id>.sh  the script as submitted -- Slurm spools it, so later edits don't matter
    rc/<id>.<n>    exit code of run n, written by the job's own wrapper shell

It deliberately reproduces the quirks the scheduler must survive: duplicate job
names, a disallowed account that sbatch accepts and that pends forever, limits
that clear, MinJobAge drop-off, lagging or absent accounting, accepted-then-
failed submissions, and requeue of a job that asked for --no-requeue.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "partitions": {"gpu": {}, "debug": {}},
    "accounts": None,               # None: any account, anywhere, no limits
    "reject_invalid_partition": True,
    "start_delay_s": 0.0,
    "min_job_age_s": 300.0,
    "accounting": True,
    "sacct_lag_s": 0.0,
    "default_sacct_window_s": 86400.0,
    "kill_wait_s": 2.0,
    "faults": {"accept_then_fail": 0},
}
ACTIVE = ("PENDING", "RUNNING")
# Slurm can report these states while the allocation is still live. Keep them
# visible to ordinary squeue calls just like PENDING/RUNNING.
LIVE = ACTIVE + ("CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED", "REQUEUE_HOLD",
                 "REQUEUE_FED", "RESIZING", "SIGNALING", "STAGE_OUT")
NODE = "fake-node1"


# ---- state ------------------------------------------------------------------------------

def _dir(state_dir: str | os.PathLike | None = None) -> Path:
    d = Path(state_dir or os.environ["FAKE_SLURM_DIR"])
    (d / "spool").mkdir(parents=True, exist_ok=True)
    (d / "rc").mkdir(exist_ok=True)
    return d


def load_cfg(d: Path) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = d / "cluster.json"
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    return cfg


def configure(state_dir, **cfg) -> None:
    d = _dir(state_dir)
    with _lock(d):
        (d / "cluster.json").write_text(json.dumps(cfg, indent=2))


@contextlib.contextmanager
def _lock(d: Path):
    with open(d / "state.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def session(state_dir=None, *, advance: bool = True):
    """Lock, load, advance, yield (dir, cfg, state), then save."""
    d = _dir(state_dir)
    with _lock(d):
        cfg = load_cfg(d)
        path = d / "state.json"
        state = json.loads(path.read_text()) if path.exists() else {"next_id": 1000, "jobs": {}}
        if advance:
            _advance(d, cfg, state, time.time())
        yield d, cfg, state
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
        if (d / "cluster.json.pending").exists():
            os.replace(d / "cluster.json.pending", d / "cluster.json")


# ---- simulation -------------------------------------------------------------------------

def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is not running anything.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _killpg(pid: int | None, sig: int) -> None:
    if pid:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _tree(pid: int | None) -> list[int]:
    """The job and every live descendant: what a job cgroup would hold, setsid or not."""
    if not pid:
        return []
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                ppid = int(open(f"/proc/{entry}/stat").read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(int(entry))
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += children.get(p, [])
    return out


def _signal_tree(pid: int | None, sig: int) -> None:
    for p in _tree(pid):
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _stop(job: dict[str, Any], kill_wait_s: float) -> None:
    """SIGTERM everything in the job, SIGKILL after kill_wait_s, like Slurm's KillWait over a cgroup."""
    _signal_tree(job.get("pid"), signal.SIGTERM)
    deadline = time.monotonic() + kill_wait_s
    while _alive(job.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.02)
    _signal_tree(job.get("pid"), signal.SIGKILL)
    deadline = time.monotonic() + 2
    while _alive(job.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.02)


def _rc_file(d: Path, job: dict[str, Any]) -> Path:
    return d / "rc" / f"{job['id']}.{job['restart']}"


def _spawn(d: Path, job: dict[str, Any]) -> None:
    out = job["output"] if Path(job["output"]).parent.is_dir() else "/dev/null"
    err = job["error"] if Path(job["error"]).parent.is_dir() else "/dev/null"
    env = {**job["env"], "SLURM_JOB_ID": str(job["id"]), "SLURM_JOB_NAME": job["name"],
           "SLURM_RESTART_COUNT": str(job["restart"]), "SLURM_JOB_PARTITION": job["partition"],
           "SLURM_SUBMIT_DIR": job["submit_dir"], "SLURM_JOB_NODELIST": NODE}
    rc = str(_rc_file(d, job))
    # setsid inside a background job of a non-interactive sh: not a group leader, so no
    # fork -- $! is the job's pid and its own process group. The launcher exits at once,
    # so nothing here is left as a zombie child of whoever advanced the simulation.
    inner = 'cd "$1" || exit 1; bash "$2" >"$3" 2>"$4"; echo $? >"$5.tmp" && mv "$5.tmp" "$5"'
    launcher = f'setsid sh -c {shlex.quote(inner)} fakeslurm "$@" </dev/null >/dev/null 2>&1 & echo $!'
    proc = subprocess.run(["sh", "-c", launcher, "launcher", job["submit_dir"], job["spool"], out, err, rc],
                          env=env, capture_output=True, text=True, timeout=10)
    job["pid"] = int(proc.stdout.strip())
    job["state"], job["reason"], job["start_time"] = "RUNNING", "None", time.time()


def _finish(job: dict[str, Any], state: str, exit_code: str, now: float) -> None:
    job["state"], job["exit_code"], job["end_time"], job["reason"] = state, exit_code, now, "None"


def _pending_reason(cfg: dict[str, Any], state: dict[str, Any], job: dict[str, Any], now: float) -> str | None:
    part = cfg["partitions"].get(job["partition"])
    if part is None:
        return "PartitionConfig"
    accounts = cfg.get("accounts")
    if accounts is not None:
        acct = accounts.get(job["account"] or "")
        # Accepted at submit time, then pends forever: a real KIAC behaviour.
        if acct is None or job["partition"] not in acct.get("partitions", [job["partition"]]):
            return "AccountNotAllowed"
        if job["qos"] and job["qos"] not in acct.get("qos", [job["qos"]]):
            return "QOSNotAllowed"
    if part.get("max_time_s") is not None and job["time_s"] > part["max_time_s"]:
        return "PartitionTimeLimit"
    if now < job["submit_time"] + float(cfg["start_delay_s"]):
        return "Priority"
    if accounts is not None:
        limit = accounts[job["account"]].get("grp_gpus")
        if limit is not None and job["gpus"]:
            used = sum(j["gpus"] for j in state["jobs"].values()
                       if j["state"] == "RUNNING" and j["account"] == job["account"])
            if used + job["gpus"] > limit:
                return "AssocGrpGRES"
    return None


def _advance(d: Path, cfg: dict[str, Any], state: dict[str, Any], now: float) -> None:
    for job in state["jobs"].values():
        if job["state"] != "RUNNING":
            continue
        rc_path = _rc_file(d, job)
        if rc_path.exists():
            rc = int(rc_path.read_text().strip() or 1)
            _finish(job, "COMPLETED" if rc == 0 else "FAILED", f"{rc}:0", now)
        elif now > job["start_time"] + job["time_s"]:
            _stop(job, float(cfg["kill_wait_s"]))
            _finish(job, "TIMEOUT", "0:15", time.time())
        elif job.get("signal") and not job.get("signalled") and \
                now >= job["start_time"] + job["time_s"] - job["signal"]["before_s"]:
            job["signalled"] = True
            if job["signal"]["batch"]:
                # B: -- the batch shell only (the wrapper's child), never the whole job.
                shells = [p for p in _tree(job["pid"]) if p != job["pid"]][:1]
                for p in shells:
                    try:
                        os.kill(p, getattr(signal, "SIG" + job["signal"]["name"]))
                    except ProcessLookupError:
                        pass
            # Without B:, Slurm signals job steps; a batch script run without srun has none.
        elif not _alive(job["pid"]):
            if rc_path.exists():            # it finished between the two checks
                rc = int(rc_path.read_text().strip() or 1)
                _finish(job, "COMPLETED" if rc == 0 else "FAILED", f"{rc}:0", now)
            else:
                _finish(job, "NODE_FAIL", "0:9", now)
    for jid in sorted(state["jobs"], key=int):
        job = state["jobs"][jid]
        if job["state"] == "PENDING":
            reason = _pending_reason(cfg, state, job, now)
            if reason is None:
                _spawn(d, job)
            else:
                job["reason"] = reason


# ---- option parsing ---------------------------------------------------------------------

LONG = {"job-name": "name", "partition": "partition", "account": "account", "qos": "qos", "time": "time",
        "nodes": "nodes", "ntasks": "ntasks", "cpus-per-task": "cpus", "mem": "mem", "output": "output",
        "error": "error", "gres": "gres", "gpus": "gpus", "signal": "signal"}
SHORT = {"-J": "name", "-p": "partition", "-A": "account", "-q": "qos", "-t": "time", "-N": "nodes",
         "-n": "ntasks", "-c": "cpus", "-o": "output", "-e": "error", "-G": "gpus"}
FLAGS = {"--no-requeue": "no_requeue", "--requeue": "requeue", "--parsable": "parsable"}


def _parse_opts(tokens: list[str], opts: dict[str, Any], *, stop_at_positional: bool) -> list[str]:
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in FLAGS:
            opts[FLAGS[tok]] = True
        elif tok.startswith("--") and tok[2:].split("=", 1)[0] in LONG:
            key, eq, value = tok[2:].partition("=")
            if not eq:
                i += 1
                value = tokens[i]
            opts[LONG[key]] = value
        elif tok in SHORT:
            i += 1
            opts[SHORT[tok]] = tokens[i]
        elif tok.startswith("-"):
            pass                              # an option this fake doesn't model
        elif stop_at_positional:
            return tokens[i:]
        i += 1
    return []


def parse_time(value: str) -> int:
    days = 0
    if "-" in value:
        d, value = value.split("-", 1)
        days = int(d)
        parts = [int(p) for p in value.split(":")] + [0, 0]
        h, m, s = parts[0], parts[1], parts[2]
    else:
        parts = [int(p) for p in value.split(":")]
        if len(parts) == 1:
            h, m, s = 0, parts[0], 0          # bare number = minutes
        elif len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        else:
            h, m, s = parts[0], parts[1], parts[2]
    return days * 86400 + h * 3600 + m * 60 + s


def directives(script: str) -> list[str]:
    """#SBATCH tokens up to the first non-comment, non-blank line -- where Slurm stops reading."""
    tokens: list[str] = []
    for n, line in enumerate(script.splitlines()):
        stripped = line.strip()
        if n == 0 and stripped.startswith("#!"):
            continue
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        if stripped.startswith("#SBATCH"):
            tokens += shlex.split(stripped[len("#SBATCH"):])
    return tokens


def _parse_signal(value: str | None) -> dict[str, Any] | None:
    """--signal=[B:]SIG[@seconds] (default 60 s before the end)."""
    if not value:
        return None
    batch = value.startswith("B:")
    name, _, before = value[2:].partition("@") if batch else value.partition("@")
    return {"batch": batch, "name": name.upper().removeprefix("SIG"), "before_s": int(before or 60)}


def _gpus(opts: dict[str, Any]) -> int:
    if opts.get("gpus"):
        return int(str(opts["gpus"]).rsplit(":", 1)[-1])
    gres = opts.get("gres") or ""
    m = re.match(r"gpu(?::[^:]+)?:(\d+)$", gres)
    return int(m.group(1)) if m else (1 if gres == "gpu" else 0)


# ---- commands ---------------------------------------------------------------------------

def _die(message: str, rc: int = 1) -> int:
    sys.stderr.write(message + "\n")
    return rc


def main_sbatch(argv: list[str]) -> int:
    cli: dict[str, Any] = {}
    rest = _parse_opts(argv, cli, stop_at_positional=True)
    if not rest:
        return _die("sbatch: error: no script given (stdin scripts are not modelled)")
    script_path = Path(rest[0])
    try:
        text = script_path.read_text()
    except OSError as exc:
        return _die(f"sbatch: error: Unable to open file {script_path}: {exc.strerror}")
    opts: dict[str, Any] = {}
    _parse_opts(directives(text), opts, stop_at_positional=False)
    opts.update(cli)                          # the command line overrides the script
    submit_dir = os.getcwd()
    with session() as (d, cfg, state):
        partition = opts.get("partition") or next(iter(cfg["partitions"]))
        if cfg["reject_invalid_partition"] and partition not in cfg["partitions"]:
            return _die("sbatch: error: Batch job submission failed: Invalid partition name specified")
        jid = state["next_id"]
        state["next_id"] = jid + 1
        name = opts.get("name") or script_path.name

        def expand(pattern: str) -> str:
            path = pattern.replace("%j", str(jid)).replace("%x", name)
            return path if path.startswith("/") else str(Path(submit_dir) / path)
        output = expand(opts.get("output") or "slurm-%j.out")
        spool = d / "spool" / f"{jid}.sh"
        shutil.copyfile(script_path, spool)
        state["jobs"][str(jid)] = {
            "id": jid, "name": name, "partition": partition, "account": opts.get("account"),
            "qos": opts.get("qos"), "time_s": parse_time(opts.get("time") or "60"), "gpus": _gpus(opts),
            "output": output, "error": expand(opts["error"]) if opts.get("error") else output,
            "no_requeue": bool(opts.get("no_requeue")), "submit_dir": submit_dir, "env": dict(os.environ),
            "submit_time": time.time(), "state": "PENDING", "reason": "None", "start_time": None,
            "end_time": None, "pid": None, "restart": 0, "exit_code": "0:0", "spool": str(spool),
            "signal": _parse_signal(opts.get("signal")),
        }
        _advance(d, cfg, state, time.time())
        faults = cfg.get("faults") or {}
        reply_delay_s = float(faults.get("reply_delay_s", 0))
        if int(faults.get("accept_then_fail", 0)) > 0:
            # The controller took the job; the reply never made it back.
            faults["accept_then_fail"] = int(faults["accept_then_fail"]) - 1
            (d / "cluster.json.pending").write_text(json.dumps({**cfg, "faults": faults}, indent=2))
            return _die("sbatch: error: Batch job submission failed: Socket timed out on send/recv operation")
    # Keep the job visible in scheduler queries while the submitting client
    # waits for the sbatch reply. This lets tests exercise name based adoption.
    if reply_delay_s > 0:
        time.sleep(reply_delay_s)
    print(jid if opts.get("parsable") else f"Submitted batch job {jid}")
    return 0


def _visible_in_squeue(cfg: dict[str, Any], job: dict[str, Any], now: float) -> bool:
    if job["state"] in LIVE:
        return True
    return job["end_time"] is not None and now <= job["end_time"] + float(cfg["min_job_age_s"])


def _field(job: dict[str, Any], spec: str) -> str:
    return {
        "%i": str(job["id"]), "%A": str(job["id"]), "%T": job["state"], "%j": job["name"],
        "%r": job["reason"] if job["state"] == "PENDING" else "None",
        "%N": NODE if job["start_time"] else "", "%P": job["partition"], "%a": job["account"] or "",
    }.get(spec, spec)


def main_squeue(argv: list[str]) -> int:
    header, ids, names, all_states, fmt = True, None, None, False, "%i|%T|%r|%N|%j"
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--noheader"):
            header = False
        elif tok in ("-j", "--jobs") or tok.startswith("--jobs="):
            ids = (tok.split("=", 1)[1] if "=" in tok else argv[(i := i + 1)]).split(",")
        elif tok in ("-n", "--name") or tok.startswith("--name="):
            names = (tok.split("=", 1)[1] if "=" in tok else argv[(i := i + 1)]).split(",")
        elif tok in ("-t", "--states") or tok.startswith("--states="):
            all_states = (tok.split("=", 1)[1] if "=" in tok else argv[(i := i + 1)]).lower() == "all"
        elif tok in ("-o", "--format") or tok.startswith("--format="):
            fmt = tok.split("=", 1)[1] if "=" in tok else argv[(i := i + 1)]
        i += 1
    now = time.time()
    with session() as (d, cfg, state):
        hidden_ids = {str(x) for x in (cfg.get("faults") or {}).get("squeue_hidden_ids", [])}
        visible = [j for j in state["jobs"].values()
                   if (_visible_in_squeue(cfg, j, now) if all_states else j["state"] in LIVE)]
        # A controller/partition view can temporarily omit a real job. This is
        # deliberately a query-only fault: accounting and the job process keep
        # running, so callers must treat the empty result as uncertain.
        visible = [j for j in visible if str(j["id"]) not in hidden_ids]
        if ids is not None:
            wanted = {x.split("_")[0] for x in ids if x}
            visible = [j for j in visible if str(j["id"]) in wanted]
            known = [j for j in state["jobs"].values()
                     if str(j["id"]) in wanted and _visible_in_squeue(cfg, j, now)]
            if not known:
                return _die("slurm_load_jobs error: Invalid job id specified")
        if names is not None:
            visible = [j for j in visible if j["name"] in names]
        rows = [re.sub(r"%[a-zA-Z]", lambda m, job=j: _field(job, m.group(0)), fmt)
                for j in sorted(visible, key=lambda j: j["id"])]
    if header:
        print(fmt.replace("%", ""))
    for row in rows:
        print(row)
    return 0


def _acct_field(job: dict[str, Any], name: str) -> str:
    if name == "State":
        return f"CANCELLED by {os.getuid()}" if job["state"] == "CANCELLED" else job["state"]
    return {"JobID": str(job["id"]), "JobName": job["name"], "ExitCode": job["exit_code"],
            "Partition": job["partition"], "Account": job["account"] or ""}.get(name, "")


def main_sacct(argv: list[str]) -> int:
    ids, names, fields, header, sep = None, None, ["JobID", "JobName", "State", "ExitCode"], True, None
    starttime = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-n", "--noheader"):
            header = False
        elif tok in ("-P", "--parsable2"):
            sep = "|"
        elif tok in ("-j", "--jobs") or tok.startswith("--jobs="):
            ids = (tok.split("=", 1)[1] if "=" in tok else argv[(i := i + 1)]).split(",")
        elif tok.startswith("--name="):
            names = tok.split("=", 1)[1].split(",")
        elif tok == "--name":
            names = argv[(i := i + 1)].split(",")
        elif tok.startswith("--format="):
            fields = tok.split("=", 1)[1].split(",")
        elif tok.startswith("--starttime="):
            starttime = tok.split("=", 1)[1]
        elif tok in ("-S", "--starttime", "-E", "--endtime", "-o", "--format"):
            if tok in ("-o", "--format"):
                fields = argv[i + 1].split(",")
            elif tok in ("-S", "--starttime"):
                starttime = argv[i + 1]
            i += 1
        i += 1
    now = time.time()
    with session() as (d, cfg, state):
        if not cfg["accounting"]:
            return _die("sacct: error: Slurm accounting storage is disabled")
        lag = float(cfg["sacct_lag_s"])
        earliest = now - float(cfg["default_sacct_window_s"])
        if starttime is not None:
            match = re.fullmatch(r"now-(\d+)days?", starttime)
            if not match:
                return _die(f"sacct: error: unsupported fake start time {starttime}")
            earliest = now - int(match.group(1)) * 86400
        jobs = [j for j in state["jobs"].values()
                if earliest <= j["submit_time"] and now >= j["submit_time"] + lag]
        if ids is not None:
            jobs = [j for j in jobs if str(j["id"]) in {x.split(".")[0] for x in ids}]
        if names is not None:
            jobs = [j for j in jobs if j["name"] in names]
        rows = [[_acct_field(j, f) for f in fields] for j in sorted(jobs, key=lambda j: j["id"])]
    sep = sep or " "
    if header:
        print(sep.join(fields))
    for row in rows:
        print(sep.join(row))
    return 0


def main_scancel(argv: list[str]) -> int:
    targets = [a for a in argv if not a.startswith("-")]
    errors = []
    with session() as (d, cfg, state):
        for jid in targets:
            job = state["jobs"].get(jid.split("_")[0])
            if job is None:
                errors.append(f"scancel: error: Kill job error on job id {jid}: Invalid job id specified")
                continue
            if job["state"] == "PENDING":
                _finish(job, "CANCELLED", "0:0", time.time())
            elif job["state"] == "RUNNING":
                _stop(job, float(cfg["kill_wait_s"]))
                _finish(job, "CANCELLED", "0:15", time.time())
    for message in errors:
        sys.stderr.write(message + "\n")
    return 1 if errors else 0


# ---- test helpers -----------------------------------------------------------------------

def jobs(state_dir) -> list[dict[str, Any]]:
    with session(state_dir) as (_, _, state):
        return [dict(j) for j in sorted(state["jobs"].values(), key=lambda j: j["id"])]


def advance_now(state_dir) -> None:
    with session(state_dir):
        pass


def set_state(state_dir, jobid: int, state_name: str, reason: str = "None") -> None:
    """Inject a scheduler state for visibility/parser fault tests."""
    with session(state_dir, advance=False) as (_, _, state):
        job = state["jobs"][str(jobid)]
        job["state"], job["reason"] = state_name, reason
        if state_name not in LIVE:
            job["end_time"] = time.time()


def wait_for(state_dir, jobid: int, states, timeout: float = 10.0) -> dict[str, Any]:
    states = {states} if isinstance(states, str) else set(states)
    deadline = time.monotonic() + timeout
    while True:
        job = next((j for j in jobs(state_dir) if j["id"] == int(jobid)), None)
        if job is not None and job["state"] in states:
            return job
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {jobid} is {job and job['state']}, wanted {sorted(states)}")
        time.sleep(0.05)


def requeue(state_dir, jobid: int) -> None:
    """Like an administrator's `scontrol requeue`: honoured even for --no-requeue jobs."""
    with session(state_dir) as (d, cfg, state):
        job = state["jobs"][str(jobid)]
        if job["state"] != "RUNNING":
            raise ValueError(f"job {jobid} is {job['state']}, not RUNNING")
        _stop(job, 0.0)
        job["restart"] += 1
        _spawn(d, job)


def kill_all(state_dir) -> None:
    d = Path(state_dir)
    if not (d / "state.json").exists():
        return
    for job in json.loads((d / "state.json").read_text())["jobs"].values():
        _killpg(job.get("pid"), signal.SIGKILL)
