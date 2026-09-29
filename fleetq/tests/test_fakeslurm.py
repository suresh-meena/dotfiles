"""The fake Slurm itself: real processes, and the quirks fleetq must survive (§13.2)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(FAKES))
import fakeslurm  # noqa: E402

FORMAT = "%i|%T|%r|%N|%j"
ACCT = "JobID,JobName,State,ExitCode"


@pytest.fixture()
def slurm(tmp_path):
    state = tmp_path / "slurm"
    env = {**os.environ, "FAKE_SLURM_DIR": str(state), "PATH": f"{FAKES / 'bin'}:{os.environ['PATH']}"}

    def run(*argv, cwd=None):
        return subprocess.run(list(argv), env=env, cwd=cwd or tmp_path, capture_output=True, text=True, timeout=30)

    def script(body: str, *directives: str, name: str = "job.sh") -> Path:
        path = tmp_path / name
        path.write_text("#!/bin/bash\n" + "".join(f"#SBATCH {d}\n" for d in directives) + body + "\n")
        return path

    def submit(path: Path, *extra: str) -> int:
        res = run("sbatch", "--parsable", *extra, str(path))
        assert res.returncode == 0, res.stderr
        return int(res.stdout.strip())

    run.state, run.script, run.submit, run.tmp = state, script, submit, tmp_path
    state.mkdir()
    yield run
    fakeslurm.kill_all(state)


def squeue(slurm, *sel):
    res = slurm("squeue", "-h", *sel, "--states=all", "-o", FORMAT)
    return res, [line.split("|") for line in res.stdout.splitlines()]


def test_submit_runs_the_job_and_accounting_reports_the_exit(slurm):
    jid = slurm.submit(slurm.script("echo hi from $SLURM_JOB_ID", "--job-name=fq-a", "--output=out-%j.log"))
    fakeslurm.wait_for(slurm.state, jid, "COMPLETED")
    assert (slurm.tmp / f"out-{jid}.log").read_text() == f"hi from {jid}\n"
    acct = slurm("sacct", "-X", "-n", "-P", "-j", str(jid), "-S", "now-7days", f"--format={ACCT}")
    assert acct.stdout.strip() == f"{jid}|fq-a|COMPLETED|0:0"


def test_nonzero_exit_is_failed_with_its_code(slurm):
    jid = slurm.submit(slurm.script("exit 3", "--job-name=fq-b"))
    fakeslurm.wait_for(slurm.state, jid, "FAILED")
    acct = slurm("sacct", "-X", "-n", "-P", "-j", str(jid), f"--format={ACCT}")
    assert acct.stdout.strip().endswith("|FAILED|3:0")


def test_the_spooled_copy_runs_even_after_the_original_is_deleted(slurm):
    marker = slurm.tmp / "ran"
    path = slurm.script(f"sleep 0.3; touch {marker}", "--job-name=fq-c")
    fakeslurm.configure(slurm.state, start_delay_s=0.5)
    jid = slurm.submit(path)
    path.unlink()
    fakeslurm.wait_for(slurm.state, jid, "COMPLETED", timeout=10)
    assert marker.exists()


def test_job_environment_carries_slurm_variables(slurm):
    out = slurm.tmp / "env.txt"
    jid = slurm.submit(slurm.script(f'echo "$SLURM_JOB_ID $SLURM_RESTART_COUNT $SLURM_JOB_NAME" > {out}',
                                    "--job-name=fq-env"))
    fakeslurm.wait_for(slurm.state, jid, "COMPLETED")
    assert out.read_text().split() == [str(jid), "0", "fq-env"]


def test_command_line_options_override_directives(slurm):
    jid = slurm.submit(slurm.script("true", "--job-name=from-script", "--partition=gpu"), "--job-name=from-cli")
    _, rows = squeue(slurm, "-j", str(jid))
    assert rows[0][4] == "from-cli"


def test_directives_after_the_first_command_are_ignored(slurm):
    path = slurm.tmp / "late.sh"
    path.write_text("#!/bin/bash\n#SBATCH --job-name=early\necho x\n#SBATCH --job-name=late\n")
    jid = slurm.submit(path)
    _, rows = squeue(slurm, "-j", str(jid))
    assert rows[0][4] == "early"


def test_name_lookup_finds_duplicates(slurm):
    fakeslurm.configure(slurm.state, start_delay_s=60)
    a = slurm.submit(slurm.script("true", "--job-name=fq-dup"))
    b = slurm.submit(slurm.script("true", "--job-name=fq-dup"))
    slurm.submit(slurm.script("true", "--job-name=fq-dup-not"))
    _, rows = squeue(slurm, "-n", "fq-dup")
    assert [int(r[0]) for r in rows] == [a, b]
    assert {r[1] for r in rows} == {"PENDING"} and {r[2] for r in rows} == {"Priority"}


def test_finished_jobs_leave_squeue_after_min_job_age(slurm):
    fakeslurm.configure(slurm.state, min_job_age_s=1)
    jid = slurm.submit(slurm.script("true", "--job-name=fq-age"))
    fakeslurm.wait_for(slurm.state, jid, "COMPLETED")
    res, rows = squeue(slurm, "-j", str(jid))
    assert res.returncode == 0 and rows[0][1] == "COMPLETED"
    live = slurm("squeue", "-h", "-j", str(jid), "-o", FORMAT)
    assert live.stdout == "", "without --states=all a finished job is not listed"
    time.sleep(1.2)
    res, rows = squeue(slurm, "-j", str(jid))
    assert res.returncode == 1 and "Invalid job id specified" in res.stderr and rows == []
    acct = slurm("sacct", "-X", "-n", "-P", "-j", str(jid), f"--format={ACCT}")
    assert "COMPLETED" in acct.stdout, "accounting outlives MinJobAge"


def test_a_mixed_id_list_lists_the_known_ones(slurm):
    jid = slurm.submit(slurm.script("sleep 5", "--job-name=fq-mix"))
    res, rows = squeue(slurm, "-j", f"999999,{jid}")
    assert res.returncode == 0 and [int(r[0]) for r in rows] == [jid]


def test_accounting_can_be_off_or_lagging(slurm):
    fakeslurm.configure(slurm.state, accounting=False)
    off = slurm("sacct", "-X", "-n", "-P", "--name=x", f"--format={ACCT}")
    assert off.returncode == 1 and off.stderr.strip() == "sacct: error: Slurm accounting storage is disabled"
    fakeslurm.configure(slurm.state, sacct_lag_s=1.0)
    jid = slurm.submit(slurm.script("true", "--job-name=fq-lag"))
    young = slurm("sacct", "-X", "-n", "-P", "--name=fq-lag", f"--format={ACCT}")
    assert young.returncode == 0 and young.stdout == ""
    time.sleep(1.1)
    fakeslurm.wait_for(slurm.state, jid, "COMPLETED")
    later = slurm("sacct", "-X", "-n", "-P", "--name=fq-lag", f"--format={ACCT}")
    assert later.stdout.startswith(f"{jid}|fq-lag|")


def test_explicit_accounting_window_finds_a_job_missed_by_the_default(slurm):
    jid = slurm.submit(slurm.script("true", "--job-name=fq-old"))
    fakeslurm.wait_for(slurm.state, jid, "COMPLETED")
    with fakeslurm.session(slurm.state, advance=False) as (_, _, state):
        state["jobs"][str(jid)]["submit_time"] = time.time() - 2 * 86400
    default = slurm("sacct", "-X", "-n", "-P", "-j", str(jid), f"--format={ACCT}")
    explicit = slurm("sacct", "-X", "-n", "-P", "-j", str(jid),
                     "-S", "now-7days", f"--format={ACCT}")
    assert default.returncode == 0 and default.stdout == ""
    assert explicit.stdout.startswith(f"{jid}|fq-old|COMPLETED|")


def test_a_disallowed_account_is_accepted_then_pends_forever(slurm):
    fakeslurm.configure(slurm.state, accounts={"lab": {"partitions": ["gpu"]}})
    jid = slurm.submit(slurm.script("true", "--job-name=fq-acct", "--partition=gpu", "--account=other"))
    time.sleep(0.2)
    _, rows = squeue(slurm, "-j", str(jid))
    assert rows[0][1:3] == ["PENDING", "AccountNotAllowed"]
    wrong_part = slurm.submit(slurm.script("true", "--partition=debug", "--account=lab"))
    _, rows = squeue(slurm, "-j", str(wrong_part))
    assert rows[0][2] == "AccountNotAllowed"


def test_a_group_gpu_limit_pends_then_clears(slurm):
    fakeslurm.configure(slurm.state, accounts={"lab": {"partitions": ["gpu"], "grp_gpus": 1}})
    first = slurm.submit(slurm.script("sleep 1", "--partition=gpu", "--account=lab", "--gres=gpu:1"))
    second = slurm.submit(slurm.script("true", "--partition=gpu", "--account=lab", "--gres=gpu:a100:1"))
    _, rows = squeue(slurm, "-j", str(second))
    assert rows[0][1:3] == ["PENDING", "AssocGrpGRES"]
    fakeslurm.wait_for(slurm.state, first, "COMPLETED", timeout=10)
    fakeslurm.wait_for(slurm.state, second, "COMPLETED", timeout=10)


def test_over_the_partition_time_limit_pends_forever(slurm):
    fakeslurm.configure(slurm.state, partitions={"gpu": {"max_time_s": 3600}})
    jid = slurm.submit(slurm.script("true", "--partition=gpu", "--time=2:00:00"))
    _, rows = squeue(slurm, "-j", str(jid))
    assert rows[0][1:3] == ["PENDING", "PartitionTimeLimit"]


def test_an_invalid_partition_is_rejected(slurm):
    res = slurm("sbatch", "--parsable", str(slurm.script("true", "--partition=nope")))
    assert res.returncode == 1 and res.stdout == ""
    assert "Invalid partition name specified" in res.stderr
    assert fakeslurm.jobs(slurm.state) == []


def test_accept_then_fail_queues_a_job_the_caller_never_heard_about(slurm):
    fakeslurm.configure(slurm.state, faults={"accept_then_fail": 1})
    marker = slurm.tmp / "ran"
    res = slurm("sbatch", "--parsable", str(slurm.script(f"touch {marker}", "--job-name=fq-lost")))
    assert res.returncode == 1 and res.stdout == "" and "Socket timed out" in res.stderr
    _, rows = squeue(slurm, "-n", "fq-lost")
    assert len(rows) == 1
    fakeslurm.wait_for(slurm.state, int(rows[0][0]), "COMPLETED")
    assert marker.exists()
    again = slurm("sbatch", "--parsable", str(slurm.script("true", name="next.sh")))
    assert again.returncode == 0, "the fault fires only the configured number of times"


def test_scancel_pending_running_and_unknown(slurm):
    fakeslurm.configure(slurm.state, start_delay_s=60)
    pending = slurm.submit(slurm.script("true"))
    assert slurm("scancel", str(pending)).returncode == 0
    assert fakeslurm.wait_for(slurm.state, pending, "CANCELLED")["exit_code"] == "0:0"
    fakeslurm.configure(slurm.state, kill_wait_s=0.5)
    running = slurm.submit(slurm.script("trap '' TERM; sleep 30"))
    job = fakeslurm.wait_for(slurm.state, running, "RUNNING")
    assert slurm("scancel", str(running)).returncode == 0
    assert fakeslurm.wait_for(slurm.state, running, "CANCELLED")
    assert not fakeslurm._alive(job["pid"]), "the whole process group died, even ignoring TERM"
    acct = slurm("sacct", "-X", "-n", "-P", "-j", str(running), f"--format={ACCT}")
    assert f"|CANCELLED by {os.getuid()}|" in acct.stdout
    unknown = slurm("scancel", "424242")
    assert unknown.returncode == 1 and "Invalid job id specified" in unknown.stderr
    assert slurm("scancel", str(pending)).returncode == 0, "cancelling a finished job is silent"


def test_walltime_is_timeout(slurm):
    fakeslurm.configure(slurm.state, kill_wait_s=0.5)
    jid = slurm.submit(slurm.script("sleep 30", "--time=00:00:01"))
    job = fakeslurm.wait_for(slurm.state, jid, "RUNNING")
    time.sleep(1.2)
    done = fakeslurm.wait_for(slurm.state, jid, "TIMEOUT", timeout=5)
    assert done["exit_code"] == "0:15" and not fakeslurm._alive(job["pid"])


def test_requeue_reruns_the_spooled_script_with_a_restart_count(slurm):
    log = slurm.tmp / "runs.txt"
    jid = slurm.submit(slurm.script(f'echo "run $SLURM_RESTART_COUNT" >> {log}; sleep 30', "--no-requeue"))
    fakeslurm.wait_for(slurm.state, jid, "RUNNING")
    deadline = time.monotonic() + 5
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    fakeslurm.requeue(slurm.state, jid)
    deadline = time.monotonic() + 5
    while log.read_text().count("run") < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert log.read_text().split("\n")[:2] == ["run 0", "run 1"]
    assert fakeslurm.jobs(slurm.state)[0]["restart"] == 1


def test_signal_b_reaches_only_the_batch_shell_before_the_end(slurm):
    fakeslurm.configure(slurm.state, kill_wait_s=0.5)
    got = slurm.tmp / "got"
    body = f"trap 'echo warned > {got}' USR1\nsleep 30 &\nwait $!\nwait $!\n"
    jid = slurm.submit(slurm.script(body, "--time=00:00:03", "--signal=B:USR1@2"))
    fakeslurm.wait_for(slurm.state, jid, "RUNNING")
    deadline = time.monotonic() + 5
    while not got.exists() and time.monotonic() < deadline:
        fakeslurm.advance_now(slurm.state)
        time.sleep(0.05)
    assert got.read_text() == "warned\n"
    assert fakeslurm.wait_for(slurm.state, jid, "TIMEOUT", timeout=10)


def test_a_setsid_payload_still_dies_with_its_job(slurm):
    fakeslurm.configure(slurm.state, kill_wait_s=0.3)
    pidfile = slurm.tmp / "pid"
    jid = slurm.submit(slurm.script(f"setsid sh -c 'echo $$ > {pidfile}; sleep 30' & wait"))
    fakeslurm.wait_for(slurm.state, jid, "RUNNING")
    deadline = time.monotonic() + 5
    while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    escaped = int(pidfile.read_text())
    assert slurm("scancel", str(jid)).returncode == 0
    fakeslurm.wait_for(slurm.state, jid, "CANCELLED")
    deadline = time.monotonic() + 3
    while fakeslurm._alive(escaped) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not fakeslurm._alive(escaped), "a cgroup holds setsid'd processes too"
