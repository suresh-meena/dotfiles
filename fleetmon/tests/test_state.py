import pytest

from fleetmon.state import MAX_STATE_BYTES, HubLock, OperationalState


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


def test_target_cap_prunes_oldest_retired_entries_and_keeps_active(tmp_path):
    path = tmp_path / "runtime.json"
    state = OperationalState(path)
    state.data["targets"] = {"active": {"last_success": 1.0, "_updated": 9e9}}
    for index in range(1100):
        state.data["targets"][f"churn{index}"] = {
            "failures": 1,
            "_updated": float(index),
        }
    state.save()
    reloaded = OperationalState(path)
    names = set(reloaded.data["targets"])
    assert len(names) == 1024
    assert "active" in names
    assert reloaded.target("active")["last_success"] == 1.0
    assert "churn0" not in names
    assert "churn1099" in names
    remaining_churn = {
        int(name[len("churn") :]) for name in names if name.startswith("churn")
    }
    assert min(remaining_churn) == 1100 - 1023


def test_byte_budget_prunes_before_oversized_write(tmp_path):
    path = tmp_path / "runtime.json"
    state = OperationalState(path)
    state.data["targets"] = {
        "active": {
            "last_success": 1.0,
            "helper_path": "/opt/helper/current/fleetmon",
            "_updated": 500.0,
        }
    }
    for index in range(40):
        state.data["targets"][f"old{index}"] = {
            "last_error": "timeout",
            "note": "x" * 40_000,
            "_updated": float(index),
        }
    state.save()
    assert path.stat().st_size <= MAX_STATE_BYTES
    reloaded = OperationalState(path)
    assert reloaded.target("active")["last_success"] == 1.0
    assert reloaded.target("active")["helper_path"]
    assert "old0" not in reloaded.data["targets"]
    assert "old39" in reloaded.data["targets"]
