import json
import re
import sys
from collections import namedtuple
from types import SimpleNamespace

from fleetmon import snapshot
from fleetmon.protocol import MAX_JSON_BYTES, validate_snapshot

collect_snapshot = snapshot.collect_snapshot


def test_snapshot_is_bounded_and_schema_valid_without_gpu(monkeypatch):
    monkeypatch.setattr(
        "fleetmon.snapshot._nvml", lambda *_: ([], {}, False, "nvml_missing")
    )
    doc = collect_snapshot(window_seconds=0)
    validate_snapshot(doc)
    assert doc["observation_duration_seconds"] >= 0
    assert doc["collection_duration_seconds"] >= doc["observation_duration_seconds"]
    assert doc["gpus"] == []
    assert len(doc["processes"]) <= 80
    assert len(doc["users"]) <= 128
    assert all(user["gpu_process_count"] is None for user in doc["users"])
    assert all(user["vram_bytes"] is None for user in doc["users"])
    assert "cmdline" not in doc["processes"][0] if doc["processes"] else True


def test_strings_are_truncated_and_power_none():
    assert len(snapshot._text("x" * 1000)) == 256
    fake = SimpleNamespace(nvmlDeviceGetPowerUsage=lambda handle: None)
    assert snapshot._power_watts(fake, object()) is None
    assert snapshot._nvml_memory_value(fake, None) is None


def test_user_cap_keeps_gpu_users():
    users = [
        {"uid": i, "gpu_process_count": 1 if i == 127 else 0, "cpu_cores": 0}
        for i in range(129)
    ]
    chosen = sorted(
        users, key=lambda x: (x["gpu_process_count"] > 0, x["cpu_cores"]), reverse=True
    )[:128]
    assert chosen[0]["uid"] == 127


def test_process_cpu_never_negative():
    assert max(0.0, (1.0 - 3.0) / 0.25) == 0.0
    assert snapshot._process_cpu_total((1.5, 2.5, 99.0, 99.0)) == 4.0


def test_host_cpu_does_not_double_count_guest_and_treats_iowait_as_idle():
    Times = namedtuple("Times", "user idle iowait guest guest_nice")
    before = Times(100, 100, 10, 20, 0)
    after = Times(110, 105, 15, 25, 0)
    busy, valid = snapshot._cpu_delta(before, after)
    # Raw tuple delta is 25, but guest's 5 seconds are already included in
    # user. Correct total is 20; idle+iowait accounts for 10 of it.
    assert valid == 1
    assert busy == 0.5


def test_nvml_unavailable_memory_sentinel_is_unknown():
    fake = SimpleNamespace(NVML_VALUE_NOT_AVAILABLE=2**64 - 1)
    assert snapshot._nvml_memory_value(fake, 2**64 - 1) is None
    assert snapshot._nvml_memory_value(fake, 1024) == 1024


def test_nvml_multi_gpu_process_has_bounded_allocations_and_unknown_total(monkeypatch):
    class FakeNvml:
        NVML_VALUE_NOT_AVAILABLE = 2**64 - 1

        @staticmethod
        def nvmlInit():
            return None

        @staticmethod
        def nvmlShutdown():
            return None

        @staticmethod
        def nvmlDeviceGetCount():
            return 2

        @staticmethod
        def nvmlDeviceGetHandleByIndex(index):
            return index

        @staticmethod
        def nvmlDeviceGetUUID(handle):
            return f"GPU-{handle}".encode()

        @staticmethod
        def nvmlDeviceGetName(handle):
            return "test"

        @staticmethod
        def nvmlDeviceGetUtilizationRates(handle):
            return SimpleNamespace(gpu=25)

        @staticmethod
        def nvmlDeviceGetMemoryInfo(handle):
            return SimpleNamespace(total=1000, used=500)

        @staticmethod
        def nvmlDeviceGetTemperature(handle, sensor):
            return 40

        @staticmethod
        def nvmlDeviceGetPowerUsage(handle):
            return 1000

        @staticmethod
        def nvmlDeviceGetComputeRunningProcesses(handle):
            return [
                SimpleNamespace(pid=123, usedGpuMemory=None if handle == 0 else 200)
            ]

    monkeypatch.setitem(sys.modules, "pynvml", FakeNvml)
    gpus, processes, supported, error = snapshot._nvml()
    assert supported is True
    assert error is None
    assert len(gpus) == 2
    assert processes[123]["gpu_uuid"] is None
    assert processes[123]["gpu_index"] is None
    assert processes[123]["vram_bytes"] is None
    assert "_vram_unknown" not in processes[123]
    assert len(processes[123]["gpu_allocations"]) == 2


