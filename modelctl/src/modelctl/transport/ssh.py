from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any


class SSHTransport:
    """OpenSSH argv-safe transport. Never constructs shell from agent text.

    Note: OpenSSH joins the given argv with spaces and the remote host
    re-parses it through its login shell. To keep remote shell interpretation
    inert we locally quote every argument (see escape_arg) before handing the
    vector to ssh.
    """

    def __init__(self, host: str, user: str | None = None, port: int | None = None):
        self.host = host
        self.user = user
        self.port = port

    def _base(self) -> list[str]:
        target = f"{self.user}@{self.host}" if self.user else self.host
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
        if self.port:
            cmd += ["-p", str(self.port)]
        cmd.append(target)
        return cmd

    def run(self, argv: list[str], *, timeout: int = 30, input_data: bytes | None = None) -> subprocess.CompletedProcess:
        # No `--` separator (invalid for OpenSSH). Each argument is shell-quoted
        # locally so the remote shell cannot reinterpret metacharacters.
        full = self._base() + [self.escape_arg(a) for a in argv]
        return subprocess.run(full, input=input_data, timeout=timeout, capture_output=True)

    def run_posix(self, command_argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
        """Execute POSIX command argv on remote without shell interpolation."""
        # We intentionally do not use `ssh host 'bash -c ...'`; we pass argv vector via ssh exec channel.
        # For remotely executed helpers that need shell, the helper itself is argv[0] and args are encoded.
        return self.run(command_argv, timeout=timeout)

    def check_reachable(self, timeout: int = 10) -> tuple[bool, str]:
        try:
            cp = self.run(["echo", "ok"], timeout=timeout)
            if cp.returncode == 0 and b"ok" in cp.stdout:
                return True, "reachable"
            return False, cp.stderr.decode(errors="ignore")[:500]
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except FileNotFoundError:
            return False, "ssh not found"
        except Exception as e:
            return False, str(e)[:500]

    @staticmethod
    def escape_arg(arg: str) -> str:
        return shlex.quote(arg)
