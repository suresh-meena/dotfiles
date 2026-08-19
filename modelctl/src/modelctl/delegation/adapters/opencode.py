from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


class OpenCodeAdapter:
    def __init__(self, executable: str = "opencode", provider: str = "opencode-go"):
        self.executable = executable
        self.provider = provider

    def check_available(self) -> tuple[bool, str]:
        try:
            cp = subprocess.run([self.executable, "--help"], capture_output=True, timeout=5)
            return cp.returncode == 0, "available" if cp.returncode == 0 else cp.stderr.decode()[:300]
        except FileNotFoundError:
            return False, "opencode not found"
        except Exception as e:
            return False, str(e)[:300]

    def version(self) -> str | None:
        try:
            cp = subprocess.run([self.executable, "--version"], capture_output=True, timeout=5, text=True)
            out = (cp.stdout + cp.stderr).strip()
            import re

            m = re.search(r"(\d+\.\d+\.\d+)", out)
            if m:
                return m.group(1)
            return out.split()[0] if out else None
        except Exception:
            return None

    def run(
        self,
        *,
        model_ref: str,
        agent_profile: str,
        task_prompt: str,
        workdir: Path,
        timeout_s: int = 600,
        extra_args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Invoke `opencode --pure run --model <ref> --agent <profile> --format json --dir <workdir> <prompt>` via argv vector."""
        # Validate model_ref and agent_profile to prevent injection
        if "/" not in model_ref or any(c in model_ref for c in [";", "&", "|", "`", "$", "\n"]):
            raise ValueError(f"invalid model_ref: {model_ref}")
        if any(c in agent_profile for c in [";", "&", "|", "`", "$", "\n", " "]):
            raise ValueError(f"invalid agent_profile: {agent_profile}")
        # Build argv vector - never shell
        argv = [
            self.executable,
            "--pure",
            "run",
            "--model",
            model_ref,
            "--agent",
            agent_profile,
            "--format",
            "json",
            "--dir",
            str(workdir),
        ]
        if extra_args:
            argv.extend(extra_args)
        argv.append(task_prompt)

        # Spawn in new process group for containment per §36.1
        start = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            from ...errors import ModelctlError

            raise ModelctlError(code="E_OPENCODE_NOT_FOUND", message="opencode executable not found")
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            latency_ms = int((time.time() - start) * 1000)
            # Try to parse stdout as JSON (opencode --format json should emit JSON events)
            try:
                data = json.loads(stdout) if stdout.strip().startswith("{") else {"raw": stdout[:2000]}
            except Exception:
                data = {"raw": stdout[:2000], "stderr": stderr[:2000]}
            return {
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "latency_ms": latency_ms,
                "parsed": data,
                "argv": argv,  # for provenance, but redact prompt body in logs? we keep full for internal
            }
        except subprocess.TimeoutExpired:
            # SIGTERM owned group, wait grace, SIGKILL
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                proc.wait(timeout=5)
            # verify no owned child remains (best effort)
            latency_ms = int((time.time() - start) * 1000)
            from ...errors import ModelctlError

            raise ModelctlError(code="E_DELEGATE_TIMEOUT", message=f"opencode run timed out after {timeout_s}s", details={"latency_ms": latency_ms})

    def build_argv(self, *, model_ref: str, agent_profile: str, workdir: Path, prompt: str) -> list[str]:
        # helper for testing / dry-run
        return [
            self.executable,
            "--pure",
            "run",
            "--model",
            model_ref,
            "--agent",
            agent_profile,
            "--format",
            "json",
            "--dir",
            str(workdir),
            prompt,
        ]
