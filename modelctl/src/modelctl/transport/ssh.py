from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


class SSHTransport:
    """OpenSSH argv-safe transport. Never constructs shell from agent text.

    Note: OpenSSH joins the given argv with spaces and the remote host
    re-parses it through its login shell. To keep remote shell interpretation
    inert we locally quote every argument (see escape_arg) before handing the
    vector to ssh.

    password_file: optional path to a 0600 file containing only the password.
    When set, ssh is wrapped with `sshpass -f` (key auth never hits the disk
    for hosts whose authorized_keys are unreadable). The password never
    appears in argv or logs.
    """

    def __init__(self, host: str, user: str | None = None, port: int | None = None, password_file: str | None = None):
        self.host = host
        self.user = user
        self.port = port
        self.password_file = str(Path(password_file).expanduser()) if password_file else None

    def _base(self) -> list[str]:
        target = f"{self.user}@{self.host}" if self.user else self.host
        cmd: list[str] = []
        if self.password_file:
            pw = Path(self.password_file)
            if not pw.is_file():
                raise FileNotFoundError(f"ssh password_file not found: {pw}")
            cmd += ["sshpass", "-f", str(pw)]
        cmd += ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
        if not self.password_file:
            cmd += ["-o", "BatchMode=yes"]  # key-only; password path needs the prompt
        if self.port:
            cmd += ["-p", str(self.port)]
        cmd.append(target)
        return cmd

    def run(self, argv: list[str], *, timeout: int = 30, input_data: bytes | None = None) -> subprocess.CompletedProcess:
        # No `--` separator (invalid for OpenSSH). Each argument is shell-quoted
        # locally so the remote shell cannot reinterpret metacharacters.
        full = self._base() + [self.escape_arg(a) for a in argv]
        return subprocess.run(full, input=input_data, timeout=timeout, capture_output=True)

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
