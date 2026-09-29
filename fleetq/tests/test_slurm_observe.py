"""parse_observation: one observation session's text → per-attempt evidence (§3.6)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from fleetq.executors.slurm import SlurmExecutor, parse_observation
from fleetq.slurm.render import OBSERVE_SCRIPT, render_batch
from fleetq.transport.fleetctl import FleetctlResult


def session(attempts: dict[str, dict], *, squeue: list[str] = (), squeue_rc: int = 0,
            squeue_error: str | None = None, names: list[str] = (),
            sacct: list[str] | None = None) -> str:
    out = []
    for aid, f in attempts.items():
        out += [f"__ATT {aid}", f"__CLAIM {f.get('claim', 1)}", f"__STAGED {f.get('staged', 1)}",
                f"__RECEIPT {f.get('receipt', '')}", f"__STARTED {f.get('started', '')}",
                f"__ENTERED {f.get('entered', 0)}", f"__CANCEL {f.get('cancel', 0)}",
                f"__RESULT {json.dumps(f['result']) if 'result' in f else ''}"]
    out += ["__SQUEUE_BEGIN", *squeue]
    if squeue_error:
        out.append(squeue_error)
    out += [f"__SQUEUE_RC {squeue_rc}", "__NAMES_BEGIN", *names, "__SACCT_BEGIN"]
    if sacct is not None:
        out += [*sacct, "__SACCT_RC 0"]
    return "\n".join(out + ["__END"]) + "\n"


def one(text: str, *, accounting: bool = True, aid: str = "att_1"):
    return parse_observation(text, [aid], accounting=accounting)[aid]


def test_unclaimed_attempts_are_staged_or_absent():
    assert one(session({"att_1": {"claim": 0}})).state == "staged"
    assert one(session({"att_1": {"claim": 0, "staged": 0}})).state == "absent"


def test_pending_carries_the_reason_and_whether_it_can_ever_clear():
    grp = one(session({"att_1": {"receipt": "1001"}}, squeue=["1001|PENDING|AssocGrpGRES||fq-att_1"]))
    assert grp.state == "pending" and grp.evidence["blocking"] is False
    never = one(session({"att_1": {"receipt": "1001"}}, squeue=["1001|PENDING|AccountNotAllowed||fq-att_1"]))
    assert never.state == "pending" and never.evidence["blocking"] is True
    nodes = one(session({"att_1": {"receipt": "1001"}},
                        squeue=["1001|PENDING|ReqNodeNotAvail, UnavailableNodes:node03||fq-att_1"]))
    assert nodes.state == "pending" and nodes.evidence["blocking"] is True


def test_running_and_a_receipt_with_a_cluster_suffix():
    obs = one(session({"att_1": {"receipt": "1001;kiac", "entered": 1}}, squeue=["1001|RUNNING|None|gpu7|fq-att_1"]))
    assert obs.state == "running" and obs.remote_id == "1001" and obs.payload_entered


def test_the_payload_rc_decides_success_after_a_normal_scheduler_end():
    ok = one(session({"att_1": {"receipt": "7", "entered": 1, "result": {"exit_code": 0}}},
                     squeue=["7|COMPLETED|None|n|fq-att_1"]))
    assert (ok.state, ok.outcome, ok.exit_code) == ("stopped", "COMPLETED", 0)
    bad = one(session({"att_1": {"receipt": "7", "entered": 1, "result": {"exit_code": 3}}},
                      squeue=["7|COMPLETED|None|n|fq-att_1"]))
    assert (bad.outcome, bad.exit_code) == ("FAILED", 3)


def test_scheduler_verdicts_outrank_the_payload_rc():
    obs = one(session({"att_1": {"receipt": "7", "entered": 1, "result": {"exit_code": 0}}},
                      squeue=["7|TIMEOUT|None|n|fq-att_1"]))
    assert obs.outcome == "TIMEOUT"


def test_a_clean_scheduler_exit_without_a_result_is_never_success():
    """The batch script exits 0 when a requeued replay skips the payload; that is not the payload's success."""
    obs = one(session({"att_1": {"receipt": "7", "entered": 1}}, squeue=["7|COMPLETED|None|n|fq-att_1"]))
    assert obs.state == "stopped" and obs.outcome == "UNKNOWN_EXIT"


def test_result_published_during_scheduler_query_is_seen(tmp_path):
    root = tmp_path / "control"
    attempt_id = "att_0123456789abcdef01234567"
    attempt = root / "attempts" / attempt_id
    attempt.mkdir(parents=True)
    (root / "cache").mkdir()
    # Batch jobs persist resumable checkpoints outside attempts/cache. This
    # private root entry must not make later observer calls reject the root.
    (root / "jobs").mkdir(mode=0o700)
    root.chmod(0o700)
    (root / "attempts").chmod(0o700)
    (root / "cache").chmod(0o700)
    attempt.chmod(0o700)
    (attempt / "receipt").write_text("7")
    (attempt / "submit.claim").mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    squeue = fake_bin / "squeue"
    squeue.write_text("#!/bin/sh\nprintf '{\"exit_code\":7}\\n' > \"$FAKE_ATTEMPT_DIR/result.json\"\n"
                      f"printf '7|COMPLETED|None|n|fq-{attempt_id}\\n'\n")
    squeue.chmod(0o700)
    result = subprocess.run(
        ["sh", "-c", OBSERVE_SCRIPT, "fleetq", str(root), "0", attempt_id],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "FAKE_ATTEMPT_DIR": str(attempt)},
        capture_output=True, text=True, check=True,
    )
    obs = one(result.stdout, accounting=False, aid=attempt_id)
    assert (obs.state, obs.outcome, obs.exit_code) == ("stopped", "FAILED", 7)


