"""High-value fake Slurm P2 fault checks (§13.2); no remote access."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(FAKES))
import fakeslurm  # noqa: E402

FORMAT = "%i|%T|%r|%N|%j"


@pytest.fixture()
def slurm(tmp_path):
    state = tmp_path / "slurm"
    state.mkdir()
    env = {**os.environ, "FAKE_SLURM_DIR": str(state),
           "PATH": f"{FAKES / 'bin'}:{os.environ['PATH']}"}

    def run(*argv):
        return subprocess.run(list(argv), env=env, cwd=tmp_path, capture_output=True,
                              text=True, timeout=30)

    def submit(name="fq-matrix"):
        script = tmp_path / f"{name}.sh"
        script.write_text("#!/bin/bash\ntrue\n")
        result = run("sbatch", "--parsable", "--job-name=" + name, str(script))
        assert result.returncode == 0, result.stderr
        return int(result.stdout.strip())

    run.state, run.submit = state, submit
    yield run
    fakeslurm.kill_all(state)


def test_queue_disappearance_is_a_false_negative_while_job_stays_live(slurm):
    fakeslurm.configure(slurm.state, start_delay_s=60)
    jid = slurm.submit()
    fakeslurm.configure(slurm.state, start_delay_s=60, faults={"squeue_hidden_ids": [jid]})

    hidden = slurm("squeue", "-h", "-j", str(jid), "-o", FORMAT)
    assert hidden.returncode == 0 and hidden.stdout == ""
    assert fakeslurm.jobs(slurm.state)[0]["state"] == "PENDING"

    fakeslurm.configure(slurm.state, start_delay_s=60)
    visible = slurm("squeue", "-h", "-j", str(jid), "-o", FORMAT)
    assert visible.returncode == 0 and visible.stdout.startswith(f"{jid}|PENDING|")


@pytest.mark.parametrize("state", ["CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED",
                                    "REQUEUE_HOLD", "REQUEUE_FED", "RESIZING", "SIGNALING", "STAGE_OUT"])
def test_live_transitional_states_remain_visible_in_default_queue(slurm, state):
    fakeslurm.configure(slurm.state, start_delay_s=60)
    jid = slurm.submit()
    fakeslurm.set_state(slurm.state, jid, state)

    result = slurm("squeue", "-h", "-j", str(jid), "-o", FORMAT)
    assert result.returncode == 0 and result.stdout.startswith(f"{jid}|{state}|")
