"""End to end on a cluster: bin/fq -> HTTP -> fleetqd -> SlurmExecutor -> fleetctl(fake) -> sbatch(fake).

Everything fleetq ships runs for real: the submit-once wrapper, the one-session
observer, the cancel tombstone, and the batch script inside the "allocation"
(bundle integrity check, safe extraction, runner-entry claim, result file). Only
the SSH hop and Slurm itself are faked, and the fake Slurm runs real processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from fleetq import auth
from fleetq.config import DaemonConfig, NodeConfig

from test_e2e_bare import FAKES, FQ, Server, _loopback_ok

sys.path.insert(0, str(FAKES.parent))
import fakeslurm

pytestmark = pytest.mark.skipif(not _loopback_ok(), reason="needs loopback (run via scripts/test)")

SITE = {"default_queue": "gpu", "accounting": True, "preflight": True,
        "queues": {"gpu": {"partition": "gpu", "account": "lab", "default_time_s": 600,
                           "default_mem_mb": 1000, "default_cpus": 1, "max_gpus": 4}}}


class Cluster:
    """One fake login node 'clus', a shared control root, and a fake Slurm behind it."""

    def __init__(self, tmp: Path, monkeypatch) -> None:
        self.tmp = tmp
        self.fleet_root = tmp / "fleet"
        self.sandbox = self.fleet_root / "clus"
        self.control_root = tmp / "clus-root"
        self.slurm = tmp / "slurm"
        for d in (self.sandbox, self.slurm, self.control_root):
            d.mkdir(parents=True)
        (self.sandbox / "env.json").write_text(json.dumps({
            "PATH": f"{FAKES}:/usr/bin:/bin", "FAKE_SLURM_DIR": str(self.slurm)}))
        monkeypatch.setenv("FAKE_FLEET_ROOT", str(self.fleet_root))

    def faults(self, **kw) -> None:
        (self.sandbox / "faults.json").write_text(json.dumps(kw))

    def calls(self) -> list[dict]:
        path = self.fleet_root / "calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def submit_wrapper_calls(self) -> int:
        return sum(1 for c in self.calls() if c["argv"][0] == "exec" and any("sbatch --parsable" in a for a in c["argv"]))

    def jobs(self) -> list[dict]:
        return fakeslurm.jobs(self.slurm)

    def attempt_dirs(self) -> list[Path]:
        root = self.control_root / "attempts"
        return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


@pytest.fixture()
def make_env(tmp_path, monkeypatch):
    started: list = []

    def make(site: dict | None = None, **controller):
        cluster = Cluster(tmp_path, monkeypatch)
        state = tmp_path / "state"
        state.mkdir()
        (state / ".fleetq-volume").write_text("test\n")
        node = NodeConfig(id="clus", backend="slurm", enabled=True, control_root=str(cluster.control_root),
                          site=site or SITE, caps={"gpus": 4, "jobs": 10},
                          budget={"monitor_per_minute": 6000, "action_per_minute": 6000,
                                  "transfer_per_minute": 6000, "burst": 1000,
                                  "max_sessions_per_operation": 100,
                                  "max_bytes_per_transfer": 1073741824,
                                  "sessions_per_minute": 6000, "sessions_burst": 1000,
                                  "bytes_per_minute": 1073741824, "bytes_burst": 1073741824,
                                  "action_session_reserve": 100})
        cfg = DaemonConfig(state_dir=state, fleetctl=FAKES / "fleetctl", dev_mode=True, nodes=[node],
                           controller={"tick_s": 0.2, "slurm_observe_fast_s": 0.3, "slurm_observe_slow_s": 0.3,
                                       "post_job_cooldown_s": 0, "refusal_backoff_s": 0, "stage_backoff_s": 0,
                                       "artifact_backoff_s": 0.2, "retry_backoff_s": 0,
                                       "collect_max_bytes": 16 * 1024 * 1024,
                                       "collect_max_files": 1000, **controller})
        from fleetq.db.store import Store
        from fleetq.engine import fence
        tmp_store = Store(cfg.db_path)
        tmp_store.open()
        tmp_store.run_sync(lambda c: fence.start_controller(c))
        _, token = tmp_store.run_sync(lambda c: auth.create_token(c, owner="suresh", kind="human", label="laptop",
                                                                   allow_clusters=True))
        tmp_store.close()
        token_file = tmp_path / "token"
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        server = Server(cfg).start()
        started.append(server)
        project = tmp_path / "proj"
        project.mkdir()
        (project / "train.py").write_text("import os\nprint('trained in', os.environ['SLURM_JOB_ID'])\n")
        (project / ".env").write_text("SECRET=hunter2\n")
        return {"cluster": cluster, "server": server, "token_file": token_file, "token": token,
                "project": project, "tmp": tmp_path}

    yield make
    for server in started:
        server.stop()
    fakeslurm.kill_all(tmp_path / "slurm")


def fq(env, *args, timeout=120):
    proc = subprocess.run([sys.executable, str(FQ), "--json", *args], capture_output=True, text=True,
                          cwd=env["project"], timeout=timeout,
                          env={**os.environ, "FQ_URL": f"http://127.0.0.1:{env['server'].port}",
                               "FQ_TOKEN_FILE": str(env["token_file"]),
                               "XDG_STATE_HOME": str(env["tmp"] / "xdg-state"),
                               "XDG_CACHE_HOME": str(env["tmp"] / "xdg-cache")})
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else {}), proc.stderr


def job(env, job_id: int) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{env['server'].port}/api/v1/jobs/{job_id}",
                                 headers={"Authorization": f"Bearer {env['token']}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())["job"]


def until(predicate, timeout=30.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def submit(env, key, *cmd, wait=True, extra=()):
    args = ["submit", "--on", "clus", "--gpus", "1", "--idempotency-key", key, *extra]
    if wait:
        args += ["--wait", "--timeout", "60s"]
    return fq(env, *args, "--", *cmd)


# ---- the happy path and ordinary failures ----------------------------------------------


def test_snapshot_job_runs_on_the_cluster_once(make_env):
    env = make_env()
    rc, out, err = submit(env, "s1", "python3", "train.py")
    assert rc == 0, (out.get("job"), err)
    assert out["job"]["execution"]["outcome"] == "COMPLETED"
    jobs = env["cluster"].jobs()
    assert len(jobs) == 1 and jobs[0]["state"] == "COMPLETED" and jobs[0]["name"].startswith("fq-att")
    assert jobs[0]["account"] == "lab" and jobs[0]["partition"] == "gpu" and jobs[0]["gpus"] == 1
    (adir,) = env["cluster"].attempt_dirs()
    assert json.loads((adir / "result.json").read_text())["exit_code"] == 0
    assert f"trained in {jobs[0]['id']}" in (adir / "logs" / f"slurm-{jobs[0]['id']}.out").read_text()
    code = adir / "code"
    assert (code / "train.py").exists() and not (code / ".env").exists()
    assert (adir / "receipt").read_text() == str(jobs[0]["id"])
    assert env["cluster"].submit_wrapper_calls() == 1
    remote_calls = [c for c in env["cluster"].calls() if c["argv"][0] in {"exec", "sync"}]
    assert remote_calls and all(c["argv"][c["argv"].index("--expected-role") + 1] == "login"
                                for c in remote_calls)


def test_payload_failure_is_failed_with_its_exit_code(make_env):
    env = make_env()
    rc, out, _ = submit(env, "s2", "/bin/sh", "-c", "exit 7")
    assert rc == 1
    assert out["job"]["execution"]["outcome"] == "FAILED" and out["job"]["execution"]["exit"]["code"] == 7


def test_every_cluster_call_is_budgeted_admin_with_an_op_class(make_env):
    env = make_env()
    submit(env, "s3", "true")
    execs = [c["argv"] for c in env["cluster"].calls() if c["argv"][0] == "exec"]
    assert execs and all("--admin" in a and "--op-class" in a for a in execs)
    classes = {a[a.index("--op-class") + 1] for a in execs}
    assert classes <= {"monitor", "action"}


# ---- submission uncertainty: never a second sbatch -------------------------------------


def test_reply_lost_after_sbatch_is_adopted_from_the_receipt(make_env):
    env = make_env()
    env["cluster"].faults(drop_once_on_script="sbatch --parsable")
    runs = env["tmp"] / "runs.txt"
    rc, out, err = submit(env, "u1", "/bin/sh", "-c", f"echo run >> {runs}")
    assert rc == 0, (out, err)
    assert out["job"]["execution"]["outcome"] == "COMPLETED" and out["job"]["attempts"] == 1
    assert (env["cluster"].sandbox / "dropped_once").exists(), "the fault fired: the submit reply was lost"
    (adir,) = env["cluster"].attempt_dirs()
    assert (adir / "receipt").exists(), "the wrapper persisted the receipt before the reply was lost"
    assert runs.read_text().count("run") == 1
    assert len(env["cluster"].jobs()) == 1
    assert env["cluster"].submit_wrapper_calls() == 1, "an uncertain submission is observed, never resubmitted"


def test_sbatch_that_failed_but_queued_the_job_is_found_by_its_name(make_env):
    env = make_env()
    fakeslurm.configure(env["cluster"].slurm, faults={"accept_then_fail": 1})
    runs = env["tmp"] / "runs.txt"
    rc, out, err = submit(env, "u2", "/bin/sh", "-c", f"echo run >> {runs}")
    assert rc == 0, (out, err)
    assert out["job"]["execution"]["outcome"] == "COMPLETED"
    (adir,) = env["cluster"].attempt_dirs()
    assert not (adir / "receipt").exists(), "sbatch never gave a receipt; the name lookup found the job"
    assert len(env["cluster"].jobs()) == 1 and runs.read_text().count("run") == 1
    assert env["cluster"].submit_wrapper_calls() == 1


def test_submit_timeout_with_remote_sbatch_still_running_adopts_the_job(make_env):
    env = make_env()
    # fleetctl returns a timeout after starting the remote submit wrapper. The
    # wrapper is left running while fake sbatch holds its reply after accepting
    # the job, so the controller must reconcile the uncertain attempt by name.
    env["cluster"].faults(timeout_detach_on_script="sbatch --parsable")
    fakeslurm.configure(env["cluster"].slurm, faults={"reply_delay_s": 1.5})
    runs = env["tmp"] / "runs-timeout.txt"
    rc, out, err = submit(env, "u-timeout", "/bin/sh", "-c", f"echo run >> {runs}", wait=False)
    assert rc == 0, (out, err)
    jid = out["jobs"][0]

    uncertain = until(lambda: (j := job(env, jid))["phase"] == "SUBMISSION_UNKNOWN" and j,
                      what="SUBMISSION_UNKNOWN")
    (adir,) = env["cluster"].attempt_dirs()
    assert (env["cluster"].sandbox / "detached_once").exists(), "the timed-out remote command kept running"
    assert until(lambda: env["cluster"].jobs(), what="sbatch acceptance"), "sbatch accepted the job before its delayed reply"
    assert not (adir / "receipt").exists(), "reconciliation starts before the delayed receipt arrives"

    adopted = until(lambda: (j := job(env, jid))["execution"]["outcome"] == "COMPLETED" and j,
                    what="adopted job completion")
    assert uncertain["phase"] == "SUBMISSION_UNKNOWN"
    assert adopted["attempts"] == 1
    assert until(lambda: (adir / "receipt").exists(), what="delayed submit receipt")
    assert (adir / "receipt").read_text() == str(env["cluster"].jobs()[0]["id"])
    assert runs.read_text().count("run") == 1
    assert len(env["cluster"].jobs()) == 1
    assert env["cluster"].submit_wrapper_calls() == 1


def test_a_rejected_submission_blocks_without_retrying(make_env):
    env = make_env()
    fakeslurm.configure(env["cluster"].slurm, partitions={"debug": {}})
    rc, out, _ = submit(env, "u3", "true", wait=False)
    jid = out["jobs"][0]
    blocked = until(lambda: (j := job(env, jid))["phase"] == "BLOCKED" and j, what="BLOCKED")
    assert "sbatch_rejected" in blocked["reason"]
    time.sleep(1.0)
    assert env["cluster"].submit_wrapper_calls() == 1 and env["cluster"].jobs() == []


def test_preflight_errors_refuse_before_any_submission(make_env):
    env = make_env()
    env["cluster"].faults(preflight_errors=["KIAC033"])
    rc, out, _ = submit(env, "u4", "true", wait=False)
    jid = out["jobs"][0]
    blocked = until(lambda: (j := job(env, jid))["phase"] in ("BLOCKED", "TERMINAL") and j, what="refusal")
    assert "KIAC033" in (blocked["reason"] or json.dumps(blocked))
    assert env["cluster"].submit_wrapper_calls() == 0 and env["cluster"].jobs() == []


# ---- pending, cancellation, requeue ------------------------------------------------------


def test_a_request_the_site_never_runs_is_reported_and_kept_single(make_env):
    env = make_env()
    fakeslurm.configure(env["cluster"].slurm, accounts={"other": {"partitions": ["gpu"]}})
    rc, out, _ = submit(env, "p1", "true", wait=False)
    jid = out["jobs"][0]
    pending = until(lambda: (j := job(env, jid))["reason"] == "slurm: AccountNotAllowed" and j, what="reason")
    assert pending["phase"] == "SUBMITTED"
    time.sleep(1.0)
    assert len(env["cluster"].jobs()) == 1 and env["cluster"].submit_wrapper_calls() == 1
    rc, _, _ = fq(env, "cancel", str(jid))
    assert rc == 0
    until(lambda: job(env, jid)["phase"] == "TERMINAL", what="cancel")
    assert env["cluster"].jobs()[0]["state"] == "CANCELLED"


def test_cancel_while_pending_never_runs_the_payload(make_env):
    env = make_env()
    fakeslurm.configure(env["cluster"].slurm, start_delay_s=3600)
    marker = env["tmp"] / "ran"
    rc, out, _ = submit(env, "c1", "touch", str(marker), wait=False)
    jid = out["jobs"][0]
    until(lambda: job(env, jid)["phase"] == "SUBMITTED", what="SUBMITTED")
    assert fq(env, "cancel", str(jid))[0] == 0
    done = until(lambda: (j := job(env, jid))["phase"] == "TERMINAL" and j, what="TERMINAL")
    assert done["execution"]["outcome"] == "CANCELLED" and not marker.exists()
    (adir,) = env["cluster"].attempt_dirs()
    assert (adir / "cancel").exists() and not (adir / "runner_entered").exists()
    assert env["cluster"].jobs()[0]["state"] == "CANCELLED"


def test_cancel_while_running_kills_the_job(make_env):
    env = make_env()
    fakeslurm.configure(env["cluster"].slurm, kill_wait_s=0.5)
    started = env["tmp"] / "started"
    rc, out, _ = submit(env, "c2", "/bin/sh", "-c", f"touch {started}; sleep 60", wait=False)
    jid = out["jobs"][0]
    until(lambda: started.exists() and job(env, jid)["phase"] == "RUNNING", what="RUNNING")
    pid = env["cluster"].jobs()[0]["pid"]
    assert fq(env, "cancel", str(jid))[0] == 0
    done = until(lambda: (j := job(env, jid))["phase"] == "TERMINAL" and j, what="TERMINAL")
    assert done["execution"]["outcome"] == "CANCELLED"
    assert not fakeslurm._alive(pid)


def test_a_scheduler_requeue_never_reruns_the_payload(make_env):
    env = make_env()
    runs = env["tmp"] / "runs.txt"
    rc, out, _ = submit(env, "r1", "/bin/sh", "-c", f"echo run >> {runs}; sleep 60", wait=False)
    jid = out["jobs"][0]
    until(lambda: runs.exists() and job(env, jid)["phase"] == "RUNNING", what="RUNNING")
    slurm_id = env["cluster"].jobs()[0]["id"]
    fakeslurm.requeue(env["cluster"].slurm, slurm_id)
    done = until(lambda: (j := job(env, jid))["phase"] == "TERMINAL" and j, what="TERMINAL")
    assert runs.read_text().count("run") == 1
    (adir,) = env["cluster"].attempt_dirs()
    assert list(adir.glob("replay.*")), "the restarted batch script saw the runner claim and stopped"
    # The payload was killed before it reported: a clean scheduler exit is not success.
    assert done["execution"]["outcome"] == "UNKNOWN_EXIT" and done["success"] is False


# ---- evidence when the scheduler has forgotten ----------------------------------------


def test_no_accounting_and_min_job_age_zero_resolves_from_the_result_file(make_env):
    site = {**SITE, "accounting": False}
    env = make_env(site=site)
    fakeslurm.configure(env["cluster"].slurm, accounting=False, min_job_age_s=0)
    rc, out, err = submit(env, "e1", "/bin/sh", "-c", "exit 4")
    assert rc == 1, (out, err)
    assert out["job"]["execution"]["outcome"] == "FAILED" and out["job"]["execution"]["exit"]["code"] == 4


def test_nothing_live_means_no_cluster_traffic(make_env):
    env = make_env()
    rc, out, _ = submit(env, "q1", "true")
    assert rc == 0, out

    def observations():
        return sum(1 for c in env["cluster"].calls() if any("__SQUEUE_BEGIN" in a for a in c["argv"]))
    settled = observations()
    time.sleep(1.5)                                  # five fast cycles at the test cadence
    assert observations() == settled, "a finished job must not keep the login node busy"


def test_cluster_logs_arrive_after_the_job_and_reads_cost_nothing(make_env):
    import base64
    env = make_env()
    rc, out, _ = submit(env, "l1", "python3", "train.py")
    assert rc == 0, out
    jid = out["job"]["id"]

    def logs(*extra):
        code, body, err = fq(env, "logs", str(jid), *extra)
        assert code == 0, (body, err)
        return body, base64.b64decode(body["data_b64"])
    until(lambda: logs()[0]["complete"], what="final log tail")
    before = len(env["cluster"].calls())
    slurm_id = env["cluster"].jobs()[0]["id"]
    assert logs()[1] == f"trained in {slurm_id}\n".encode()
    assert logs("--err")[0]["complete"]
    time.sleep(0.5)
    assert len(env["cluster"].calls()) == before, "a log read is a cache read"


def test_cluster_outputs_are_staged_pulled_and_published(make_env):
    env = make_env()
    rc, out, err = submit(env, "a1", "/bin/sh", "-c",
                          "mkdir -p logs out && echo ckpt > out/ckpt.pt && echo 'x y' > 'logs/run 1.txt'",
                          extra=("--collect", "out", "--collect", "logs"))
    assert rc == 0, (out, err)
    assert out["job"]["artifacts"]["state"] == "COMPLETE" and out["job"]["success"] is True
    dest = env["tmp"] / "fetched"
    rc, got, err = fq(env, "fetch", str(out["job"]["id"]), "-o", str(dest))
    assert rc == 0, (got, err)
    # `logs/` is one of fleetctl's default sync excludes; numbered slots keep it from being dropped.
    assert (dest / "logs" / "run 1.txt").read_text() == "x y\n" and (dest / "out" / "ckpt.pt").read_text() == "ckpt\n"
    classes = [c["argv"][c["argv"].index("--op-class") + 1] for c in env["cluster"].calls()
               if c["argv"][0] == "exec" and "--op-class" in c["argv"]]
    pulls = [c for c in env["cluster"].calls() if c["argv"][:2] == ["sync", "pull"]]
    assert len(pulls) == 1 and "--admin" in pulls[0]["argv"]
    assert "action" in classes


def test_cluster_array_members_each_see_their_own_index(make_env):
    env = make_env()
    out_dir = env["tmp"] / "idx"
    out_dir.mkdir()
    rc, out, err = fq(env, "submit", "--on", "clus", "--gpus", "1", "--array", "4,7", "--idempotency-key", "arr",
                      "--", "/bin/sh", "-c", f'touch {out_dir}/task-"$FQ_ARRAY_TASK_ID"-"${{SLURM_ARRAY_TASK_ID:-none}}"')
    assert rc == 0, (out, err)
    rc, _, err = fq(env, "wait", *map(str, out["jobs"]), "--timeout", "60s")
    assert rc == 0, err
    assert sorted(p.name for p in out_dir.iterdir()) == ["task-4-none", "task-7-none"]


def test_a_cluster_job_is_warned_by_slurm_times_out_and_resumes(make_env):
    from test_e2e_bare import RESUMABLE
    env = make_env()
    fakeslurm.configure(env["cluster"].slurm, kill_wait_s=0.5)
    log = env["tmp"] / "resume.log"
    (env["project"] / "train.sh").write_text(RESUMABLE)
    rc, out, err = fq(env, "submit", "--on", "clus", "--gpus", "1", "--time", "4s", "--warn-before", "2s",
                      "--resume", "2", "--env", f"LOG={log}", "--idempotency-key", "resume",
                      "--wait", "--timeout", "90s", "--", "/bin/sh", "train.sh")
    assert rc == 0, (out.get("job", {}).get("execution"), log.read_text() if log.exists() else "")
    assert log.read_text().splitlines() == ["attempt=1 resumed=0", "warned", "attempt=2 resumed=1",
                                            "resumed from step-41"]
    jobs = env["cluster"].jobs()
    assert [j["state"] for j in jobs] == ["TIMEOUT", "COMPLETED"]
    assert jobs[0]["signal"] == {"batch": True, "name": "USR1", "before_s": 2}, "asked Slurm for --signal=B:USR1@2"
