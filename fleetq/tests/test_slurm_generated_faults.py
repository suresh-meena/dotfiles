"""Generated end-to-end Slurm faults around uncertain submit cancellation.

These cases exercise the real fleetq controller and the fake scheduler.  They
cover the interaction where Slurm accepted a job, the sbatch reply was lost,
and both queue and accounting observations temporarily omit that job while a
user requests cancellation.
"""

from __future__ import annotations

import random

import pytest

from test_e2e_slurm import _loopback_ok, fakeslurm, fq, job, make_env, submit, until

pytestmark = pytest.mark.skipif(not _loopback_ok(), reason="needs loopback (run via scripts/test)")


@pytest.mark.parametrize("seed", (7, 29))
def test_seeded_lost_submit_cancel_waits_through_observation_gap(make_env, seed):
    """A query gap plus cancellation cannot turn an accepted unknown into a replay."""
    rng = random.Random(seed)
    env = make_env()
    marker = env["tmp"] / f"payload-ran-{seed}"

    # Both routes mean the scheduler accepted the batch job but the submitting
    # client did not get its normal receipt.  The second route loses the reply
    # at the fleetctl boundary; the first has sbatch accept before failing.
    route = rng.choice(("transport_reply_lost", "sbatch_reply_lost"))
    if route == "transport_reply_lost":
        env["cluster"].faults(drop_once_on_script="sbatch --parsable")

    # Hold the accepted job pending. Hide its exact ID from squeue and make
    # accounting lag beyond the whole test, so empty observations stay
    # ambiguous until cancellation has durably reached the attempt directory.
    fakeslurm.configure(
        env["cluster"].slurm,
        start_delay_s=3600,
        sacct_lag_s=3600,
        faults={"accept_then_fail": int(route == "sbatch_reply_lost"),
                "squeue_hidden_ids": [1000]},
    )

    rc, out, err = submit(env, f"lost-cancel-{seed}", "touch", str(marker), wait=False)
    assert rc == 0, (out, err)
    jid = out["jobs"][0]
    unknown = until(lambda: (j := job(env, jid))["phase"] == "SUBMISSION_UNKNOWN" and j,
                    what="uncertain accepted Slurm submission")
    assert unknown["attempts"] == 1
    assert env["cluster"].jobs() and len(env["cluster"].jobs()) == 1
    dropped = (env["cluster"].sandbox / "dropped_once").exists()
    assert dropped is (route == "transport_reply_lost")

    rc, response, err = fq(env, "cancel", str(jid))
    assert rc == 0, (response, err)
    (adir,) = env["cluster"].attempt_dirs()
    until(lambda: (adir / "cancel").exists(), what="durable cancellation tombstone")

    # The tombstone was committed while the accepted job could not be found.
    # Restore the queue view so the controller can learn the exact ID and
    # confirm scheduler cancellation; accounting remains delayed.
    fakeslurm.configure(env["cluster"].slurm, start_delay_s=3600, sacct_lag_s=3600)
    done = until(lambda: (j := job(env, jid))["phase"] == "TERMINAL" and j,
                 timeout=20, what="cancellation confirmed after queue recovery")

    jobs = env["cluster"].jobs()
    assert done["execution"]["outcome"] == "CANCELLED"
    assert done["attempts"] == 1
    assert len(jobs) == 1 and jobs[0]["state"] == "CANCELLED"
    assert env["cluster"].submit_wrapper_calls() == 1
    assert len(env["cluster"].attempt_dirs()) == 1
    assert not marker.exists()
    assert not (adir / "runner_entered").exists()


def test_delayed_submit_reply_and_repeated_running_observations_survive_restart(make_env):
    """A late receipt plus repeated RUNNING evidence cannot replay the payload."""
    env = make_env()
    env["cluster"].faults(timeout_detach_on_script="sbatch --parsable")
    fakeslurm.configure(env["cluster"].slurm, faults={"reply_delay_s": 1.5})
    runs = env["tmp"] / "restart-runs.txt"
    rc, out, err = submit(env, "delayed-restart", "/bin/sh", "-c",
                          f"echo run >> {runs}; sleep 60", wait=False)
    assert rc == 0, (out, err)
    jid = out["jobs"][0]
    uncertain = until(lambda: (value := job(env, jid))["phase"] == "SUBMISSION_UNKNOWN" and value,
                      what="uncertain detached submit")
    (adir,) = env["cluster"].attempt_dirs()
    assert (env["cluster"].sandbox / "detached_once").exists()
    assert until(lambda: (adir / "receipt").exists(), timeout=10, what="delayed receipt")
    slurm_id = int((adir / "receipt").read_text())

    # Observe the same live execution repeatedly before asking the fake
    # scheduler to restart its batch script.
    for _ in range(3):
        running = until(lambda: (value := job(env, jid))["phase"] == "RUNNING" and value,
                         what="repeated running observation")
        assert running["attempts"] == 1
        assert len(env["cluster"].jobs()) == 1
        assert runs.read_text().splitlines() == ["run"]

    fakeslurm.requeue(env["cluster"].slurm, slurm_id)
    done = until(lambda: (value := job(env, jid))["phase"] == "TERMINAL" and value,
                 timeout=20, what="restart classified")
    assert uncertain["phase"] == "SUBMISSION_UNKNOWN"
    assert done["attempts"] == 1
    assert done["execution"]["outcome"] == "UNKNOWN_EXIT"
    assert runs.read_text().splitlines() == ["run"]
    assert len(env["cluster"].jobs()) == 1
    assert env["cluster"].submit_wrapper_calls() == 1
    assert len(env["cluster"].attempt_dirs()) == 1
    assert list(adir.glob("replay.*")), "the restarted batch script must honor runner_entered"
