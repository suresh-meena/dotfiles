"""The only module that spawns fleetctl (§11).

fleetctl stays the independent transport and policy layer (§8): admission,
routes, host keys, preflight, budgets and audit. fleetq asks it for typed JSON
results (``fleetctl.result/v1``) and never interprets exit codes by guesswork.

The decisive field is ``may_have_executed``. When fleetctl says an operation
may have run, or fleetq can't get a trustworthy envelope back after the
command could have been sent, the result is *uncertain*. Uncertain is never
an ordinary retryable failure (§8).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_STDOUT = 4 * 1024 * 1024

# fleetctl is a ~14k-line script. Run directly, Python recompiles all of it on every
# call (0.23 s of a 0.29 s call on the Pi 5); loaded through this launcher, the
# compiled bytecode is cached under PREFIX and only the work is paid for. The
# script still runs as itself: same argv, same exit status, same stdout.
LAUNCHER = """\
import importlib.machinery, importlib.util, sys
path, prefix = sys.argv[1], sys.argv[2]
if prefix:
    sys.pycache_prefix = prefix
sys.argv = [path, *sys.argv[3:]]
loader = importlib.machinery.SourceFileLoader("fleetctl", path)
spec = importlib.util.spec_from_loader("fleetctl", loader)
mod = importlib.util.module_from_spec(spec)
sys.modules["fleetctl"] = mod
loader.exec_module(mod)
main = getattr(mod, "main", None)
sys.exit(main() if callable(main) else 0)
"""


def _is_python_script(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            first = handle.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first
MAX_STDERR = 64 * 1024


@dataclass
class FleetctlResult:
    outcome: str                      # ok | refused | transport_failed | remote_failed | budget_refused
                                      # | malformed_response | timeout | may_have_executed | local_error
    may_have_executed: bool
    exit_code: int | None = None
    remote_exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    retry_after: float | None = None
    envelope: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def payload(self) -> dict[str, Any] | None:
        """The remote command's own JSON (e.g. fq-node's one object), if parseable."""
        text = self.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text.splitlines()[-1])
        except json.JSONDecodeError:
            return None


class Fleetctl:
    def __init__(self, path: Path | str, *, config_home: Path | None = None, state_home: Path | None = None,
                 cache_home: Path | None = None, caller: str = "fleetq", concurrency: int = 3,
                 pycache_dir: Path | None = None) -> None:
        self.path = str(path)
        self.pycache_dir = pycache_dir
        self.config_home = config_home
        self.state_home = state_home
        self.cache_home = cache_home
        self.caller = caller
        self._sem = asyncio.Semaphore(concurrency)
        self.invocations: list[list[str]] = []           # for tests and the remote-call ledger

    def _base(self) -> list[str]:
        if self.pycache_dir is not None and _is_python_script(self.path):
            argv = [sys.executable, "-c", LAUNCHER, self.path, str(self.pycache_dir)]
        else:
            argv = [self.path]
        for flag, value in (("--config-home", self.config_home), ("--state-home", self.state_home),
                            ("--cache-home", self.cache_home)):
            if value is not None:
                argv += [flag, str(value)]
        return argv

    async def _run(self, argv: list[str], *, timeout: float, mutation: bool,
                   permit: str | None = None, expected_verb: str | None = None,
                   expected_target: str | None = None) -> FleetctlResult:
        env = {**os.environ, "FLEETCTL_CALLER": self.caller}
        env.pop("SSH_AUTH_SOCK", None)                 # never forward the controller's agent (§3.5)
        if permit:
            env["FLEETCTL_PERMIT"] = permit
        self.invocations.append(argv)
        async with self._sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True)
            except OSError as exc:
                return FleetctlResult("local_error", False, stderr=str(exc))
            try:
                # fleetctl enforces its own --timeout; ours is a backstop for a
                # wedged local process, and hitting it means "unknown".
                out, err = await asyncio.wait_for(_drain(proc), timeout + 20)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return FleetctlResult("timeout", mutation, stderr="fleetctl did not return before the backstop")
        return _classify(proc.returncode, out, err, mutation=mutation,
                         expected_verb=expected_verb, expected_target=expected_target)

    async def exec(self, target: str, argv: list[str], *, timeout: float, mutation: bool, admin: bool = False,
                   op_class: str | None = None, permit: str | None = None,
                   expected_role: str | None = None) -> FleetctlResult:
        cmd = self._base() + ["exec", "--json", "--timeout", str(int(timeout)), "--target", target]
        if expected_role is not None:
            if expected_role not in {"workstation", "login"}:
                raise ValueError("expected_role must be workstation or login")
            cmd += ["--expected-role", expected_role]
        if admin:
            cmd.append("--admin")
        if op_class:
            cmd += ["--op-class", op_class]
        return await self._run(cmd + ["--", *argv], timeout=timeout, mutation=mutation, permit=permit,
                               expected_verb="exec", expected_target=target)

    async def sync_push(self, target: str, local: Path, remote: str, *, timeout: float,
                        admin: bool = False, permit: str | None = None,
                        budget_bytes: int | None = None,
                        expected_role: str | None = None) -> FleetctlResult:
        cmd = self._base() + ["sync", "push", "--json", "--timeout", str(int(timeout)), "--target", target,
                              str(local), remote]
        if expected_role is not None:
            if expected_role not in {"workstation", "login"}:
                raise ValueError("expected_role must be workstation or login")
            cmd += ["--expected-role", expected_role]
        if admin:
            cmd.append("--admin")
        if budget_bytes is not None:
            if budget_bytes <= 0:
                raise ValueError("budget_bytes must be positive")
            cmd += ["--budget-bytes", str(budget_bytes)]
        # A push stages inputs only; it executes nothing, so it is not a mutation of execution state.
        return await self._run(cmd, timeout=timeout, mutation=False, permit=permit,
                               expected_verb="sync", expected_target=target)

    async def sync_pull(self, target: str, remote: str, local: Path, *, timeout: float,
                        admin: bool = False, permit: str | None = None,
                        budget_bytes: int | None = None,
                        expected_role: str | None = None) -> FleetctlResult:
        """Copy the *contents* of remote directory ``remote`` into ``local``. Reads only."""
        cmd = self._base() + ["sync", "pull", "--json", "--timeout", str(int(timeout)), "--target", target,
                              str(local), remote]
        if expected_role is not None:
            if expected_role not in {"workstation", "login"}:
                raise ValueError("expected_role must be workstation or login")
            cmd += ["--expected-role", expected_role]
        if admin:
            cmd.append("--admin")
        if budget_bytes is not None:
            if budget_bytes <= 0:
                raise ValueError("budget_bytes must be positive")
            cmd += ["--budget-bytes", str(budget_bytes)]
        return await self._run(cmd, timeout=timeout, mutation=False, permit=permit,
                               expected_verb="sync", expected_target=target)

    async def preflight(self, script: Path, target: str) -> FleetctlResult:
        """Offline lint (never connects without --live). Its --json report is its own
        document, not a result envelope: outcome is ``ok`` when a report came back, with
        ``details["errors"]`` holding the ERROR rule ids, else ``malformed_response``."""
        cmd = self._base() + ["preflight", str(script), "--target", target, "--json"]
        result = await self._run(cmd, timeout=60, mutation=False)
        try:
            report = json.loads(result.stdout if result.envelope is None else "")
        except json.JSONDecodeError:
            return result
        if not isinstance(report, dict) or not isinstance(report.get("diagnostics"), list):
            return result
        errors = sorted({d.get("rule_id", "?") for d in report["diagnostics"]
                         if isinstance(d, dict) and str(d.get("level", "")).upper() == "ERROR"})
        return FleetctlResult("ok", False, exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                              details={"report": report, "errors": errors})


async def _drain(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Read both pipes to EOF with caps, discarding the excess so the child never blocks."""

    async def read(stream, cap):
        chunks, size = [], 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            if size < cap:
                chunks.append(chunk[: cap - size])
            size += len(chunk)
        return b"".join(chunks)
    out, err = await asyncio.gather(read(proc.stdout, MAX_STDOUT), read(proc.stderr, MAX_STDERR))
    await proc.wait()
    return out, err