def test_batch_checkpoint_root_is_created_private():
    batch = render_batch(attempt_id="att_0123456789abcdef01234567", adir="/shared/attempts/att_0123456789abcdef01234567",
                         root="/shared", request={"partition": "gpu", "time_s": 60, "cpus": 1,
                                                   "mem_mb": 100, "gpus": 0},
                         spec={"command": {"argv": ["true"]}, "env": {}, "setup": "",
                               "control": {}, "workdir": {"bundle": "sha256:" + "a" * 64}},
                         site={}, bundle_digest="sha256:" + "a" * 64, bundle_sha256="b" * 64,
                         job_id=9)
    assert "umask 077" in batch
    assert 'mkdir -p "$FQ_CHECKPOINT_DIR"' in batch


def test_a_tombstone_that_beat_the_start_is_cancelled_not_completed():
    obs = one(session({"att_1": {"receipt": "7", "entered": 0, "cancel": 1}}, squeue=["7|COMPLETED|None|n|fq-att_1"]))
    assert obs.outcome == "CANCELLED"


def test_aged_out_of_squeue_resolves_from_sacct():
    obs = one(session({"att_1": {"receipt": "7", "entered": 1, "result": {"exit_code": 0}}},
                      sacct=["7|fq-att_1|COMPLETED|0:0"]))
    assert obs.outcome == "COMPLETED" and obs.evidence["sacct"]


def test_aged_out_with_lagging_accounting_waits():
    obs = one(session({"att_1": {"receipt": "7", "entered": 1}}, sacct=[]))
    assert obs.state == "unknown"


def test_aged_out_with_no_accounting_trusts_the_result_file():
    """wellsfargo has no sacct: after MinJobAge, the result file is the only evidence left."""
    obs = one(session({"att_1": {"receipt": "7", "entered": 1, "result": {"exit_code": 2}}},
                      squeue_rc=1, squeue_error="slurm_load_jobs error: Invalid job id specified"),
              accounting=False)
    assert (obs.state, obs.outcome, obs.exit_code) == ("stopped", "FAILED", 2)
    assert obs.evidence["evidence"] == "result_file"


def test_a_failed_squeue_is_never_read_as_absence():
    obs = one(session({"att_1": {"receipt": "7", "entered": 1}}, squeue_rc=1), accounting=False)
    assert obs.state == "unknown"


def test_a_failed_squeue_does_not_promote_a_stale_result_file():
    obs = one(session({"att_1": {"receipt": "7", "entered": 1, "result": {"exit_code": 0}}},
                      squeue_rc=1, squeue_error="slurm_load_jobs error: Unable to contact slurm controller"),
              accounting=False)
    assert obs.state == "unknown"


@pytest.mark.parametrize("state", ["CONFIGURING", "SUSPENDED", "REQUEUED", "REQUEUE_HOLD", "SIGNALING"])
def test_transitional_states_keep_allocation_live(state):
    obs = one(session({"att_1": {"receipt": "7", "entered": 1, "result": {"exit_code": 0}}},
                      squeue=[f"7|{state}|None|n|fq-att_1"]))
    assert obs.state == "pending" and obs.outcome is None


def test_unknown_scheduler_states_and_accounting_never_free_reservation():
    fields = {"receipt": "7", "entered": 1, "result": {"exit_code": 0}}
    current = one(session({"att_1": fields}, squeue=["7|FUTURE_STATE|None|n|fq-att_1"]))
    historical = one(session({"att_1": fields}, sacct=["7|fq-att_1|FUTURE_STATE|0:0"]))
    assert current.state == historical.state == "unknown"


def test_truncated_observation_envelope_cannot_confirm_completion():
    class Transport:
        async def exec(self, *_args, **_kwargs):
            return FleetctlResult("ok", False, stdout="__END\n", envelope={"stdout_truncated": True})

    async def permit(*_args):
        return True, "permit", None

    node = SimpleNamespace(id="site", backend="slurm", control_root="/control", fleetctl_target="site",
                           site={"accounting": False})
    executor = SlurmExecutor(Transport(), SimpleNamespace(nodes=[node], controller={}), permit=permit)
    result = asyncio.run(executor.observe("site", ["att_1"]))
    assert result.reachable is False and result.reason == "output_truncated"


def test_claim_without_receipt_resolves_by_exact_name_only():
    found = one(session({"att_1": {"entered": 1}}, names=["1001|RUNNING|None|n|fq-att_1"]))
    # No receipt file means `squeue -j` could not list it; the name row is the evidence.
    assert (found.state, found.remote_id) == ("running", "1001")
    ended = one(session({"att_1": {"entered": 1, "result": {"exit_code": 0}}}, names=["1005|fq-att_1|COMPLETED|0:0"]))
    assert (ended.state, ended.outcome, ended.remote_id) == ("stopped", "COMPLETED", "1005")
    none = one(session({"att_1": {}}))
    assert none.state == "unknown" and "no job found" in none.evidence["reason"]
    dup = one(session({"att_1": {}}, names=["1001|PENDING|Priority||fq-att_1", "1002|PENDING|Priority||fq-att_1"]))
    assert dup.state == "unknown" and dup.evidence["ids"] == ["1001", "1002"]
    other = one(session({"att_1": {}}, names=["1003|PENDING|Priority||fq-att_10"]))
    assert other.state == "unknown", "a prefix match is not the attempt"
    via_sacct = one(session({"att_1": {}}, names=["1004|fq-att_1|COMPLETED|0:0"]))
    assert via_sacct.remote_id == "1004"
