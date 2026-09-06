"""One-shot, bounded host observation (no daemon state or child commands)."""

from __future__ import annotations

import datetime as _dt
import heapq
import math
import os
import time
from contextlib import suppress
from typing import Any

from .protocol import (
    MAX_GPU_ALLOCATIONS,
    MAX_GPUS,
    MAX_PROCESSES,
    MAX_USERS,
    SCHEMA_VERSION,
    ProtocolError,
    encode_snapshot,
    validate_snapshot,
)

HELPER_VERSION = str(SCHEMA_VERSION)
DEFAULT_WINDOW_SECONDS = 0.25
MAX_WINDOW_SECONDS = 1.0
MAX_COLLECTION_SECONDS = 5.0
MAX_STRING_LENGTH = 256
MAX_COUNTER_PROCESSES = 2048
MAX_SCAN_PROCESSES = 2048
MAX_USERNAME_CACHE = 512


def _reason(exc: BaseException) -> str:
    return type(exc).__name__.lower()


def _username(
    uid: int | None, cache: dict[int, str | None] | None = None
) -> str | None:
    if uid is None:
        return None
    if cache is not None and uid in cache:
        return cache[uid]
    try:
        import pwd

        name: str | None = pwd.getpwuid(uid).pw_name[:MAX_STRING_LENGTH]
    except (KeyError, OSError):
        name = None
    # Bound the cache so hosts with many distinct uids cannot grow it
    # without limit; a miss beyond the cap simply looks the name up again.
    if cache is not None and len(cache) < MAX_USERNAME_CACHE:
        cache[uid] = name
    return name


def _ephemeral_gpu_identity(index: int) -> str:
    """Derive a per-snapshot identity string that cannot alias across snapshots.

    A GPU whose UUID is unavailable has no stable identity. The returned
    string is unique to this snapshot, so a GPU reorder can never make two
    snapshots agree on a fabricated identity; the hub must treat it as
    unknown for grouping.
    """

    import secrets

    return f"unavailable-{secrets.token_hex(8)}-{index}"


def _proc_owner(pid: int) -> tuple[int | None, str | None]:
    """Read the minimal /proc identity for one pid: (uid, name).

    Used to attribute GPU compute processes the psutil scan missed (the scan
    is bounded and the process may be short-lived or owned by another user).
    Only the owner and the command name are read; no environment, argv, or
    paths. A vanished or unreadable process yields (None, None).
    """

    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as handle:
            text = handle.read(4096)
    except OSError:
        return None, None
    uid: int | None = None
    name: str | None = None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            fields = line.split()
            if len(fields) > 1 and fields[1].isdigit():
                uid = int(fields[1])
        elif line.startswith("Name:"):
            value = line[5:].strip()
            name = value[:MAX_STRING_LENGTH] if value else None
    return uid, name


def _cpu_delta(before: Any, after: Any) -> tuple[float | None, int]:
    try:
        b = sum(float(x) for x in before)
        a = sum(float(x) for x in after)
        # Linux reports guest time both in user/nice and in separate guest
        # fields. Remove the duplicate before deriving a fraction. Treat
        # iowait as idle, matching psutil's CPU-percent semantics.
        guest_delta = (
            float(getattr(after, "guest", 0))
            + float(getattr(after, "guest_nice", 0))
            - float(getattr(before, "guest", 0))
            - float(getattr(before, "guest_nice", 0))
        )
        total = a - b - max(0.0, guest_delta)
        if total <= 0:
            return None, 0
        idle_delta = (
            float(getattr(after, "idle", 0))
            + float(getattr(after, "iowait", 0))
            - float(getattr(before, "idle", 0))
            - float(getattr(before, "iowait", 0))
        )
        busy = (total - idle_delta) / total
        return min(1.0, max(0.0, busy)), 1
    except Exception:
        return None, 0


