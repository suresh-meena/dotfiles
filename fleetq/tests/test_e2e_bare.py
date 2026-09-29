"""End to end: bin/fq -> HTTP -> fleetqd -> BareExecutor -> fleetctl(fake) -> fq-node -> systemd(fake) -> payload.

Only fleetctl's SSH hop and systemd/nvidia-smi are faked; everything fleetq
ships runs for real, including the snapshot bundle, its upload and validation,
safe extraction on the "node", the launch gate, and the runner claim.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fleetq import auth
from fleetq.api.app import create_app
from fleetq.cli import build_runtime
from fleetq.config import DaemonConfig, NodeConfig

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes" / "bin"
FQ = ROOT / "bin" / "fq"
SHIM = ROOT / "build" / "fq-node"


def _loopback_ok() -> bool:
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _loopback_ok(), reason="needs loopback (run via scripts/test)")


@pytest.fixture(scope="module", autouse=True)
def built():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py")], check=True, capture_output=True)


class Fleet:
    """One fake workstation 'ws1' reachable through the fake fleetctl."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.fleet_root = tmp / "fleet"
        self.sandbox = self.fleet_root / "ws1"
        self.control_root = tmp / "ws1-root"
        for d in (self.sandbox, tmp / "sysd", tmp / "cg", tmp / "run"):
            d.mkdir(parents=True)
        (tmp / "run").chmod(0o700)
        (tmp / "meminfo").write_text("MemTotal: 131072000 kB\nMemAvailable: 65536000 kB\n")
        (tmp / "nvsmi.json").write_text(json.dumps({"gpus": [
            {"uuid": "GPU-aaaa", "index": 0, "name": "RTX 4090", "total": 24564, "used": 0, "util": 0},
            {"uuid": "GPU-bbbb", "index": 1, "name": "RTX 4090", "total": 24564, "used": 0, "util": 0}],
            "apps": []}))
        # The user manager's delegated controllers, as the probe reads them.
        uid = os.getuid()
        delegated = tmp / "cg" / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
        delegated.mkdir(parents=True)
        (delegated / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
        self.node_env = {
            "PATH": f"{FAKES}:/usr/bin:/bin", "FQ_NODE_PATH": f"{FAKES}:/usr/bin:/bin",
            "FAKE_SYSTEMD_DIR": str(tmp / "sysd"), "FQ_NODE_CGROUP_ROOT": str(tmp / "cg"),
            "FQ_NODE_RUNTIME_DIR": str(tmp / "run"), "FAKE_NVSMI_FILE": str(tmp / "nvsmi.json"),
            "FQ_NODE_MEMINFO": str(tmp / "meminfo"), "FQ_NODE_BOOT_ID": "boot-1",
            "FQ_NODE_GATE_INTERVAL": "0.01", "FQ_NODE_NVSMI_TIMEOUT": "2",
        }
        (self.sandbox / "env.json").write_text(json.dumps(self.node_env))
        self.control_root.mkdir()
        os.environ["FAKE_FLEET_ROOT"] = str(self.fleet_root)

    def faults(self, **kw) -> None:
        (self.sandbox / "faults.json").write_text(json.dumps(kw))

    def calls(self) -> list[dict]:
        path = self.fleet_root / "calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class Server:
    def __init__(self, cfg: DaemonConfig) -> None:
        self.cfg = cfg
        self.port = None
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import uvicorn
        asyncio.set_event_loop(self.loop)

        async def main():
            self.store, self.controller, rt = build_runtime(self.cfg)
            app = create_app(rt)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
            self.server = server
            ctl = asyncio.create_task(self.controller.run_forever())
            self.ready.set()
            await server.serve(sockets=[sock])
            await self.controller.stop()
            ctl.cancel()
            await asyncio.gather(ctl, return_exceptions=True)
        self.loop.run_until_complete(main())

    def start(self) -> "Server":
        self.thread.start()
        self.ready.wait(20)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(20)
        self.store.close()


@pytest.fixture()
def env(tmp_path):
    fleet = Fleet(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / ".fleetq-volume").write_text("test\n")
    node = NodeConfig(id="ws1", backend="bare", enabled=True, mode="exclusive",
                      control_root=str(fleet.control_root),
                      capacity={"ram_budget_mb": 32000, "cpus": 16, "scratch_budget_mb": 50000, "job_slots": 4,
                                "gpu_count": 2, "max_vram_mb": 24000, "mem_headroom_mb": 100},
                      defaults={"mem_mb": 1000, "cpus": 1, "time_s": 300})
    cfg = DaemonConfig(state_dir=state, fleetctl=FAKES / "fleetctl", dev_mode=True, nodes=[node],
                       controller={"tick_s": 0.2, "observe_interval_s": 0.2, "post_job_cooldown_s": 0,
                                   "refusal_backoff_s": 0, "stage_backoff_s": 0, "artifact_backoff_s": 0.2,
                                   "retry_backoff_s": 0})
    # Onboard exactly as an operator would (`fleetqd node install ws1`): push and
    # activate the shim, enroll it to this fleet, probe it into node_gpus.
    from fleetq import onboard
    from fleetq.cli import open_store
    from fleetq.transport.fleetctl import Fleetctl
    tmp_store = open_store(cfg)
    token_id, token = tmp_store.run_sync(lambda c: auth.create_token(c, owner="suresh", kind="human", label="laptop"))
    report = asyncio.run(onboard.install(cfg, tmp_store, Fleetctl(FAKES / "fleetctl"), "ws1"))
    tmp_store.close()
    assert report["probe"]["fit"], report
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    server = Server(cfg).start()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "train.py").write_text("import os\nprint('trained on', os.environ['CUDA_VISIBLE_DEVICES'])\n")
    (project / ".env").write_text("SECRET=hunter2\n")        # must never be uploaded
    yield {"fleet": fleet, "server": server, "token_file": token_file, "project": project, "tmp": tmp_path}
    server.stop()