def _classify(rc: int | None, out: bytes, err: bytes, *, mutation: bool,
              expected_verb: str | None = None, expected_target: str | None = None) -> FleetctlResult:
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    # Under --json, fleetctl's stdout is exactly one (pretty-printed) JSON document.
    # Anything else -- extra text, two documents, a truncated one -- is not an envelope.
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
    if not _valid_envelope(envelope, rc, expected_verb=expected_verb, expected_target=expected_target):
        envelope = None
    if envelope is None:
        # No trustworthy envelope. For a mutation, assume it may have run.
        return FleetctlResult("malformed_response", mutation, exit_code=rc, stdout=stdout, stderr=stderr)
    return FleetctlResult(
        outcome=envelope.get("outcome", "malformed_response"),
        may_have_executed=envelope["may_have_executed"],
        exit_code=envelope["exit_code"],
        remote_exit_code=envelope["remote_exit_code"],
        stdout=envelope["stdout"],
        stderr=envelope["stderr"],
        retry_after=envelope["details"].get("retry_after"),
        envelope=envelope,
        details=envelope["details"],
    )


def _valid_envelope(value: Any, rc: int | None, *, expected_verb: str | None,
                    expected_target: str | None) -> bool:
    """Reject incomplete or contradictory results before trusting retry safety."""
    if not isinstance(value, dict) or value.get("schema") != "fleetctl.result/v1":
        return False
    required = {"ok", "verb", "target", "route", "outcome", "may_have_executed", "exit_code",
                "remote_exit_code", "stdout", "stderr", "stdout_truncated", "stderr_truncated",
                "duration_s", "error", "details"}
    if not required.issubset(value):
        return False
    if expected_verb is not None and value["verb"] != expected_verb:
        return False
    outcomes = {"ok", "refused", "transport_failed", "remote_failed", "budget_refused",
                "malformed_response", "timeout", "may_have_executed"}
    outcome = value.get("outcome")
    executed = value.get("may_have_executed")
    code = value.get("exit_code")
    details = value.get("details")
    if (not isinstance(outcome, str) or outcome not in outcomes
            or type(value.get("ok")) is not bool
            or value["ok"] != (outcome == "ok") or type(executed) is not bool
            or type(code) is not int or code != rc
            or not isinstance(value.get("verb"), str)
            or value["verb"] not in {"exec", "script", "sync", "submit", "job"}
            or not isinstance(details, dict)):
        return False
    if outcome == "ok" and code != 0:
        return False
    if outcome in {"refused", "transport_failed", "budget_refused"} and executed:
        return False
    if outcome in {"remote_failed", "may_have_executed"} and not executed:
        return False
    if expected_target is not None:
        target = value["target"]
        if target != expected_target and not (target is None and outcome in {"refused", "timeout"}
                                              and not executed):
            return False
    for field in ("target", "route"):
        if value.get(field) is not None and not isinstance(value[field], str):
            return False
    remote_code = value.get("remote_exit_code")
    if remote_code is not None and type(remote_code) is not int:
        return False
    if not all(isinstance(value.get(field), str) for field in ("stdout", "stderr")):
        return False
    if not all(type(value.get(field)) is bool for field in ("stdout_truncated", "stderr_truncated")):
        return False
    duration = value.get("duration_s")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        return False
    error = value.get("error")
    if error is not None and (not isinstance(error, dict)
                              or not isinstance(error.get("class"), str)
                              or not isinstance(error.get("message"), str)):
        return False
    retry_after = details.get("retry_after")
    if retry_after is not None and (type(retry_after) not in (int, float)
                                    or not math.isfinite(retry_after) or retry_after < 0):
        return False
    return True
