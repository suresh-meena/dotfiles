from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_kb.errors import capability_unavailable, schema_validation_failed, temporary_unavailable


@dataclass
class AdapterResult:
    external_ref: str
    phase: str
    detail: dict[str, Any] = field(default_factory=dict)
    idempotent_submission: bool = False


class BaseAdapter:
    name = "base"
    idempotent_submission = False

    def dispatch(self, intent: dict[str, Any], *, spool_dir: Path) -> AdapterResult:
        raise NotImplementedError

    def cancel(self, external_ref: str, *, spool_dir: Path) -> dict[str, Any]:
        return {"phase": "cancel_requested", "external_ref": external_ref, "verified_termination": False}


class ImportAdapter(BaseAdapter):
    name = "import_only"
    idempotent_submission = True

    def dispatch(self, intent: dict[str, Any], *, spool_dir: Path) -> AdapterResult:
        external_ref = intent.get("external_ref") or f"import:{uuid.uuid4()}"
        spool_dir.mkdir(parents=True, exist_ok=True)
        path = spool_dir / f"import-{external_ref.replace(':', '_')}.json"
        path.write_text(json.dumps(intent, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return AdapterResult(
            external_ref=external_ref,
            phase="accepted",
            detail={"spool_file": str(path), "mode": "observe_and_import_only"},
            idempotent_submission=True,
        )


class LocalProcessAdapter(BaseAdapter):
    name = "local_process"
    idempotent_submission = False

    def dispatch(self, intent: dict[str, Any], *, spool_dir: Path) -> AdapterResult:
        invocation = intent.get("invocation") or {}
        executable = invocation.get("executable")
        arguments = invocation.get("arguments") or []
        if not executable or not isinstance(arguments, list):
            raise schema_validation_failed(
                "Local process dispatch requires an executable and an argument array.",
                hint="Shell strings are not accepted.",
            )
        if not os.path.isabs(executable):
            raise schema_validation_failed("The executable must be an absolute path.")
        if not Path(executable).exists():
            raise temporary_unavailable(f"The executable does not exist: {executable}")
        working_directory = invocation.get("working_directory")
        if working_directory and not Path(working_directory).is_dir():
            raise temporary_unavailable(f"The working directory does not exist: {working_directory}")
        log_dir = spool_dir / "exec-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        execution_id = intent.get("execution_id", uuid.uuid4().hex)
        stdout_path = log_dir / f"{execution_id}.stdout.log"
        stderr_path = log_dir / f"{execution_id}.stderr.log"
        with stdout_path.open("ab") as stdout_handle, stderr_path.open("ab") as stderr_handle:
            process = subprocess.Popen(
                [executable, *arguments],
                cwd=working_directory or None,
                env={**os.environ, **{k: v for k, v in (invocation.get("env") or {}).items()}},
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
        boot_id = None
        boot_path = Path("/proc/sys/kernel/random/boot_id")
        if boot_path.exists():
            boot_id = boot_path.read_text(encoding="utf-8").strip()
        return AdapterResult(
            external_ref=f"pid:{process.pid}",
            phase="accepted",
            detail={
                "pid": process.pid,
                "pid_start_time": _pid_start_time(process.pid),
                "boot_id": boot_id,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "idempotent": False,
            },
        )

    def cancel(self, external_ref: str, *, spool_dir: Path) -> dict[str, Any]:
        if not external_ref.startswith("pid:"):
            return {"phase": "cancel_requested", "external_ref": external_ref, "verified_termination": False}
        pid = int(external_ref.split(":", 1)[1])
        request_path = spool_dir / f"cancel-{pid}.requested"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(
            "Cancellation requested. A wrapper must verify termination before resources are released.\n",
            encoding="utf-8",
        )
        return {
            "phase": "cancel_requested",
            "external_ref": external_ref,
            "verified_termination": False,
            "note": "A cancel request is not verified termination.",
        }


def _pid_start_time(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None
    try:
        fields = stat_path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return fields[19]
    except (IndexError, OSError):
        return None


class SlurmAdapter(BaseAdapter):
    name = "slurm"
    idempotent_submission = False

    def dispatch(self, intent: dict[str, Any], *, spool_dir: Path) -> AdapterResult:
        sbatch = shutil.which("sbatch")
        if sbatch is None:
            raise temporary_unavailable("sbatch is not available on this host.")
        job_script = intent.get("slurm_script")
        if not job_script or not Path(job_script).exists():
            raise schema_validation_failed("Slurm dispatch requires an existing job script.")
        raise temporary_unavailable(
            "Slurm submission is not enabled for this deployment.",
            hint=(
                "Submission without idempotent support must be reconciled by the stable execution ID; "
                "blind resubmission after a timeout is unsafe."
            ),
        )


_ADAPTERS: dict[str, type[BaseAdapter]] = {
    "local_process": LocalProcessAdapter,
    "import_only": ImportAdapter,
    "slurm": SlurmAdapter,
}


def get_adapter(name: str) -> BaseAdapter:
    adapter_class = _ADAPTERS.get(name)
    if adapter_class is None:
        raise capability_unavailable(
            "The requested execution adapter is not installed.",
            adapter=name,
            available=sorted(_ADAPTERS),
        )
    return adapter_class()