def fq(env, *args, timeout=120):
    proc = subprocess.run([sys.executable, str(FQ), "--json", *args], capture_output=True, text=True,
                          cwd=env["project"], timeout=timeout,
                          env={**os.environ, "FQ_URL": f"http://127.0.0.1:{env['server'].port}",
                               "FQ_TOKEN_FILE": str(env["token_file"]),
                               "XDG_STATE_HOME": str(env["tmp"] / "xdg-state"),
                               "XDG_CACHE_HOME": str(env["tmp"] / "xdg-cache")})
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else {}), proc.stderr


def test_snapshot_job_runs_end_to_end(env):
    rc, out, err = fq(env, "submit", "--gpus", "1", "--on", "ws1", "--wait", "--timeout", "90s",
                      "--idempotency-key", "e2e-1", "--", "python3", "train.py")
    assert rc == 0, (out, err)
    job = out["job"]
    assert job["terminal"] and job["success"] and job["execution"]["outcome"] == "COMPLETED"
    assert job["placement"]["target"] == "ws1" and len(job["placement"]["gpus"]) == 1
    root = env["fleet"].control_root
    attempt_dirs = list((root / "attempts").iterdir())
    assert len(attempt_dirs) == 1
    stdout = (attempt_dirs[0] / "stdout.log").read_text()
    assert "trained on GPU-" in stdout                          # pinned by UUID, not index
    code = attempt_dirs[0] / "code"
    assert (code / "train.py").exists() and not (code / ".env").exists()   # secrets never shipped
    # Replaying the same idempotency key returns the same job, runs nothing new.
    rc2, out2, _ = fq(env, "submit", "--gpus", "1", "--on", "ws1", "--idempotency-key", "e2e-1",
                      "--", "python3", "train.py")
    assert rc2 == 0 and out2["idempotent_replay"] is True and out2["jobs"] == [job["id"]]
    assert len(list((root / "attempts").iterdir())) == 1


def test_lost_launch_reply_is_adopted_not_rerun(env):
    env["fleet"].faults(drop_once_on="launch")
    counter = env["project"] / "count.sh"
    counter.write_text(f"echo run >> {env['tmp']}/runs.txt\n")
    rc, out, err = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "90s",
                      "--idempotency-key", "e2e-2", "--", "/bin/sh", "count.sh")
    assert rc == 0, (out, err)
    assert out["job"]["execution"]["outcome"] == "COMPLETED"
    assert (env["tmp"] / "runs.txt").read_text().count("run") == 1   # executed exactly once
    assert out["job"]["attempts"] == 1


def test_failed_job_exit_code_and_cli_exit(env):
    rc, out, _ = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "90s",
                    "--idempotency-key", "e2e-3", "--", "/bin/sh", "-c", "exit 7")
    assert rc == 1
    assert out["job"]["execution"]["outcome"] == "FAILED" and out["job"]["execution"]["exit"]["code"] == 7


