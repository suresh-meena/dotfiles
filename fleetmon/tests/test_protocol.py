import pytest

from fleetmon.protocol import (
    ProtocolError,
    decode_snapshot,
    encode_snapshot,
    validate_snapshot,
)


def minimal():
    return {
        "schema_version": 1,
        "captured_at": "2026-01-01T00:00:00Z",
        "observation_duration_seconds": 0.25,
        "collection_duration_seconds": 0.5,
        "boot_id": "b",
        "helper_version": "1",
        "status": "ok",
        "cpu": {"logical_count": 2, "busy_fraction": 0.5},
        "memory": {},
        "disk": {},
        "visibility": {
            "partial": False,
            "permission_denied": 0,
            "processes_visible": 0,
            "processes_emitted": 0,
            "counters_truncated": False,
        },
        "limits": {"processes": 80, "users": 128, "truncated": False},
        "capabilities": {
            "nvml_supported": True,
            "nvml_error": None,
            "psutil_error": None,
        },
        "gpus": [],
        "users": [],
        "processes": [],
    }


def test_round_trip_and_validation():
    blob = encode_snapshot(minimal())
    assert decode_snapshot(blob)["schema_version"] == 1
    assert validate_snapshot(minimal())["status"] == "ok"


def test_rejects_oversize_and_wrong_schema():
    with pytest.raises(ProtocolError):
        encode_snapshot({"x": "a" * 300_000})
    bad = minimal()
    bad["schema_version"] = 99
    with pytest.raises(ProtocolError):
        validate_snapshot(bad)


def test_observation_duration_is_required():
    document = minimal()
    del document["observation_duration_seconds"]
    with pytest.raises(ProtocolError):
        validate_snapshot(document)


def test_collection_duration_is_required_and_bounded():
    document = minimal()
    del document["collection_duration_seconds"]
    with pytest.raises(ProtocolError):
        validate_snapshot(document)
    document["collection_duration_seconds"] = 5.1
    with pytest.raises(ProtocolError):
        validate_snapshot(document)


def test_rejects_unpaired_unicode_surrogate_as_protocol_error():
    document = minimal()
    document["boot_id"] = "\ud800"
    with pytest.raises(ProtocolError, match="UTF-8"):
        validate_snapshot(document)


def test_rejects_integer_that_sqlite_cannot_store():
    document = minimal()
    document["memory"]["total_bytes"] = 2**63
    with pytest.raises(ProtocolError):
        validate_snapshot(document)


@pytest.mark.parametrize(
    "section,total_key,value_key",
    [
        ("memory", "total_bytes", "used_bytes"),
        ("disk", "total_bytes", "free_bytes"),
    ],
)
def test_rejects_used_or_free_values_above_total(section, total_key, value_key):
    document = minimal()
    document[section].update({total_key: 1, value_key: 2})
    with pytest.raises(ProtocolError):
        validate_snapshot(document)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("status", "healthy"),
        ("captured_at", "not-a-time"),
    ],
)
def test_rejects_ambiguous_core_types(field, value):
    document = minimal()
    document[field] = value
    with pytest.raises(ProtocolError):
        validate_snapshot(document)


def test_rejects_privacy_sensitive_process_fields():
    document = minimal()
    document["processes"] = [
        {
            "pid": 1,
            "cmdline": ["python", "--token", "secret"],
        }
    ]
    with pytest.raises(ProtocolError, match="forbidden"):
        validate_snapshot(document)


def test_rejects_unrecognised_fields_and_numeric_booleans():
    document = minimal()
    document["unexpected"] = "do not persist"
    with pytest.raises(ProtocolError, match="unknown snapshot"):
        validate_snapshot(document)

    document = minimal()
    document["cpu"]["logical_count"] = True
    with pytest.raises(ProtocolError):
        validate_snapshot(document)

    document = minimal()
    document["cpu"]["load_1m"] = 10**1000
    with pytest.raises(ProtocolError):
        validate_snapshot(document)


def test_accepts_partial_optional_capability_metadata():
    document = minimal()
    document["status"] = "partial"
    document["capabilities"] = {
        "nvml_supported": False,
        "nvml_error": "nvml_missing",
        "psutil_error": None,
    }
    document["visibility"] = {
        "partial": True,
        "permission_denied": 2,
        "processes_visible": 10,
        "processes_emitted": 8,
        "counters_truncated": False,
    }
    document["limits"]["truncated"] = True
    document["processes"] = [{"pid": pid + 1} for pid in range(8)]
    assert validate_snapshot(document)["status"] == "partial"


def test_rejects_duplicate_storage_identities_and_inconsistent_counts():
    document = minimal()
    document["gpus"] = [
        {
            "uuid": "same",
            "index": index,
            "compute_process_count": 0,
            "supported": True,
            "mig_detected": False,
            "instance_supported": False,
        }
        for index in range(2)
    ]
    with pytest.raises(ProtocolError, match="duplicate gpu"):
        validate_snapshot(document)

    document = minimal()
    document["processes"] = [{"pid": 1}, {"pid": 1}]
    document["visibility"]["processes_visible"] = 2
    document["visibility"]["processes_emitted"] = 2
    with pytest.raises(ProtocolError, match="duplicate process"):
        validate_snapshot(document)


def test_rejects_partial_evidence_reported_as_ok():
    document = minimal()
    document["capabilities"]["nvml_error"] = "permission"
    with pytest.raises(ProtocolError, match="partial data"):
        validate_snapshot(document)


def test_user_unknown_memory_is_not_coerced_to_zero():
    document = minimal()
    document["users"] = [
        {
            "uid": 1000,
            "username": "u",
            "cpu_cores": 0,
            "rss_bytes": None,
            "process_count": 1,
            "gpu_process_count": None,
            "vram_bytes": None,
        }
    ]
    validated = validate_snapshot(document)["users"][0]
    assert validated["gpu_process_count"] is None
    assert validated["vram_bytes"] is None


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update({"x": [[[[[[[[[1]]]]]]]]]}),
        lambda d: d["cpu"].update({"busy_fraction": float("nan")}),
        lambda d: d.update({"processes": [{}] * 81}),
    ],
)
def test_rejects_depth_nonfinite_and_count_overflow(change):
    doc = minimal()
    change(doc)
    with pytest.raises(ProtocolError):
        validate_snapshot(doc)