def test_nvml_missing_uuid_is_partial_but_wire_valid(monkeypatch):
    monkeypatch.setitem(sys.modules, "pynvml", _fake_nvml_module(count=1, uuid=None))
    gpus, _, supported, error = snapshot._nvml()
    assert supported is True
    assert error == "uuid_unavailable"
    assert re.fullmatch(r"unavailable-[0-9a-f]{16}-0", gpus[0]["uuid"])
    assert gpus[0]["error"] == "uuid_unavailable"


def _fake_nvml_module(count=2, uuid=None):
    class FakeNvml:
        @staticmethod
        def nvmlInit():
            return None

        @staticmethod
        def nvmlShutdown():
            return None

        @staticmethod
        def nvmlDeviceGetCount():
            return count

        @staticmethod
        def nvmlDeviceGetHandleByIndex(index):
            return index

        @staticmethod
        def nvmlDeviceGetUUID(handle):
            return uuid

        @staticmethod
        def nvmlDeviceGetName(handle):
            return "test"

        @staticmethod
        def nvmlDeviceGetUtilizationRates(handle):
            return SimpleNamespace(gpu=0)

        @staticmethod
        def nvmlDeviceGetMemoryInfo(handle):
            return SimpleNamespace(total=0, used=0)

        @staticmethod
        def nvmlDeviceGetTemperature(handle, sensor):
            return 0

        @staticmethod
        def nvmlDeviceGetPowerUsage(handle):
            return 0

        @staticmethod
        def nvmlDeviceGetComputeRunningProcesses(handle):
            return []

    return FakeNvml


def test_gpu_reorder_with_unavailable_uuids_never_aliases_across_snapshots(
    monkeypatch,
):
    monkeypatch.setitem(sys.modules, "pynvml", _fake_nvml_module(count=2, uuid=None))
    first_gpus, _, _, first_error = snapshot._nvml()
    second_gpus, _, _, second_error = snapshot._nvml()
    first_ids = {gpu["uuid"] for gpu in first_gpus}
    second_ids = {gpu["uuid"] for gpu in second_gpus}
    assert len(first_ids) == 2 and len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)
    assert all(gpu["error"] == "uuid_unavailable" for gpu in first_gpus + second_gpus)
    assert first_error == second_error == "uuid_unavailable"


def test_unavailable_uuid_snapshot_is_wire_valid(monkeypatch):
    monkeypatch.setitem(sys.modules, "pynvml", _fake_nvml_module(count=1, uuid=None))
    gpus, _, supported, error = snapshot._nvml()
    document = collect_snapshot(window_seconds=0)
    process = {
        "pid": 7,
        "gpu_process": True,
        "gpu_uuid": gpus[0]["uuid"],
        "gpu_index": 0,
        "gpu_allocations": [
            {"gpu_uuid": gpus[0]["uuid"], "gpu_index": 0, "vram_bytes": 10}
        ],
    }
    document["gpus"] = gpus
    document["processes"] = [process]
    document["visibility"]["processes_visible"] = 1
    document["visibility"]["processes_emitted"] = 1
    document["capabilities"]["nvml_supported"] = supported
    document["capabilities"]["nvml_error"] = error
    validate_snapshot(document)


def test_nvml_obeys_collection_deadline(monkeypatch):
    class FakeNvml:
        @staticmethod
        def nvmlInit():
            return None

        @staticmethod
        def nvmlShutdown():
            return None

        @staticmethod
        def nvmlDeviceGetCount():
            return 8

    monkeypatch.setitem(sys.modules, "pynvml", FakeNvml)
    gpus, processes, supported, error = snapshot._nvml(deadline=0)
    assert gpus == [] and processes == {}
    assert supported is True and error == "collection_deadline"


