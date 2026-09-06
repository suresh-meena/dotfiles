from pathlib import Path

from fleetmon import cli
from fleetmon.state import MAX_STATE_BYTES


def test_installer_commands_parse_without_mutating_the_host() -> None:
    parser = cli.build_parser()
    helper = parser.parse_args(["install-helper", "--dry-run", "gpu1"])
    hub = parser.parse_args(
        ["install-hub", "--dry-run", "--python", "/usr/bin/python3"]
    )
    assert helper.target == "gpu1"
    assert helper.dry_run is True
    assert hub.dry_run is True
    assert hub.python == "/usr/bin/python3"


def test_installer_entrypoint_fails_closed() -> None:
    before = Path.cwd()
    args = cli.build_parser().parse_args(["install-hub", "--dry-run"])
    assert args.func(args) == 3
    assert Path.cwd() == before


def test_read_runtime_state_rejects_nan_infinity_and_malformed(tmp_path):
    nan = tmp_path / "nan.json"
    nan.write_text('{"next_retry":NaN}')
    assert cli._read_runtime_state(nan) == {}
    infinite = tmp_path / "inf.json"
    infinite.write_text('{"backoff":Infinity}')
    assert cli._read_runtime_state(infinite) == {}
    deep = tmp_path / "deep.json"
    depth = cli.MAX_RUNTIME_STATE_DEPTH + 2
    deep.write_text("[" * depth + "]" * depth)
    assert cli._read_runtime_state(deep) == {}
    malformed = tmp_path / "bad.json"
    malformed.write_text('{"targets":')
    assert cli._read_runtime_state(malformed) == {}
    non_dict = tmp_path / "list.json"
    non_dict.write_text("[]")
    assert cli._read_runtime_state(non_dict) == {}
    missing = tmp_path / "absent.json"
    assert cli._read_runtime_state(missing) == {}
    oversized = tmp_path / "big.json"
    oversized.write_text("{" + " " * (MAX_STATE_BYTES + 1) + "}")
    assert cli._read_runtime_state(oversized) == {}


def test_read_runtime_state_accepts_valid_state_within_bounds(tmp_path):
    valid = tmp_path / "runtime.json"
    valid.write_text('{"targets":{"gpu1":{"last_success":1.5,"failures":2}}}')
    state = cli._read_runtime_state(valid)
    assert state["targets"]["gpu1"]["failures"] == 2
