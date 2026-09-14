"""Miscellaneous integration tests: bin wrappers, module checks, filesystem."""

import os
import subprocess

from _path import SRC  # noqa: F401

from helpers import SITE
from kiac_slurm.diagnostics import Report
from kiac_slurm.live import Runner
from kiac_slurm.parser import parse_text
from kiac_slurm.rules_generic import check_filesystem, check_modules

SKILL_ROOT = SRC.parent
BIN = SKILL_ROOT / "bin"


class ListRunner:
    """Runner that records calls and claims every command succeeds."""

    def __init__(self):
        self.calls = []

    def run(self, cmd):
        self.calls.append(list(cmd))
        return 0, ""


def test_wrapper_does_not_exec_loop_with_bin_on_path():
    """P1 regression: bin/kiac-slurm on PATH must not recurse into itself."""
    env = dict(os.environ)
    env["PATH"] = f"{BIN}:{env.get('PATH', '')}"
    proc = subprocess.run(
        ["kiac-slurm", "--version"], capture_output=True, text=True, timeout=15, env=env
    )
    assert proc.returncode == 0
    assert "kiac-slurm" in proc.stdout

    proc = subprocess.run(
        ["slurm-check", str(SKILL_ROOT / "tests" / "fixtures" / "manual_basic.sbatch")],
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert proc.returncode == 1  # manual example must fail, wrapper or not
    assert "SLURM021" in proc.stdout


def test_module_check_resolves_and_flags():
    script = parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\nmodule load python/3.11 cuda/12.1\n"
    )

    class ModRunner(ListRunner):
        def run(self, cmd):
            super().run(cmd)
            if cmd[:2] == ["bash", "-lc"] and "module show" in cmd[2]:
                # cmd[2] is "module show NAME >/dev/null 2>&1"
                name = cmd[2].split(">/dev/null")[0].split()[-1].strip("'\"")
                return (0, "") if name == "python/3.11" else (1, "error")
            if cmd[:3] == ["bash", "-lc", "type module"]:
                return 0, ""
            return 0, ""

    rep = Report()
    check_modules(script, rep, ModRunner())
    levels = [d.level for d in rep.items if d.rule_id == "MOD002"]
    assert levels == ["PASS", "WARN"]  # python resolves, cuda flags


def test_module_check_skips_when_modulecmd_absent():
    script = parse_text("#!/bin/bash\n#SBATCH --partition=a100\nmodule load python/3.11\n")

    class NoModRunner(ListRunner):
        def run(self, cmd):
            super().run(cmd)
            if cmd[:3] == ["bash", "-lc", "type module"]:
                return 1, "not found"
            return 0, ""

    rep = Report()
    check_modules(script, rep, NoModRunner())
    assert rep.rule_ids() == ["MOD001"]


def test_fs_invalid_workdir():
    script = parse_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=medium\n"
        "cd /definitely/not/a/real/path\n"
        "python3 task.py\n"
    )
    rep = Report()
    check_filesystem(script, rep)
    fs1 = [d for d in rep.items if d.rule_id == "FS001"]
    assert fs1 and fs1[0].level == "ERROR"
    assert fs1[0].line is not None


def test_runner_timeout_and_missing_command():
    runner = Runner(timeout=5)
    rc, out = runner.run(["__no_such_command_xyz__"])
    assert rc == 127
    assert "command not found" in out


def test_site_policy_flags():
    assert SITE.assume_storage_writable is False


def test_h200_generator_honors_gres_count():
    """Regression: --gres used to be silently ignored for the h200 template."""
    from kiac_slurm.generator import generate

    text = generate("h200", {"partition": "h200", "account": "chiru", "gres": "gpu:2"})
    assert "#SBATCH --gres=gpu:2" in text
    assert "#SBATCH --qos=h200_qos" not in text  # qos comes from the CLI layer

    default = generate("h200", {"partition": "h200", "account": "chiru"})
    assert "#SBATCH --gres=gpu:1" in default