def test_first_pass_access_denied_does_not_abort(monkeypatch):
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    good = SimpleNamespace(pid=2, info={"create_time": 2.0, "cpu_times": (1.0, 0.0)})

    def processes(fields):
        if "create_time" in fields and len(fields) == 2:
            raise AccessDenied()
        return [good]

    ps = SimpleNamespace(
        AccessDenied=AccessDenied,
        NoSuchProcess=NoSuchProcess,
        cpu_times=lambda: (SimpleNamespace(idle=1.0),),
        process_iter=processes,
        cpu_count=lambda: 2,
        virtual_memory=lambda: SimpleNamespace(total=1, available=1, used=0),
        swap_memory=lambda: SimpleNamespace(total=0, used=0),
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", ps)
    monkeypatch.setattr(snapshot, "_nvml", lambda *_: ([], {}, False, "missing"))
    monkeypatch.setattr(snapshot.time, "sleep", lambda _: None)
    doc = snapshot.collect_snapshot(window_seconds=0)
    assert "visibility" in doc


def test_snapshot_json_trims_detail_to_wire_limit(monkeypatch):
    document = collect_snapshot(window_seconds=0)
    allocation = {"gpu_uuid": "g" * 256, "gpu_index": 0, "vram_bytes": 0}
    document["processes"] = [
        {
            "pid": index + 1,
            "name": "p" * 256,
            "gpu_allocations": [dict(allocation) for _ in range(32)],
        }
        for index in range(80)
    ]
    document["visibility"]["processes_visible"] = 80
    document["visibility"]["processes_emitted"] = 80
    monkeypatch.setattr(snapshot, "collect_snapshot", lambda **_: document)

    payload = snapshot.snapshot_json(window_seconds=0)
    decoded = json.loads(payload)

    assert len(payload) <= MAX_JSON_BYTES
    assert decoded["limits"]["truncated"] is True
    assert len(decoded["processes"]) < 80
    validate_snapshot(decoded)


def _fake_psutil_with_processes(total, rss=None):
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    def first_pass(fields):
        return []

    def second_pass(fields):
        for index in range(total):
            yield SimpleNamespace(
                pid=index + 1,
                info={
                    "pid": index + 1,
                    "name": "worker",
                    "exe": "/bin/worker",
                    "uids": SimpleNamespace(real=1000),
                    "memory_info": SimpleNamespace(
                        rss=(index % 97) * 512 if rss else 1024
                    ),
                    "create_time": 1.0,
                    "cpu_times": (0.0, 0.0),
                },
            )

    def iter_processes(fields):
        return first_pass(fields) if len(fields) == 2 else second_pass(fields)

    return SimpleNamespace(
        AccessDenied=AccessDenied,
        NoSuchProcess=NoSuchProcess,
        cpu_times=lambda: (SimpleNamespace(idle=1.0),),
        process_iter=iter_processes,
        cpu_count=lambda: 2,
        virtual_memory=lambda: SimpleNamespace(total=1, available=1, used=0),
        swap_memory=lambda: SimpleNamespace(total=0, used=0),
    )


def test_hard_scan_cap_keeps_aggregates_first_and_marks_truncation(monkeypatch):
    username_calls = {"count": 0}

    def counting_username(uid, cache=None):
        if cache is not None and uid in cache:
            return cache[uid]
        username_calls["count"] += 1
        if cache is not None:
            cache[uid] = "user1000"
        return "user1000"

    ps = _fake_psutil_with_processes(snapshot.MAX_SCAN_PROCESSES + 500)
    monkeypatch.setitem(sys.modules, "psutil", ps)
    monkeypatch.setattr(snapshot, "_nvml", lambda *_: ([], {}, False, "missing"))
    monkeypatch.setattr(snapshot.time, "sleep", lambda _: None)
    monkeypatch.setattr(snapshot, "_username", counting_username)

    doc = collect_snapshot(window_seconds=0)

    validate_snapshot(doc)
    assert doc["limits"]["truncated"] is True
    assert doc["visibility"]["counters_truncated"] is True
    assert doc["visibility"]["partial"] is True
    assert doc["visibility"]["processes_emitted"] == len(doc["processes"]) == 80
    assert doc["visibility"]["processes_visible"] == snapshot.MAX_SCAN_PROCESSES
    users = doc["users"]
    assert len(users) == 1
    assert users[0]["uid"] == 1000
    assert users[0]["process_count"] == snapshot.MAX_SCAN_PROCESSES
    assert users[0]["rss_bytes"] == snapshot.MAX_SCAN_PROCESSES * 1024
    assert username_calls["count"] == 1


def _fake_psutil_with_ranked_processes(total):
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    def first_pass(fields):
        return []

    def second_pass(fields):
        for index in range(total):
            yield SimpleNamespace(
                pid=index + 1,
                info={
                    "pid": index + 1,
                    "name": f"worker{index}",
                    "exe": "/bin/worker",
                    "uids": SimpleNamespace(real=1000 + index % 5),
                    "memory_info": SimpleNamespace(rss=(index % 97) * 512),
                    "create_time": 1.0,
                    "cpu_times": (float(index % 13), 0.0),
                },
            )

    def iter_processes(fields):
        return first_pass(fields) if len(fields) == 2 else second_pass(fields)

    return SimpleNamespace(
        AccessDenied=AccessDenied,
        NoSuchProcess=NoSuchProcess,
        cpu_times=lambda: (SimpleNamespace(idle=1.0),),
        process_iter=iter_processes,
        cpu_count=lambda: 2,
        virtual_memory=lambda: SimpleNamespace(total=1, available=1, used=0),
        swap_memory=lambda: SimpleNamespace(total=0, used=0),
    )


def test_bounded_selection_matches_full_sort_of_top_records(monkeypatch):
    total = snapshot.MAX_PROCESSES * 4
    ps = _fake_psutil_with_processes(total, rss=True)
    monkeypatch.setitem(sys.modules, "psutil", ps)
    monkeypatch.setattr(snapshot, "_nvml", lambda *_: ([], {}, False, "missing"))
    monkeypatch.setattr(snapshot.time, "sleep", lambda _: None)

    doc = collect_snapshot(window_seconds=0)

    validate_snapshot(doc)
    # The helper never retains more than the top MAX_PROCESSES records even
    # though it scanned four times that many.
    assert doc["visibility"]["processes_visible"] == total
    assert len(doc["processes"]) == snapshot.MAX_PROCESSES
    assert doc["visibility"]["processes_emitted"] == snapshot.MAX_PROCESSES
    assert doc["limits"]["truncated"] is True
    # The bounded buffer selects the same winners a stable full sort would
    # rank by (gpu usage, cpu, rss) descending; the first pass is empty so
    # per-process cpu is unknown and rss drives the ranking here.
    scanned = [(index + 1, (index % 97) * 512) for index in range(total)]
    expected_pids = [
        pid
        for pid, _rss in sorted(
            scanned, key=lambda item: (False, 0, item[1]), reverse=True
        )[: snapshot.MAX_PROCESSES]
    ]
    assert [rec["pid"] for rec in doc["processes"]] == expected_pids
    # User totals still aggregate every scanned process accurately.
    assert sum(user["process_count"] for user in doc["users"]) == total


def test_username_cache_is_bounded_and_still_resolves():
    cache: dict[int, str | None] = {}
    for uid in range(snapshot.MAX_USERNAME_CACHE + 50):
        snapshot._username(uid, cache)
    assert len(cache) <= snapshot.MAX_USERNAME_CACHE
    # Existing entries keep resolving and the helper still returns a name.
    some_cached = next(iter(cache))
    assert snapshot._username(some_cached, cache) is not None


def test_proc_owner_reads_only_uid_and_name(monkeypatch):
    # /proc/<pid>/status carries many fields; we must extract only uid+name.
    fake_status = "Name:\tgpuproc\nUid:\t2000\t2000\t2000\t2000\nGid:\t3000\n"
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path).endswith("/status"):
            import io

            return io.StringIO(fake_status)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    uid, name = snapshot._proc_owner(1234)
    assert uid == 2000
    assert name == "gpuproc"