def test_unreachable_node_keeps_job_pending_and_cli_times_out_without_cancelling(env):
    env["fleet"].faults(unreachable=True)
    rc, out, _ = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "3s",
                    "--idempotency-key", "e2e-4", "--", "true")
    assert rc == 4                                   # wait timed out; job still active
    assert out["job"]["terminal"] is False and out["job"]["desired_state"] == "RUN"
    env["fleet"].faults()
    rc, out, _ = fq(env, "wait", str(out["job"]["id"]), "--timeout", "60s")
    assert rc == 0 and out["job"]["success"] is True


def test_every_transport_call_names_its_caller(env):
    fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "60s", "--idempotency-key", "e2e-5",
       "--", "true")
    calls = env["fleet"].calls()
    assert calls and all(c["caller"] == "fleetq" for c in calls)
    assert all("--json" in c["argv"] and "--timeout" in c["argv"] for c in calls)
    assert all(c["argv"][c["argv"].index("--expected-role") + 1] == "workstation" for c in calls)
    # The local fleetctl launcher caches bytecode. The remote fq-node launcher
    # writes none before the shim has validated its configured control root.
    assert list((env["tmp"] / "state" / "pycache").rglob("*.pyc")), "fleetctl bytecode was not cached"
    assert not (env["fleet"].control_root / "pycache").exists()


def _logs(env, job_id, *extra):
    import base64
    rc, out, err = fq(env, "logs", str(job_id), *extra)
    assert rc == 0, (out, err)
    return out, base64.b64decode(out["data_b64"])


def test_logs_are_served_from_the_cache_without_remote_calls(env):
    rc, out, _ = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "60s",
                    "--idempotency-key", "e2e-logs", "--", "/bin/sh", "-c", "echo out-line; echo err-line >&2")
    assert rc == 0, out
    jid = out["job"]["id"]
    deadline = time.monotonic() + 30
    while not (_logs(env, jid)[0]["complete"] and _logs(env, jid, "--err")[0]["complete"]):
        assert time.monotonic() < deadline, "the final tail was never collected"
        time.sleep(0.2)
    before = len(env["fleet"].calls())
    meta, data = _logs(env, jid)
    assert data == b"out-line\n" and meta["attempt"] == 1 and meta["source_available"] is True
    assert _logs(env, jid, "--err")[1] == b"err-line\n"
    assert _logs(env, jid, "--tail", "5")[1] == b"line\n"
    time.sleep(0.5)
    assert len(env["fleet"].calls()) == before, "reading logs never contacts the node"


def test_collected_outputs_are_fetched_intact_and_links_never_followed(env):
    script = env["project"] / "make.sh"
    script.write_text("mkdir -p out/deep runs && echo weights > out/model.pt && echo m > out/deep/m.json"
                      " && echo 1 > runs/metrics.json && ln -s /etc/passwd out/pw\n")
    rc, out, err = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "60s",
                      "--idempotency-key", "e2e-art1", "--collect", "out", "--collect", "runs/metrics.json",
                      "--", "/bin/sh", "make.sh")
    assert rc == 0, (out, err)
    job = out["job"]
    assert job["success"] is True and job["artifacts"]["state"] == "COMPLETE"
    dest = env["tmp"] / "fetched"
    rc, got, err = fq(env, "fetch", str(job["id"]), "-o", str(dest))
    assert rc == 0, (got, err)
    assert sorted(f["relpath"] for f in got["files"]) == ["out/deep/m.json", "out/model.pt", "runs/metrics.json"]
    assert (dest / "out" / "model.pt").read_text() == "weights\n"
    assert not (dest / "out" / "pw").exists(), "a symlink is never collected, let alone followed"
    (adir,) = [p for p in (env["fleet"].control_root / "attempts").iterdir()]
    assert not (adir / "outbox").exists(), "the node's staging links are cleaned after publish"


def test_a_missing_required_output_fails_finalization_not_compute(env):
    rc, out, _ = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "60s",
                    "--idempotency-key", "e2e-art2", "--collect", "never-written.txt", "--", "true")
    job = out["job"]
    assert rc == 1 and job["terminal"] and job["success"] is False
    assert job["execution"]["outcome"] == "COMPLETED" and job["execution"]["success"] is True
    assert job["artifacts"]["state"] == "FAILED"


