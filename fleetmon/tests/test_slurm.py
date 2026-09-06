import json
from datetime import datetime, timezone

import pytest

from fleetmon.slurm import (
    MAX_SCHEDULER_OUTPUT_BYTES,
    SACCT_FORMAT,
    fleetctl_slurm_argv,
    parse_sacct,
    parse_squeue,
    parse_squeue_text,
    sacct_argv,
    sacct_local_time,
    squeue_argv,
    squeue_text_argv,
)


def _sacct_row(job_id: str = "42") -> str:
    return "|".join(
        [
            "cluster",
            job_id,
            "",
            "",
            "user",
            "account",
            "RUNNING",
            "gpu",
            "node01",
            "",
            "",
            "",
            "",
            "gpu:1",
            "gpu:1",
        ]
    )


def test_commands_are_fixed_and_explicitly_admin_targeted():
    wrapped = fleetctl_slurm_argv("login1", squeue_argv())
    assert wrapped == [
        "exec",
        "--admin",
        "--target",
        "login1",
        "--",
        "squeue",
        "--json",
    ]
    assert fleetctl_slurm_argv("login1", squeue_text_argv())[4] == "--"
    assert SACCT_FORMAT in sacct_argv("2026-09-04T00:00:00", "2026-09-04T01:00:00")
    assert SACCT_FORMAT in sacct_argv(
        "2026-09-04T05:30:00", "2026-09-04T06:30:00", "Asia/Kolkata"
    )


def test_command_allowlist_rejects_arbitrary_scheduler_arguments():
    with pytest.raises(ValueError):
        fleetctl_slurm_argv("login1", ["squeue"])
    with pytest.raises(ValueError):
        fleetctl_slurm_argv("login1", ["sacct", "--allusers"])
    with pytest.raises(ValueError):
        fleetctl_slurm_argv("-login1", squeue_argv())
    with pytest.raises(ValueError):
        fleetctl_slurm_argv("login 1", squeue_argv())


def test_sacct_time_window_is_typed_scheduler_local_and_ordered():
    with pytest.raises(ValueError):
        sacct_argv("", "2026-09-04T01:00:00")
    with pytest.raises(ValueError):
        sacct_argv("2026-09-04T02:00:00", "2026-09-04T01:00:00")
    with pytest.raises(ValueError):
        sacct_argv("2026-09-04T00:00:00;id", "2026-09-04T01:00:00")
    with pytest.raises(ValueError, match="24 hours"):
        sacct_argv("2026-09-01T00:00:00", "2026-09-04T01:00:00")
    with pytest.raises(ValueError, match="timezone"):
        sacct_argv("2026-09-04T00:00:00", "2026-09-04T01:00:00", "Bogus/Zone")
    with pytest.raises(ValueError):
        sacct_argv("2026-09-04T00:00:00+00:00", "2026-09-04T01:00:00")


def test_sacct_local_time_converts_utc_to_scheduler_zone():
    moment = datetime(2026, 9, 4, 2, 30, tzinfo=timezone.utc)
    assert sacct_local_time(moment, "Asia/Kolkata") == "2026-09-04T08:00:00"
    assert sacct_local_time(moment, "UTC") == "2026-09-04T02:30:00"
    with pytest.raises(ValueError):
        sacct_local_time(datetime(2026, 9, 4, 2, 30), "UTC")


def test_squeue_json_requires_robust_job_identifier_and_bounds_rows():
    assert parse_squeue('{"jobs":[{"job_id":1}]}')[0]["job_id"] == 1
    assert parse_squeue({"jobs": [{"job_id": "123_4"}]})[0]["job_id"] == "123_4"
    with pytest.raises(ValueError):
        parse_squeue('{"jobs":[{"job_id":true}]}')
    with pytest.raises(ValueError):
        parse_squeue({"jobs": [{"job_id": ""}]})
    with pytest.raises(ValueError):
        parse_squeue({"jobs": [{"name": "no id"}]})
    with pytest.raises(ValueError):
        parse_squeue({"jobs": []}, max_rows=0)
    rows = parse_squeue({"jobs": [{"job_id": i} for i in range(1, 6)]}, max_rows=2)
    assert [row["job_id"] for row in rows] == [1, 2]


def test_squeue_rejects_oversized_or_non_json_rows():
    with pytest.raises(ValueError):
        parse_squeue({"jobs": [{"job_id": 1, "name": "x" * 20_000}]})
    with pytest.raises(ValueError):
        parse_squeue(b"\xff")
    with pytest.raises(ValueError):
        parse_squeue(b"\x00")
    with pytest.raises(ValueError):
        parse_squeue("x" * (MAX_SCHEDULER_OUTPUT_BYTES + 1))
    with pytest.raises(ValueError):
        parse_squeue('{"jobs":[{"job_id":1,"priority":NaN}]}')
    nested = '"x"'
    for _ in range(1100):
        nested = f"[{nested}]"
    with pytest.raises(ValueError):
        parse_squeue('{"jobs":[{"job_id":1,"nested":' + nested + "}]}")


def test_sacct_has_exact_columns_and_preserves_array_job_identity():
    row = parse_sacct(_sacct_row("123_4"))[0]
    assert row["job_id"] == "123_4"
    assert row["state"] == "RUNNING"
    assert row["alloc_tres"] == "gpu:1"
    assert parse_sacct(_sacct_row("42") + "\n")[0]["job_id"] == "42"
    with pytest.raises(ValueError):
        parse_sacct("42|too|few")
    with pytest.raises(ValueError):
        parse_sacct(_sacct_row(""))
    with pytest.raises(ValueError):
        parse_sacct(_sacct_row("\x01"))


def test_sacct_parsable_escapes_and_text_squeue_fallback():
    row = _sacct_row().replace("node01", r"node\|01")
    parsed = parse_sacct(row)[0]
    assert parsed["nodes"] == "node|01"
    assert parse_squeue_text(_sacct_row())[0]["job_id"] == "42"


def test_sacct_limits_rows_and_output():
    data = "\n".join(_sacct_row(str(i + 1)) for i in range(4))
    assert len(parse_sacct(data, max_rows=2)) == 2
    with pytest.raises(ValueError):
        parse_sacct("x" * (MAX_SCHEDULER_OUTPUT_BYTES + 1))


def test_squeue_json_serialization_accepts_nested_state_shape():
    payload = {
        "jobs": [{"job_id": 99, "state": {"name": "PENDING", "reason": "Priority"}}]
    }
    assert json.loads(json.dumps(payload)) == payload
    assert parse_squeue(payload)[0]["state"]["name"] == "PENDING"
