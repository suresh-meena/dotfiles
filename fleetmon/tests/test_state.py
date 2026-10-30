import pytest

from fleetmon.state import HubLock, OperationalState


def test_operational_state_is_atomic_and_round_trips(tmp_path):
    path = tmp_path / "runtime.json"
    state = OperationalState(path)
    state.update_target("gpu1", failures=2, last_error="timeout")
    loaded = OperationalState(path)
    entry = loaded.target("gpu1")
    assert entry.pop("_updated") > 0
    assert entry == {"failures": 2, "last_error": "timeout"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_operational_state_discards_nonstandard_numbers(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text('{"next_retry":NaN}')
    assert OperationalState(path).data == {}


def test_hub_lock_refuses_a_second_writer_and_recovers(tmp_path):
    path = tmp_path / "hub.lock"
    first = HubLock(path)
    second = HubLock(path)
    first.acquire()
    with pytest.raises(RuntimeError, match="already running"):
        second.acquire()
    first.release()
    assert path.exists()
    second.acquire()
    second.release()


def test_hub_lock_refuses_a_symlink_without_touching_its_target(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    lock_path = tmp_path / "hub.lock"
    lock_path.symlink_to(victim)
    with pytest.raises(PermissionError):
        HubLock(lock_path).acquire()
    assert victim.read_text() == "keep"