def test_a_corrupted_transfer_is_pulled_again_without_rerunning(env):
    env["fleet"].faults(pull_corrupt_once=True)
    runs = env["tmp"] / "runs.txt"
    rc, out, err = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--wait", "--timeout", "60s",
                      "--idempotency-key", "e2e-art3", "--collect", "result.bin",
                      "--", "/bin/sh", "-c", f"echo run >> {runs}; printf payload > result.bin")
    assert rc == 0, (out, err)
    assert (env["fleet"].sandbox / "corrupted_once").exists(), "the fault fired"
    assert out["job"]["artifacts"]["state"] == "COMPLETE"
    assert runs.read_text().count("run") == 1, "outputs are re-pulled; compute is never rerun"
    dest = env["tmp"] / "f3"
    rc, _, _ = fq(env, "fetch", str(out["job"]["id"]), "-o", str(dest))
    assert rc == 0 and (dest / "result.bin").read_bytes() == b"payload"


def test_live_logs_ride_on_the_status_call(env):
    rc, out, _ = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--idempotency-key", "e2e-live-logs",
                    "--", "/bin/sh", "-c", "echo early; sleep 4; echo late")
    jid = out["jobs"][0]
    deadline = time.monotonic() + 30
    while True:
        meta, data = _logs(env, jid)
        if data.startswith(b"early"):
            break
        assert time.monotonic() < deadline, "live output never arrived"
        time.sleep(0.2)
    assert meta["attempt_state"] == "RUNNING", "the delta arrived while the job was still running"
    rc, done, _ = fq(env, "wait", str(jid), "--timeout", "60s")
    assert rc == 0
    execs = [c["argv"] for c in env["fleet"].calls() if c["argv"][0] == "exec"]
    assert not any("logs" in argv for argv in execs), "no separate per-stream log calls"
    assert any("--log" in argv for argv in execs), "deltas were requested inside status"


def test_array_members_each_see_their_own_index(env):
    out_dir = env["tmp"] / "idx"
    out_dir.mkdir()
    rc, out, err = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--array", "0-2%2", "--idempotency-key",
                      "e2e-array", "--", "/bin/sh", "-c", f'touch {out_dir}/task-"$FQ_ARRAY_TASK_ID"')
    assert rc == 0, (out, err)
    rc, _, err = fq(env, "wait", *map(str, out["jobs"]), "--timeout", "90s")
    assert rc == 0, err
    assert sorted(p.name for p in out_dir.iterdir()) == ["task-0", "task-1", "task-2"]


def test_the_queue_can_be_reshaped_from_the_cli(env):
    def sub(name, key, *extra):
        rc, out, err = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--hold", "--name", name,
                          "--idempotency-key", key, *extra, "--", "true")
        assert rc == 0, (out, err)
        return out["jobs"][0]
    sweep = [sub(f"sweep-{c}", f"q-{c}") for c in "abc"]
    keep = sub("baseline", "q-keep", "--begin", "+2h")
    rc, out, _ = fq(env, "cancel", "--name", "sweep-*")
    assert rc == 0 and sorted(r["job"] for r in out["results"]) == sweep
    rc, out, _ = fq(env, "modify", str(keep), "--mem", "2G", "--time", "30m", "--priority", "3", "--begin-now")
    assert rc == 0, out
    rc, out, _ = fq(env, "top", str(keep))
    assert rc == 0 and out["job"]["phase"] == "HELD"
    rc, shown, _ = fq(env, "queue")
    phases = {j["id"]: j["phase"] for j in shown["jobs"]}
    assert all(j not in phases or phases[j] == "TERMINAL" for j in sweep) and phases[keep] == "HELD"
    rc, out, _ = fq(env, "modify", str(keep), "--gpus", "64")
    assert rc == 2 and out["error"]["code"] == "unsatisfiable"
    rc, _, _ = fq(env, "release", str(keep))
    rc, done, _ = fq(env, "wait", str(keep), "--timeout", "60s")
    assert rc == 0 and done["job"]["success"] is True


RESUMABLE = r"""
ck="$FQ_CHECKPOINT_DIR/state"
echo "attempt=$FQ_ATTEMPT resumed=$FQ_RESUMED" >> "$LOG"
if [ "$FQ_RESUMED" = 1 ]; then echo "resumed from $(cat "$ck")" >> "$LOG"; exit 0; fi
trap 'echo "step-41" > "$ck"; echo warned >> "$LOG"' USR1
sleep 30 & wait $!; wait $!
"""