def _nvml(
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], bool, str | None]:
    """Collect physical GPU and visible compute process data once."""
    try:
        import pynvml

        pynvml.nvmlInit()
    except Exception as exc:
        return [], {}, False, _reason(exc)
    gpus: list[dict[str, Any]] = []
    procmap: dict[int, dict[str, Any]] = {}
    process_records = 0
    nvml_error: str | None = None
    try:
        raw_count = int(pynvml.nvmlDeviceGetCount())
        if raw_count < 0:
            raise ValueError("invalid gpu count")
        count = min(raw_count, MAX_GPUS)
        if raw_count > MAX_GPUS:
            nvml_error = "gpu_count_truncated"
        for index in range(count):
            if deadline is not None and time.monotonic() >= deadline:
                nvml_error = nvml_error or "collection_deadline"
                break
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            raw_uuid = _text(_call(pynvml.nvmlDeviceGetUUID, handle))
            uuid_unavailable = raw_uuid is None
            if uuid_unavailable:
                # Keep the physical row usable while making the loss of the
                # stable identity explicit. The identity string is unique to
                # this snapshot and must never be treated as durable.
                uuid = _ephemeral_gpu_identity(index)
                nvml_error = nvml_error or "uuid_unavailable"
            else:
                uuid = raw_uuid
            name = _call(pynvml.nvmlDeviceGetName, handle)
            util = _call(pynvml.nvmlDeviceGetUtilizationRates, handle)
            mem = _call(pynvml.nvmlDeviceGetMemoryInfo, handle)
            try:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle) or []
            except Exception as exc:
                procs = []
                nvml_error = nvml_error or _reason(exc)
            mig_mode = _call(getattr(pynvml, "nvmlDeviceGetMigMode", None), handle)
            mig_detected = bool(
                isinstance(mig_mode, (tuple, list)) and mig_mode and mig_mode[0] == 1
            )
            gpu = {
                "uuid": uuid,
                "index": index,
                "model": _text(name),
                "utilization_fraction": _nvml_fraction(util),
                "vram_total_bytes": _nonnegative_int(getattr(mem, "total", None)),
                "vram_used_bytes": _nonnegative_int(getattr(mem, "used", None)),
                "temperature_c": _finite_number(
                    _call(getattr(pynvml, "nvmlDeviceGetTemperature", None), handle, 0)
                ),
                "power_watts": _power_watts(pynvml, handle),
                "compute_process_count": len(procs),
                "supported": True,
                "error": "uuid_unavailable" if uuid_unavailable else None,
                "mig_detected": mig_detected,
                "instance_supported": False,
            }
            gpus.append(gpu)
            for p in procs:
                if deadline is not None and time.monotonic() >= deadline:
                    nvml_error = nvml_error or "collection_deadline"
                    break
                if process_records >= MAX_COUNTER_PROCESSES:
                    nvml_error = nvml_error or "gpu_processes_truncated"
                    break
                process_records += 1
                try:
                    pid = int(getattr(p, "pid", 0))
                except (TypeError, ValueError, OverflowError):
                    nvml_error = nvml_error or "invalid_process_pid"
                    continue
                if pid <= 0:
                    continue
                used = _nvml_memory_value(pynvml, getattr(p, "usedGpuMemory", None))
                allocation = {
                    "gpu_uuid": gpu["uuid"],
                    "gpu_index": index,
                    "vram_bytes": used,
                }
                process_gpu = procmap.setdefault(
                    pid,
                    {
                        "gpu_process": True,
                        "gpu_uuid": gpu["uuid"],
                        "gpu_index": index,
                        "gpu_allocations": [],
                        "vram_bytes": 0,
                        "_vram_unknown": False,
                    },
                )
                if len(process_gpu["gpu_allocations"]) < MAX_GPU_ALLOCATIONS:
                    process_gpu["gpu_allocations"].append(allocation)
                else:
                    process_gpu["_vram_unknown"] = True
                if len(process_gpu["gpu_allocations"]) > 1:
                    process_gpu["gpu_uuid"] = None
                    process_gpu["gpu_index"] = None
                if used is not None:
                    process_gpu["vram_bytes"] += used
                else:
                    process_gpu["_vram_unknown"] = True
        for process_gpu in procmap.values():
            if process_gpu.pop("_vram_unknown", False):
                process_gpu["vram_bytes"] = None
    except Exception as exc:
        for process_gpu in procmap.values():
            if process_gpu.pop("_vram_unknown", False):
                process_gpu["vram_bytes"] = None
        return gpus, procmap, True, nvml_error or _reason(exc)
    finally:
        with suppress(Exception):
            pynvml.nvmlShutdown()
    return gpus, procmap, True, nvml_error


def _call(fn: Any, *args: Any) -> Any:
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = value.decode(errors="replace") if isinstance(value, bytes) else str(value)
    return result[:MAX_STRING_LENGTH]


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0 or int(value) != value:
        return None
    return int(value)