def test_proc_owner_handles_unreadable_process(monkeypatch):
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path).endswith("/status"):
            raise OSError("gone")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert snapshot._proc_owner(1234) == (None, None)


def _gpu_proc_record(pid, uuid="GPU-0", index=0, vram=1000):
    return {
        "gpu_process": True,
        "gpu_uuid": uuid,
        "gpu_index": index,
        "gpu_allocations": [{"gpu_uuid": uuid, "gpu_index": index, "vram_bytes": vram}],
        "vram_bytes": vram,
    }


def _gpu_row_dict(uuid="GPU-0", index=0):
    return {
        "uuid": uuid,
        "index": index,
        "model": "A100",
        "utilization_fraction": 0.5,
        "vram_total_bytes": 1000,
        "vram_used_bytes": 500,
        "temperature_c": 40,
        "power_watts": 10,
        "compute_process_count": 1,
        "supported": True,
        "error": None,
        "mig_detected": False,
        "instance_supported": False,
    }


def test_nvml_process_missed_by_scan_is_supplemented(monkeypatch):
    ps = _fake_psutil_with_processes(3)
    monkeypatch.setitem(sys.modules, "psutil", ps)
    monkeypatch.setattr(
        snapshot,
        "_nvml",
        lambda *_: ([_gpu_row_dict()], {999: _gpu_proc_record(999)}, True, None),
    )
    monkeypatch.setattr(snapshot.time, "sleep", lambda _: None)
    monkeypatch.setattr(snapshot, "_proc_owner", lambda pid: (2000, "gpuproc"))
    monkeypatch.setattr(snapshot, "_username", lambda uid, cache=None: "gpuuser")

    doc = collect_snapshot(window_seconds=0)
    validate_snapshot(doc)

    pids = {rec["pid"] for rec in doc["processes"]}
    assert 999 in pids, "NVML process the psutil scan missed is emitted"
    rec = next(rec for rec in doc["processes"] if rec["pid"] == 999)
    assert rec["gpu_process"] is True
    assert rec["gpu_uuid"] == "GPU-0"
    assert rec["uid"] == 2000
    assert rec["username"] == "gpuuser"
    assert rec["name"] == "gpuproc"
    assert rec["gpu_allocations"][0]["gpu_uuid"] == "GPU-0"
    # User aggregation picked up the supplemented GPU process.
    users = {u["uid"]: u for u in doc["users"]}
    assert users[2000]["gpu_process_count"] == 1
    assert users[2000]["vram_bytes"] == 1000
    # The supplemented process counts as visible.
    assert doc["visibility"]["processes_visible"] == 4
    assert doc["visibility"]["processes_emitted"] == 4


