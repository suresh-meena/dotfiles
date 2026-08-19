from __future__ import annotations

from typing import Any


def make_result_envelope(*, run_id: str, caller: str, role: str, selected_model: str, task_class: str, workspace: dict[str, Any], validation: dict[str, Any], usage: dict[str, Any], backend: str = "opencode-run") -> dict[str, Any]:
    return {
        "ok": True,
        "run_id": run_id,
        "caller": caller,
        "role": role,
        "selected_model": selected_model,
        "task_class": task_class,
        "workspace": workspace,
        "validation": validation,
        "usage": usage,
        "provenance": {"backend": backend, "model_ref": selected_model},
    }