def _nvml_fraction(utilization: Any) -> float | None:
    value = getattr(utilization, "gpu", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return min(1.0, max(0.0, float(value) / 100.0))


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def _power_watts(pynvml: Any, handle: Any) -> float | None:
    value = _call(getattr(pynvml, "nvmlDeviceGetPowerUsage", None), handle)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return value / 1000


def _nvml_memory_value(pynvml: Any, value: Any) -> int | None:
    unavailable = getattr(pynvml, "NVML_VALUE_NOT_AVAILABLE", None)
    if value is None or value == unavailable or isinstance(value, bool):
        return None
    if (
        not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or int(value) != value
    ):
        return None
    return int(value)


def _process_cpu_total(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value.user) + float(value.system)
    except (AttributeError, TypeError, ValueError):
        try:
            return float(value[0]) + float(value[1])
        except (IndexError, TypeError, ValueError):
            return None


def collect_snapshot(
    *, window_seconds: float = DEFAULT_WINDOW_SECONDS
) -> dict[str, Any]:
    window = min(max(float(window_seconds), 0.0), MAX_WINDOW_SECONDS)
    started = time.monotonic()
    captured = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        import psutil
    except Exception as exc:
        psutil = None
        psutil_error = _reason(exc)
    else:
        psutil_error = None
    cpu_before = cpu_after = None
    proc_before: dict[tuple[int, float | None], Any] = {}
    visibility = {
        "partial": False,
        "permission_denied": 0,
        "processes_visible": 0,
        "counters_truncated": False,
    }
    counter_truncated = False
    deadline = started + MAX_COLLECTION_SECONDS
    if psutil:
        with suppress(Exception):
            psutil.process_iter.cache_clear()
        try:
            cpu_before = psutil.cpu_times()
            for p in psutil.process_iter(["cpu_times", "create_time"]):
                if time.monotonic() >= deadline:
                    visibility["partial"] = True
                    break
                try:
                    if len(proc_before) < MAX_COUNTER_PROCESSES:
                        proc_before[(p.pid, p.info.get("create_time"))] = p.info.get(
                            "cpu_times"
                        )
                    else:
                        counter_truncated = True
                        break
                except psutil.AccessDenied:
                    visibility["partial"] = True
                    visibility["permission_denied"] += 1
                except psutil.NoSuchProcess:
                    visibility["partial"] = True
        except Exception:
            visibility["partial"] = True
        with suppress(Exception):
            psutil.process_iter.cache_clear()
    remaining = max(0.0, deadline - time.monotonic())
    sleep_window = min(window, remaining)
    if sleep_window < window:
        visibility["partial"] = True
    observation_started = time.monotonic()
    time.sleep(sleep_window)
    observation_duration = time.monotonic() - observation_started
    if psutil:
        with suppress(Exception):
            cpu_after = psutil.cpu_times()
    busy, _ = (
        _cpu_delta(cpu_before, cpu_after)
        if cpu_before is not None and cpu_after is not None
        else (None, 0)
    )
    gpus, gpu_proc, gpu_supported, gpu_error = _nvml(deadline)
    # Bounded rich selection: retain only the top MAX_PROCESSES records as
    # ranked by (gpu usage, cpu, rss) instead of every scanned dict. Ties
    # keep scan order so the result matches a stable full sort. User totals
    # below still aggregate every scanned process accurately.
    top: list[tuple[tuple[bool, float, int], int, dict[str, Any]]] = []
    scanned_processes = 0
    users: dict[object, dict[str, Any]] = {}
    username_cache: dict[int, str | None] = {}
    scan_truncated = False
    if psutil:
        for scanned, p in enumerate(
            psutil.process_iter(
                [
                    "pid",
                    "name",
                    "exe",
                    "uids",
                    "memory_info",
                    "create_time",
                    "cpu_times",
                ]
            ),
            start=1,
        ):
            if scanned > MAX_SCAN_PROCESSES:
                visibility["partial"] = True
                visibility["counters_truncated"] = True
                scan_truncated = True
                break
            if time.monotonic() >= deadline:
                visibility["partial"] = True
                break
            try:
                info = p.info
                uid = info.get("uids") and info["uids"].real
                rss = _nonnegative_int(getattr(info.get("memory_info"), "rss", None))
                after_t = info.get("cpu_times")
                before_t = proc_before.get((p.pid, info.get("create_time")))
                before_cpu = _process_cpu_total(before_t)
                after_cpu = _process_cpu_total(after_t)
                cpu = (
                    (after_cpu - before_cpu) / max(observation_duration, 1e-6)
                    if sleep_window > 0
                    and after_cpu is not None
                    and before_cpu is not None
                    else None
                )
                cpu = max(0.0, cpu) if cpu is not None else None
                name = _text(info.get("name"))
                executable = os.path.basename(info.get("exe") or "") or None
                rec = {
                    "pid": p.pid,
                    "create_time": info.get("create_time"),
                    "uid": uid,
                    "username": _username(uid, username_cache),
                    "name": name,
                    "executable": _text(executable),
                    "cpu_cores": cpu,
                    "rss_bytes": rss,
                    **gpu_proc.get(p.pid, {}),
                }
                scanned_processes += 1
                rank = (
                    bool(rec.get("gpu_process")),
                    rec.get("cpu_cores") or 0,
                    rec.get("rss_bytes") or 0,
                )
                if len(top) < MAX_PROCESSES:
                    heapq.heappush(top, (rank, -scanned_processes, rec))
                elif (rank, -scanned_processes) > top[0][:2]:
                    heapq.heapreplace(top, (rank, -scanned_processes, rec))
                user_key: object = uid if uid is not None else ("unknown",)
                u = users.setdefault(
                    user_key,
                    {
                        "uid": uid,
                        "username": rec["username"],
                        "cpu_cores": 0.0,
                        "rss_bytes": 0,
                        "process_count": 0,
                        "gpu_process_count": 0,
                        "vram_bytes": 0,
                    },
                )
                if cpu is not None and u["cpu_cores"] is not None:
                    u["cpu_cores"] += cpu
                if rss is None:
                    u["rss_bytes"] = None
                elif u["rss_bytes"] is not None:
                    u["rss_bytes"] += rss
                u["process_count"] += 1
                if p.pid in gpu_proc:
                    u["gpu_process_count"] += 1
                    gpu_vram = gpu_proc[p.pid].get("vram_bytes")
                    if gpu_vram is None:
                        u["vram_bytes"] = None
                    elif u["vram_bytes"] is not None:
                        u["vram_bytes"] += gpu_vram
            except psutil.AccessDenied:
                visibility["partial"] = True
                visibility["permission_denied"] += 1
            except psutil.NoSuchProcess:
                visibility["partial"] = True
            except Exception:
                visibility["partial"] = True
    if psutil:
        with suppress(Exception):
            psutil.process_iter.cache_clear()
    proc_before.clear()
    visibility["processes_visible"] = scanned_processes
    # NVML can see compute processes the bounded psutil scan missed (scan
    # deadline, scan cap, short-lived processes).  Attribute them now from a
    # minimal /proc identity so GPU ownership is never lost to the race
    # between the scan and the NVML read.  Each supplement ranks as a GPU
    # process and evicts the lowest-ranked non-GPU record when the top
    # buffer is already full.
    supplement_evicted = False
    emitted_pids = {rec["pid"] for _rank, _sequence, rec in top}
    supplement_index = 0
    for pid, gpu_record in gpu_proc.items():
        if pid in emitted_pids:
            continue
        if len(top) == MAX_PROCESSES and top[0][0][0]:
            break
        uid, name = _proc_owner(pid)
        record = {
            "pid": pid,
            "create_time": None,
            "uid": uid,
            "username": _username(uid, username_cache) if uid is not None else None,
            "name": name,
            "executable": None,
            "cpu_cores": None,
            "rss_bytes": None,
            **gpu_record,
        }
        entry = ((True, 0, 0), -(MAX_SCAN_PROCESSES + supplement_index), record)
        if len(top) < MAX_PROCESSES:
            heapq.heappush(top, entry)
        else:
            # The loop breaks out once the whole window is GPU records, so
            # the minimum here is always a non-GPU record and loses.
            heapq.heapreplace(top, entry)
            supplement_evicted = True
        emitted_pids.add(pid)
        supplement_index += 1
        visibility["processes_visible"] += 1
        user_key: object = uid if uid is not None else ("unknown",)
        user = users.setdefault(
            user_key,
            {
                "uid": uid,
                "username": record["username"],
                "cpu_cores": 0.0,
                "rss_bytes": 0,
                "process_count": 0,
                "gpu_process_count": 0,
                "vram_bytes": 0,
            },
        )
        user["process_count"] += 1
        user["gpu_process_count"] += 1
        supplement_vram = gpu_record.get("vram_bytes")
        if supplement_vram is None:
            user["vram_bytes"] = None
        elif user["vram_bytes"] is not None:
            user["vram_bytes"] += supplement_vram
    if not gpu_supported:
        for user in users.values():
            user["gpu_process_count"] = None
            user["vram_bytes"] = None
    if counter_truncated or scan_truncated:
        visibility["partial"] = True
        visibility["counters_truncated"] = True
    top.sort(key=lambda item: (item[0], item[1]), reverse=True)
    processes = [rec for _rank, _sequence, rec in top]
    users_truncated = len(users) > MAX_USERS
    truncated = (
        scan_truncated
        or scanned_processes > MAX_PROCESSES
        or users_truncated
        or supplement_evicted
    )
    users_list = sorted(
        users.values(),
        key=lambda x: ((x["gpu_process_count"] or 0) > 0, x["cpu_cores"] or 0),
        reverse=True,
    )[:MAX_USERS]
    try:
        logical = (psutil.cpu_count() if psutil else None) or os.cpu_count() or 1
    except Exception:
        logical = os.cpu_count() or 1
    memory: dict[str, int | None] = {
        "total_bytes": None,
        "available_bytes": None,
        "used_bytes": None,
        "swap_total_bytes": None,
        "swap_used_bytes": None,
    }
    disk: dict[str, int | None] = {"total_bytes": None, "free_bytes": None}
    if psutil:
        try:
            m = psutil.virtual_memory()
            memory.update(
                total_bytes=m.total, available_bytes=m.available, used_bytes=m.used
            )
        except Exception:
            pass
        try:
            s = psutil.swap_memory()
            memory.update(swap_total_bytes=s.total, swap_used_bytes=s.used)
        except Exception:
            pass
    try:
        st = os.statvfs("/")
        disk.update(
            total_bytes=st.f_blocks * st.f_frsize, free_bytes=st.f_bavail * st.f_frsize
        )
    except OSError:
        pass
    visibility["processes_emitted"] = len(processes)
    collection_duration = time.monotonic() - started
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured,
        "observation_duration_seconds": observation_duration,
        "collection_duration_seconds": collection_duration,
        "boot_id": _boot_id(),
        "helper_version": HELPER_VERSION,
        "status": "partial"
        if (psutil_error or gpu_error or visibility["partial"] or truncated)
        else "ok",
        "cpu": {
            "logical_count": logical,
            "busy_fraction": busy,
            "load_1m": _load(0),
            "load_5m": _load(1),
            "load_15m": _load(2),
        },
        "memory": memory,
        "disk": disk,
        "gpus": gpus,
        "users": users_list,
        "processes": processes,
        "visibility": visibility,
        "limits": {
            "processes": MAX_PROCESSES,
            "users": MAX_USERS,
            "truncated": truncated,
        },
        "capabilities": {
            "nvml_supported": gpu_supported,
            "nvml_error": gpu_error,
            "psutil_error": psutil_error,
        },
    }


