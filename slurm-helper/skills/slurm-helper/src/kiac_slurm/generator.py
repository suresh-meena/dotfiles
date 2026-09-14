"""Template-driven batch script generator.

Templates live in templates/ and use {{token}} placeholders. Conditional
blocks (account/QOS/array/GRES lines, module loads) expand to a full line
or to nothing, and runs of blank lines are collapsed afterwards.

Strict bash mode is a template policy, not a correctness requirement:
workloads that rely on unset variables or tolerated pipeline failures get
'set -e' instead of 'set -euo pipefail'.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict

from .config import repo_root


class GeneratorError(RuntimeError):
    pass


TEMPLATES = ("cpu", "gpu", "h200", "multi_gpu", "array")


def templates_dir() -> Path:
    env = os.environ.get("KIAC_SLURM_TEMPLATES")
    if env:
        return Path(env)
    return repo_root() / "templates"


def default_values(template: str) -> Dict[str, str]:
    return {
        "job_name": "job",
        "partition": "h200" if template == "h200" else "",
        "time": "1:00:00",
        "ntasks": "1",
        "cpus_per_task": "1",
        "mem": "4G",
        "nodes": "1",
        "ntasks_per_node": "1",
        "workdir": ".",
        "gres": "",
        "account": "",
        "qos": "",
        "array": "",
        "module": "",
        "command": "python3 main.py",
    }


def generate(template: str, values: Dict[str, str], strict_bash: bool = True) -> str:
    if template not in TEMPLATES:
        raise GeneratorError(f"unknown template '{template}' (choose from {', '.join(TEMPLATES)})")
    tpl_path = templates_dir() / f"{template}.sbatch"
    if not tpl_path.exists():
        raise GeneratorError(f"template file missing: {tpl_path}")

    fills = default_values(template)
    fills.update({k: v for k, v in values.items() if v})
    if template in ("gpu", "multi_gpu", "h200") and not fills.get("gres"):
        fills["gres"] = "gpu:1"
    if template == "cpu":
        fills["gres"] = ""

    if template != "h200" and not fills.get("partition"):
        raise GeneratorError(
            "refusing to guess a partition (site rule); pass --partition after checking "
            "`kiac-slurm resources`"
        )
    if template == "h200" and not fills.get("account"):
        raise GeneratorError(
            "H200 jobs require an account (KIAC020); pass --account after checking "
            "`kiac-slurm account`"
        )
    if template == "array" and not fills.get("array"):
        raise GeneratorError("the array template requires --array (e.g. --array 1-100:10%8)")

    fills["strict_line"] = "set -euo pipefail" if strict_bash else "set -e"
    fills["account_line"] = f"#SBATCH --account={fills['account']}\n" if fills.get("account") else ""
    fills["qos_line"] = f"#SBATCH --qos={fills['qos']}\n" if fills.get("qos") else ""
    fills["gres_line"] = (
        f"#SBATCH --gres={fills['gres']}\n"
        if fills.get("gres") and template in ("gpu", "multi_gpu", "h200", "array")
        else ""
    )
    fills["array_line"] = f"#SBATCH --array={fills['array']}\n" if fills.get("array") else ""
    fills["module_line"] = f'module load "{fills["module"]}"\n' if fills.get("module") else ""

    text = tpl_path.read_text()
    for key, val in fills.items():
        text = text.replace("{{" + key + "}}", str(val))
    leftover = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)))
    if leftover:
        raise GeneratorError(f"unfilled template tokens: {', '.join(leftover)}")
    return re.sub(r"\n{3,}", "\n\n", text)