def test_a_checkpointing_job_is_warned_times_out_and_resumes_where_it_was(env):
    log = env["tmp"] / "resume.log"
    (env["project"] / "train.sh").write_text(RESUMABLE)
    rc, out, err = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--time", "4s", "--warn-before", "2s",
                      "--resume", "2", "--env", f"LOG={log}", "--idempotency-key", "e2e-resume",
                      "--wait", "--timeout", "90s", "--", "/bin/sh", "train.sh")
    assert rc == 0, (out.get("job", {}).get("execution"), out.get("job", {}).get("attempts"),
                     out.get("job", {}).get("reason"), log.read_text() if log.exists() else "")
    lines = log.read_text().splitlines()
    assert lines == ["attempt=1 resumed=0", "warned", "attempt=2 resumed=1", "resumed from step-41"], lines
    job = out["job"]
    assert job["attempts"] == 2 and job["execution"]["outcome"] == "COMPLETED"


def _fq_with_fleetctl(env, *args, timeout=120):
    """fq whose `shell` uses the fake fleetctl, as it would use yours."""
    proc = subprocess.run([sys.executable, str(FQ), "--json", *args], capture_output=True, text=True,
                          cwd=env["project"], timeout=timeout,
                          env={**os.environ, "FQ_URL": f"http://127.0.0.1:{env['server'].port}",
                               "FQ_TOKEN_FILE": str(env["token_file"]), "FQ_FLEETCTL": str(FAKES / "fleetctl"),
                               "XDG_STATE_HOME": str(env["tmp"] / "xdg-state"),
                               "XDG_CACHE_HOME": str(env["tmp"] / "xdg-cache")})
    return proc


def test_alloc_holds_a_gpu_and_shell_runs_inside_it(env):
    rc, out, err = fq(env, "alloc", "--gpus", "1", "--on", "ws1", "--time", "10m", "--timeout", "60s")
    assert rc == 0, (out, err)
    job = out["job"]
    assert job["phase"] == "RUNNING" and len(job["placement"]["gpus"]) == 1
    gpu = job["placement"]["gpus"][0]
    proc = _fq_with_fleetctl(env, "shell", str(job["id"]), "--", "/bin/sh", "-c",
                             'echo "$CUDA_VISIBLE_DEVICES|$FQ_SHELL|$(pwd)"')
    assert proc.returncode == 0, proc.stderr
    visible, marker, cwd = proc.stdout.strip().splitlines()[-1].split("|")
    assert visible == gpu and marker == "1" and cwd.endswith("/code"), "pinned to its GPU, in its snapshot"
    scopes = [json.loads(l) for l in (env["tmp"] / "sysd" / "scopes.jsonl").read_text().splitlines()]
    assert scopes[-1]["props"]["BindsTo"].startswith("fq-att_"), "the shell dies with the allocation"
    rc, _, _ = fq(env, "cancel", str(job["id"]))
    rc, done, _ = fq(env, "wait", str(job["id"]), "--timeout", "60s")
    assert done["job"]["execution"]["outcome"] == "CANCELLED"
    late = _fq_with_fleetctl(env, "shell", str(job["id"]), "--", "true")
    assert late.returncode == 2 and "not_running" in late.stdout


def test_alloc_refuses_clusters_and_groups(env):
    rc, out, _ = fq(env, "submit", "--gpus", "1", "--on", "ws1", "--allow-clusters", "--interactive",
                    "--idempotency-key", "ia", "--", "sleep", "5")
    assert rc == 2 and out["error"]["code"] in ("invalid_argument", "cluster_not_allowed")


def test_queue_and_history_tables_render(env):
    rc, out, _ = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--name", "table-job", "--wait", "--timeout",
                    "60s", "--idempotency-key", "tbl", "--", "true")
    assert rc == 0
    rc, _, _ = fq(env, "submit", "--gpus", "0", "--on", "ws1", "--name", "waiting", "--hold",
                  "--idempotency-key", "tbl2", "--", "true")

    def human(*args):
        proc = subprocess.run([sys.executable, str(FQ), *args], capture_output=True, text=True, cwd=env["project"],
                              timeout=60, env={**os.environ, "FQ_URL": f"http://127.0.0.1:{env['server'].port}",
                                               "FQ_TOKEN_FILE": str(env["token_file"])})
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.splitlines()
    q = human("q")
    assert q[0].split() == ["JOBID", "NAME", "USER", "ST", "WHERE", "GPUS", "TIME", "LIMIT", "PRIO", "POS", "REASON"]
    assert any("waiting" in line and " HD " in line and "held" in line for line in q[1:])
    assert human("q", "--noheader", "-o", "id,st") == [f"{out['job']['id'] + 1:<7} HD"]
    summary = human("history", "--summary")
    assert summary[0].split()[:2] == ["WHERE", "JOBS"] and summary[1].startswith("ws1")