def _load(index: int) -> float | None:
    try:
        return os.getloadavg()[index]
    except OSError:
        return None


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return _text(f.read().strip()) or "unknown"
    except OSError:
        # A host name is unnecessary for reboot detection and may disclose
        # deployment details.  The hub already knows the target name.
        return "unknown"


def snapshot_json(*, window_seconds: float = DEFAULT_WINDOW_SECONDS) -> bytes:
    document = collect_snapshot(window_seconds=window_seconds)
    while True:
        try:
            encoded = encode_snapshot(document)
        except ProtocolError as exc:
            if str(exc) != "snapshot exceeds byte limit":
                raise
            document["limits"]["truncated"] = True
            document["status"] = "partial"
            if document["processes"]:
                document["processes"].pop()
                document["visibility"]["processes_emitted"] = len(document["processes"])
                continue
            if document["users"]:
                document["users"].pop()
                continue
            raise
        validate_snapshot(document, encoded_size=len(encoded))
        return encoded


def main() -> int:
    """CLI entry point: emit exactly one bounded JSON document."""
    import sys

    try:
        sys.stdout.buffer.write(snapshot_json())
        sys.stdout.buffer.write(b"\n")
    except Exception as exc:
        # Keep failures bounded and machine-readable where possible.
        sys.stderr.write(f"snapshot failed: {type(exc).__name__}\n")
        return 1
    return 0