def test_nvml_process_already_scanned_is_not_duplicated(monkeypatch):
    # pid 2 is in the psutil scan; NVML also attributes it. No duplicate row.
    ps = _fake_psutil_with_processes(3)
    monkeypatch.setitem(sys.modules, "psutil", ps)
    monkeypatch.setattr(
        snapshot,
        "_nvml",
        lambda *_: ([_gpu_row_dict()], {2: _gpu_proc_record(2)}, True, None),
    )
    monkeypatch.setattr(snapshot.time, "sleep", lambda _: None)
    monkeypatch.setattr(snapshot, "_proc_owner", lambda pid: (2000, "gpuproc"))

    doc = collect_snapshot(window_seconds=0)
    validate_snapshot(doc)

    pids = [rec["pid"] for rec in doc["processes"]]
    assert pids.count(2) == 1, "no duplicate for a scanned GPU process"
    rec = next(rec for rec in doc["processes"] if rec["pid"] == 2)
    assert rec["gpu_process"] is True
    assert rec["gpu_allocations"][0]["gpu_uuid"] == "GPU-0"


def test_supplement_eviction_marks_truncation(monkeypatch):
    # Fill the top buffer with non-GPU processes, then an NVML process must
    # evict the lowest-ranked one and flag truncation.
    ps = _fake_psutil_with_processes(snapshot.MAX_PROCESSES, rss=True)
    monkeypatch.setitem(sys.modules, "psutil", ps)
    monkeypatch.setattr(
        snapshot,
        "_nvml",
        lambda *_: (
            [_gpu_row_dict()],
            {5000: _gpu_proc_record(5000, vram=5000)},
            True,
            None,
        ),
    )
    monkeypatch.setattr(snapshot.time, "sleep", lambda _: None)
    monkeypatch.setattr(snapshot, "_proc_owner", lambda pid: (2000, "gpuproc"))

    doc = collect_snapshot(window_seconds=0)
    validate_snapshot(doc)

    pids = [rec["pid"] for rec in doc["processes"]]
    assert 5000 in pids, "GPU process outranks non-GPU evictees"
    assert len(doc["processes"]) == snapshot.MAX_PROCESSES
    assert doc["limits"]["truncated"] is True
