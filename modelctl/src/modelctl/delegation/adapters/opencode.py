from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def _find_text(obj: Any) -> str | None:
    """Recursively find the last non-empty 'text' string in a parsed event."""
    if isinstance(obj, str):
        return None
    if isinstance(obj, dict):
        t = obj.get("text")
        if isinstance(t, str) and t.strip():
            return t
        for v in obj.values():
            r = _find_text(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_text(v)
            if r:
                return r
    return None


def _parse_json_output(stdout: str) -> dict[str, Any]:
    """opencode --format json emits newline-delimited JSON events. Extract
    event count and the final assistant text; fall back to raw capture."""
    events: list[dict[str, Any]] = []
    final_text: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            events.append(obj)
            t = _find_text(obj)
            if t:
                final_text = t
    if events:
        return {"events": len(events), "text": final_text, "last_event": events[-1]}
    return {"raw": stdout[:2000]}


class OpenCodeAdapter:
    def __init__(self, executable: str = "opencode", provider: str = "opencode-go"):
        self.executable = executable
        self.provider = provider
        self._available: tuple[bool, str] | None = None

    def check_available(self) -> tuple[bool, str]:
        if self._available is not None:
            return self._available
        try:
            cp = subprocess.run([self.executable, "--help"], capture_output=True, timeout=5)
            self._available = (cp.returncode == 0, "available" if cp.returncode == 0 else cp.stderr.decode()[:300])
        except FileNotFoundError:
            self._available = (False, "opencode not found")
        except Exception as e:
            self._available = (False, str(e)[:300])
        return self._available

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
        on_start: Any | None = None,
        variant: str | None = None,
    ) -> dict[str, Any]:
        """Invoke `opencode --pure run --model <ref> --agent <profile> --format json --dir <workdir> <prompt>` via argv vector.

        variant is provider-specific reasoning effort; None (default) omits the
        flag and uses the model's own default.
        on_start(proc) is invoked with the Popen handle immediately after
        spawn so the caller can record the process pid for later cancellation.
        """
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
        if variant:
            argv += ["--variant", variant]
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
        if on_start is not None:
            try:
                on_start(proc)
            except Exception:
                pass
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            latency_ms = int((time.time() - start) * 1000)
            data = _parse_json_output(stdout)
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
