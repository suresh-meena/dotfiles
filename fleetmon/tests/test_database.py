import sqlite3

import pytest

from fleetmon.database import (
    MAX_STORED_GPU_ALLOCATIONS,
    Database,
    DatabaseVersionError,
)


def sample():
    return {
        "helper_version": "1",
        "status": "partial",
        "collection_duration_seconds": 0.3,
        "observation_duration_seconds": 0.25,
        "boot_id": "boot-1",
        "captured_at": "2026-01-01T00:00:00Z",
        "cpu": {
            "busy_fraction": 0.5,
            "load_1m": 1,
            "load_5m": 2,
            "load_15m": 3,
        },
        "memory": {"total_bytes": 100, "used_bytes": 50},
        "disk": {"total_bytes": 200, "free_bytes": 100},
        "gpus": [{"uuid": "u", "index": 0}],
        "users": [
            {
                "uid": 1,
                "cpu_cores": 0.5,
                "rss_bytes": 10,
                "process_count": 1,
                "gpu_process_count": 0,
                "vram_bytes": 0,
            }
        ],
        "processes": [{"pid": 2}],
        "visibility": {
            "partial": False,
            "permission_denied": 0,
            "processes_visible": 1,
            "processes_emitted": 1,
            "counters_truncated": False,
        },
        "limits": {"processes": 80, "users": 128, "truncated": False},
        "capabilities": {
            "nvml_supported": True,
            "nvml_error": None,
            "psutil_error": None,
        },
    }


def test_atomic_snapshot_and_wire_keys(tmp_path):
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", sample(), 10)
    row = db.query("select * from host_samples")[0]
    assert (
        row["load5"] == 2
        and row["partial"] == 1
        and len(db.current_processes("h")) == 1
    )
    assert row["boot_id"] == "boot-1"
    assert row["observation_duration_seconds"] == 0.25
    assert row["visible_processes"] == 1 and row["emitted_processes"] == 1
    assert row["permission_denied"] == 0
    assert row["counters_truncated"] == 0 and row["limits_truncated"] == 0
    assert row["nvml_supported"] == 1
    assert row["nvml_error"] is None and row["psutil_error"] is None


def test_minimal_sample_defaults_keep_missing_operational_fields_unknown(tmp_path):
    document = sample()
    del document["boot_id"]
    del document["observation_duration_seconds"]
    del document["visibility"]
    del document["limits"]
    del document["capabilities"]
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", document, 10)
    row = db.query("select * from host_samples")[0]
    assert row["boot_id"] is None
    assert row["observation_duration_seconds"] is None
    assert row["visible_processes"] == 0 and row["emitted_processes"] == 0
    assert row["nvml_supported"] is None


def test_snapshot_stores_all_gpu_columns(tmp_path):
    document = sample()
    document["gpus"] = [
        {
            "uuid": "gpu-1",
            "index": 2,
            "model": "A100",
            "utilization_fraction": 0.75,
            "vram_total_bytes": 1000,
            "vram_used_bytes": 250,
            "temperature_c": 55,
            "power_watts": 120,
            "compute_process_count": 3,
            "supported": True,
            "error": None,
            "mig_detected": True,
            "instance_supported": False,
        }
    ]
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", document, 10)
    row = db.query("SELECT * FROM gpu_samples")[0]
    assert dict(row) == {
        "poll_id": "p",
        "uuid": "gpu-1",
        "idx": 2,
        "model": "A100",
        "utilization": 0.75,
        "vram_total": 1000,
        "vram_used": 250,
        "temperature_c": 55,
        "power_watts": 120,
        "compute_process_count": 3,
        "supported": 1,
        "error": None,
        "mig_detected": 1,
        "instance_supported": 0,
    }


def test_gpu_error_and_instance_support_flags_are_stored(tmp_path):
    document = sample()
    document["gpus"] = [
        {
            "uuid": "gpu-1",
            "index": 0,
            "supported": True,
            "error": "uuid_unavailable",
            "mig_detected": True,
            "instance_supported": False,
        }
    ]
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", document, 10)
    row = db.query("SELECT error, instance_supported FROM gpu_samples")[0]
    assert row["error"] == "uuid_unavailable"
    assert row["instance_supported"] == 0


def test_truncated_and_partial_sample_flags_are_stored(tmp_path):
    document = sample()
    document["visibility"] = {
        "partial": True,
        "permission_denied": 3,
        "processes_visible": 9,
        "processes_emitted": 8,
        "counters_truncated": True,
    }
    document["limits"] = {"processes": 80, "users": 128, "truncated": True}
    document["capabilities"] = {
        "nvml_supported": False,
        "nvml_error": "nvml_missing",
        "psutil_error": None,
    }
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", document, 10)
    row = db.query("SELECT * FROM host_samples")[0]
    assert row["visible_processes"] == 9
    assert row["emitted_processes"] == 8
    assert row["permission_denied"] == 3
    assert row["counters_truncated"] == 1
    assert row["limits_truncated"] == 1
    assert row["nvml_supported"] == 0
    assert row["nvml_error"] == "nvml_missing"


def test_process_gpu_association_is_stored(tmp_path):
    document = sample()
    document["processes"] = [
        {
            "pid": 2,
            "gpu_process": True,
            "gpu_uuid": "gpu-1",
            "gpu_index": 3,
            "vram_bytes": 100,
            "gpu_allocations": [
                {"gpu_uuid": "gpu-1", "gpu_index": 3, "vram_bytes": 60},
                {"gpu_uuid": "gpu-2", "gpu_index": 4, "vram_bytes": 40},
            ],
        }
    ]
    db = Database(tmp_path / "x.db")
    db.upsert_host("h", "compute", "direct")
    db.snapshot("p", "h", document, 10)
    process = db.current_processes("h")[0]
    assert process["gpu_uuid"] == "gpu-1"
    assert process["gpu_index"] == 3
    assert process["vram"] == 100
    allocations = db.query(
        """
        SELECT position, gpu_uuid, gpu_index, vram_bytes
        FROM current_process_allocations ORDER BY position
        """
    )
    assert [
        (row["position"], row["gpu_uuid"], row["gpu_index"], row["vram_bytes"])
        for row in allocations
    ] == [(0, "gpu-1", 3, 60), (1, "gpu-2", 4, 40)]
    view = db.host("h")
    assert view["processes"][0]["gpu_allocations"] == [
        {"position": 0, "gpu_uuid": "gpu-1", "gpu_index": 3, "vram_bytes": 60},
        {"position": 1, "gpu_uuid": "gpu-2", "gpu_index": 4, "vram_bytes": 40},
    ]


def test_process_allocations_are_bounded(tmp_path):
    document = sample()
    document["processes"] = [
        {
            "pid": 2,
            "gpu_allocations": [
                {"gpu_uuid": f"gpu-{i}", "gpu_index": i, "vram_bytes": i}
                for i in range(12)
            ],
        }
    ]
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", document, 10)
    rows = db.query(
        "SELECT position FROM current_process_allocations ORDER BY position"
    )
    assert [row["position"] for row in rows] == list(range(MAX_STORED_GPU_ALLOCATIONS))


def test_overview_contains_only_the_latest_sample_metrics(tmp_path):
    db = Database(tmp_path / "x.db")
    db.upsert_host("h", "compute", "direct")
    db.snapshot("old", "h", sample(), 10)
    newer = sample()
    newer["cpu"]["load_1m"] = 9
    newer["gpus"][0]["utilization_fraction"] = 0.75
    newer["visibility"]["processes_visible"] = 4
    db.snapshot("new", "h", newer, 20)
    row = db.overview()[0]
    assert row["load1"] == 9
    assert row["gpu_count"] == 1
    assert row["gpu_utilization"] == 0.75
    assert row["visible_users"] == 1
    assert row["collection_duration_seconds"] == 0.3
    assert row["sample_boot_id"] == "boot-1"
    assert row["sample_observation_duration_seconds"] == 0.25
    assert row["sample_visible_processes"] == 4
    assert row["sample_emitted_processes"] == 1


def test_duplicate_poll_rolls_back(tmp_path):
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", sample(), 10)
    with pytest.raises(sqlite3.IntegrityError):
        db.snapshot("p", "h", sample(), 11)
    assert len(db.query("select * from polls")) == 1


def test_writer_restart_preserves_history(tmp_path):
    path = tmp_path / "x.db"
    db = Database(path)
    db.snapshot("p", "h", sample(), 10)
    db.upsert_slurm_jobs("c", [{"job_id": 1, "state": "RUNNING"}])
    db.close()
    reopened = Database(path)
    try:
        assert [
            row["poll_id"] for row in reopened.query("select poll_id from polls")
        ] == ["p"]
        assert (
            reopened.query("select boot_id from host_samples")[0]["boot_id"] == "boot-1"
        )
        assert reopened.query("PRAGMA integrity_check")[0]["integrity_check"] == "ok"
        assert reopened.query("PRAGMA foreign_key_check") == []
    finally:
        reopened.close()


def test_integrity_and_foreign_keys_stay_clean_after_retention(tmp_path):
    db = Database(tmp_path / "x.db")
    try:
        db.upsert_host("h", "compute", "direct")
        db.snapshot("p", "h", sample(), 1)
        db.retain(2)
        assert db.query("PRAGMA integrity_check")[0]["integrity_check"] == "ok"
        assert db.query("PRAGMA foreign_key_check") == []
        assert db.current_processes("h") == []
    finally:
        db.close()


def test_retention_cascades_children(tmp_path):
    db = Database(tmp_path / "x.db")
    db.snapshot("p", "h", sample(), 1)
    db.retain(2)
    assert not db.query("select * from polls") and not db.query(
        "select * from gpu_samples"
    )


def test_retention_removes_all_terminal_slurm_jobs_but_keeps_active(tmp_path):
    db = Database(tmp_path / "x.db")
    db.upsert_slurm_jobs(
        "h",
        [
            {"job_id": 1, "state": "FAILED"},
            {"job_id": 2, "state": "TIMEOUT"},
            {"job_id": 3, "state": "NODE_FAIL"},
            {"job_id": 4, "state": "RUNNING"},
        ],
        updated_at=1,
    )
    db.retain(2)
    assert [row["job_id"] for row in db.jobs()] == ["4"]


def test_slurm_job_state_alias_is_normalized_for_active_filter(tmp_path):
    db = Database(tmp_path / "x.db")
    db.upsert_slurm_jobs("h", [{"job_id": 9, "job_state": {"name": "RUNNING"}}])
    row = db.jobs(active_only=True)[0]
    assert row["state"] == "RUNNING"


def test_slurm_jobs_use_row_cluster_and_normalize_state_shapes(tmp_path):
    db = Database(tmp_path / "x.db")
    db.upsert_slurm_jobs(
        "target-cluster",
        [
            {"cluster": "row-cluster", "job_id": 1, "state": ["RUNNING"]},
            {"job_id": 2, "state": {"name": "COMPLETED"}},
            {"job_id": 3, "state": "FAILED"},
        ],
    )
    rows = {row["job_id"]: row for row in db.jobs()}
    assert rows["1"]["cluster"] == "row-cluster"
    assert rows["2"]["cluster"] == "target-cluster"
    assert rows["1"]["state"] == "RUNNING"
    assert rows["2"]["state"] == "COMPLETED"
    assert [row["job_id"] for row in db.jobs(active_only=True)] == ["1"]


def test_slurm_job_storage_rejects_nan_and_bounds_batch(tmp_path):
    db = Database(tmp_path / "x.db")
    with pytest.raises(ValueError, match="Out of range float values"):
        db.upsert_slurm_jobs("h", [{"job_id": 1, "score": float("nan")}])
    assert db.query("SELECT COUNT(*) AS count FROM slurm_jobs")[0]["count"] == 0

    db.upsert_slurm_jobs(
        "h", [{"job_id": i, "state": "RUNNING"} for i in range(1, 2_002)]
    )
    assert db.query("SELECT COUNT(*) AS count FROM slurm_jobs")[0]["count"] == 2_000


def test_slurm_identifiers_reject_control_characters_and_length(tmp_path):
    db = Database(tmp_path / "x.db")
    try:
        with pytest.raises(ValueError):
            db.upsert_slurm_jobs("bad\ncluster", [{"job_id": 1}])
        with pytest.raises(ValueError):
            db.upsert_slurm_jobs("c", [{"job_id": "1\x7f2"}])
        with pytest.raises(ValueError):
            db.upsert_slurm_jobs("c", [{"job_id": 1, "array_task_id": "a" * 257}])
        with pytest.raises(ValueError):
            db.upsert_slurm_jobs("c", [{"job_id": 1, "step_id": "s\tb"}])
        with pytest.raises(ValueError):
            db.upsert_slurm_jobs("c", [{"job_id": 1, "cluster": "x" * 257}])
        with pytest.raises(ValueError):
            db.upsert_slurm_jobs("c", [{"job_id": {"deep": "object"}}])
        db.upsert_slurm_jobs("c", [{"job_id": None}])
        assert db.query("SELECT COUNT(*) AS count FROM slurm_jobs")[0]["count"] == 0
    finally:
        db.close()


def test_slurm_identifier_boundaries_are_accepted(tmp_path):
    db = Database(tmp_path / "x.db")
    try:
        db.upsert_slurm_jobs(
            "c" * 256,
            [{"job_id": 1, "array_task_id": "a" * 256, "step_id": ""}],
        )
        row = db.jobs()[0]
        assert row["cluster"] == "c" * 256
        assert row["array_task_id"] == "a" * 256
        assert row["step_id"] == ""
    finally:
        db.close()


def test_target_and_poll_id_entry_points_validate_untrusted_strings(tmp_path):
    db = Database(tmp_path / "x.db")
    try:
        with pytest.raises(ValueError):
            db.upsert_host("bad\x7ftarget", "compute", "direct")
        with pytest.raises(ValueError):
            db.snapshot("p\noll", "h", sample(), 10)
        with pytest.raises(ValueError):
            db.record_error("p", "bad\ntarget", "transport")
        assert db.query("SELECT COUNT(*) AS count FROM polls")[0]["count"] == 0
    finally:
        db.close()


def test_error_text_is_never_persisted(tmp_path):
    db = Database(tmp_path / "x.db")
    db.record_error("p", "h", "secret token from stderr")
    assert db.query("select error from polls")[0]["error"] == "transport"


def test_online_backup_restores_with_integrity_and_row_parity(tmp_path):
    db = Database(tmp_path / "x.db")
    try:
        db.upsert_host("h", "compute", "direct")
        db.snapshot("p", "h", sample(), 10)
        db.upsert_slurm_jobs("c", [{"job_id": 1, "state": "RUNNING"}])
        db.set_slurm_state("login", "watermark", "live")
        destination = tmp_path / "backup.db"
        db.backup(destination)
        assert destination.stat().st_mode & 0o777 == 0o600
        restored = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            for table in (
                "schema_migrations",
                "hosts",
                "polls",
                "host_samples",
                "gpu_samples",
                "user_samples",
                "current_processes",
                "current_process_allocations",
                "slurm_jobs",
                "slurm_poll_state",
            ):
                expected = db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
                actual = restored.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert expected == actual, table
        finally:
            restored.close()
    finally:
        db.close()


def test_backup_refuses_to_overwrite_an_existing_file(tmp_path):
    db = Database(tmp_path / "x.db")
    try:
        destination = tmp_path / "backup.db"
        destination.write_bytes(b"existing")
        with pytest.raises(FileExistsError):
            db.backup(destination)
        assert destination.read_bytes() == b"existing"
    finally:
        db.close()


def test_schema_version_is_recorded(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.query("select version from schema_migrations")[0]["version"] == 2
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert (tmp_path / "x.db").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "x.db-wal").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "x.db-shm").stat().st_mode & 0o777 == 0o600


def test_unversioned_existing_database_is_refused(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE old_data(value TEXT)")
    connection.close()
    with pytest.raises(DatabaseVersionError, match="unversioned"):
        Database(path)


def test_malformed_current_version_schema_is_refused_without_modification(tmp_path):
    path = tmp_path / "mismatch.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=2")
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL)"
    )
    connection.execute("CREATE TABLE polls(poll_id TEXT PRIMARY KEY)")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (2, 0)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(DatabaseVersionError, match="schema"):
        Database(path)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        == 2
    )
    connection.close()


def test_older_version_database_is_refused_without_modification(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    db.snapshot("p", "h", sample(), 10)
    db.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(DatabaseVersionError, match="older"):
        Database(path)
    assert path.read_bytes() == before


def test_newer_version_database_is_refused(tmp_path):
    path = tmp_path / "future.db"
    db = Database(path)
    db.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=3")
    connection.commit()
    connection.close()
    with pytest.raises(DatabaseVersionError, match="newer"):
        Database(path)


def test_host_charts_returns_one_bounded_group_per_target(tmp_path):
    db = Database(tmp_path / "charts.db")
    for index in range(5):
        document = sample()
        document["cpu"]["busy_fraction"] = 0.1 * index
        document["gpus"] = [
            {
                "uuid": "gpu-1",
                "index": 0,
                "model": "A100",
                "utilization_fraction": 0.2 * index,
                "vram_total_bytes": 1000,
                "vram_used_bytes": 100 * index,
            }
        ]
        db.snapshot(f"p{index}", "h", document, 1000 + index)
    charts = db.host_charts("h", points=3)
    by_chart = {item["chart"]: item for item in charts["series"]}
    assert len(by_chart["cpu"]["points"]) == 3
    # Oldest points are dropped when the cap is reached; order is ascending.
    assert by_chart["cpu"]["points"][-1][1] == 0.4
    assert by_chart["ram"]["points"][-1][1] == 0.5
    assert by_chart["disk"]["points"][-1][1] == 0.5
    assert by_chart["gpu_util"]["points"][-1][1] == 0.8
    assert by_chart["gpu_vram"]["points"][-1][1] == 0.4
    assert by_chart["gpu_util"]["label"] == "gpu0 A100"
    missing = db.host_charts("absent")
    assert all(not item["points"] for item in missing["series"])


def test_host_charts_point_cap_matches_the_api_limit(tmp_path):
    db = Database(tmp_path / "cap.db")
    for index in range(9):
        db.snapshot(f"p{index}", "h", sample(), 100 + index)
    charts = db.host_charts("h")
    assert all(len(item["points"]) <= 2000 for item in charts["series"])
    assert len(next(i for i in charts["series"] if i["chart"] == "cpu")["points"]) == 9
    db.close()


def test_sparklines_return_one_bounded_recent_cpu_series_per_host(tmp_path):
    db = Database(tmp_path / "spark.db")
    db.upsert_host("h", "compute", "direct")
    db.upsert_host("quiet", "compute", "direct")
    for index in range(70):
        document = sample()
        document["cpu"]["busy_fraction"] = 0.1 * (index % 10)
        db.snapshot(f"p{index}", "h", document, 1000 + index)
    spark = db.sparklines()
    assert spark["bounded"] is True and spark["points"] == 60
    by_target = {item["target"]: item["points"] for item in spark["series"]}
    assert set(by_target) == {"h", "quiet"}
    points = by_target["h"]
    assert len(points) == 60
    assert points[0][0] < points[-1][0], "ascending receive order"
    assert all(0 <= pair[1] <= 1 for pair in points)
    assert by_target["quiet"] == []
    assert all(
        len(item["points"]) <= 60 for item in db.sparklines(points=5000)["series"]
    )
    db.close()


def test_gpu_recent_returns_latest_two_observations_per_uuid(tmp_path):
    db = Database(tmp_path / "x.db")
    first = sample()
    first["gpus"] = [
        {
            "uuid": "u1",
            "index": 0,
            "utilization_fraction": 0.0,
            "compute_process_count": 0,
        },
        {
            "uuid": "u2",
            "index": 1,
            "utilization_fraction": 1.0,
            "compute_process_count": 1,
        },
    ]
    second = sample()
    second["gpus"] = [
        {
            "uuid": "u1",
            "index": 0,
            "utilization_fraction": 0.0,
            "compute_process_count": 0,
        }
    ]
    third = sample()
    third["gpus"] = [
        {
            "uuid": "u1",
            "index": 0,
            "utilization_fraction": 2.0,
            "compute_process_count": 1,
        }
    ]
    db.snapshot("p1", "h", first, 10)
    db.snapshot("p2", "h", second, 20)
    db.snapshot("p3", "h", third, 30)
    recent = db.gpu_recent("h")
    # The window is exactly the latest two observations; u2 only existed in
    # an older poll and must not be reported from history.
    assert set(recent) == {"u1"}
    newest, older = recent["u1"]
    assert newest["gpu"]["utilization"] == 2.0
    assert older["gpu"]["utilization"] == 0.0
    assert newest["received_at"] == 30 and older["received_at"] == 20
    assert newest["boot_id"] == older["boot_id"] == "boot-1"
    assert db.gpu_recent("other") == {}


def test_gpu_recent_breaks_chain_when_latest_observation_lacks_gpu_rows(tmp_path):
    db = Database(tmp_path / "x.db")
    with_gpus = sample()
    with_gpus["gpus"] = [
        {
            "uuid": "u1",
            "index": 0,
            "utilization_fraction": 0.0,
            "compute_process_count": 0,
        }
    ]
    db.snapshot("p1", "h", with_gpus, 10)
    db.snapshot("p2", "h", with_gpus, 20)
    # A committed observation whose GPU list is empty (e.g. NVML failure).
    empty = sample()
    empty["gpus"] = []
    db.snapshot("p3", "h", empty, 30)
    recent = db.gpu_recent("h")
    assert set(recent) == {"u1"}
    newest, older = recent["u1"]
    assert newest["gpu"] is None, "missing GPU row must not borrow a historic row"
    assert older["gpu"]["utilization"] == 0.0
    from fleetmon import notify

    availability, reason = notify.classify_gpu(newest, older)
    assert availability == "unknown" and reason == "missing_gpu_row"


def test_gpu_recent_errors_break_observation_chain(tmp_path):
    db = Database(tmp_path / "x.db")
    with_gpus = sample()
    with_gpus["gpus"] = [{"uuid": "u1", "index": 0}]
    db.snapshot("p1", "h", with_gpus, 10)
    db.snapshot("p2", "h", with_gpus, 20)
    # A failed latest poll breaks the chain instead of reusing history.
    db.record_error("e1", "h", "transport", 25)
    recent = db.gpu_recent("h")
    assert [slot["received_at"] for slot in recent["u1"]] == [25, 20]
    assert recent["u1"][0]["gpu"] is None


def test_gpu_window_returns_two_observations_per_target(tmp_path):
    db = Database(tmp_path / "x.db")
    db.upsert_host("h1", "compute", "direct")
    db.upsert_host("h2", "compute", "direct")
    document = sample()
    document["gpus"] = [{"uuid": "u1", "index": 0, "utilization_fraction": 0.0}]
    db.snapshot("a1", "h1", document, 10)
    db.snapshot("a2", "h1", document, 20)
    window = db.gpu_window()
    assert set(window) == {"h1", "h2"}
    assert window["h2"] == []
    assert [obs["received_at"] for obs in window["h1"]] == [20, 10]
    assert set(window["h1"][0]["gpus"]) == {"u1"}


def test_downsample_history_keeps_latest_two_and_thins_to_interval(tmp_path):
    db = Database(tmp_path / "x.db")
    document = sample()
    document["gpus"] = [{"uuid": "u1", "index": 0}]
    # Simulate 2-second polling: 0, 2, 4, ..., 120.
    stamps = list(range(0, 121, 2))
    for index, stamp in enumerate(stamps):
        db.snapshot(f"p{index}", "h", document, stamp)
    deleted = db.downsample_history("h", 60)
    assert deleted > 0
    kept = [
        row["received_at"]
        for row in db.query(
            "SELECT ended_at AS received_at FROM polls WHERE target='h' ORDER BY ended_at"
        )
    ]
    # Newest two observations always survive for classification, even when
    # they are closer together than the history interval.
    assert kept[-2:] == [118.0, 120.0]
    # Surviving history below them is spaced at least 60 seconds apart.
    for older, newer in zip(kept, kept[1:-1], strict=False):
        assert newer - older >= 60
    # Children of deleted polls cascade away; integrity stays clean.
    assert db.query("PRAGMA integrity_check")[0]["integrity_check"] == "ok"
    assert db.query("PRAGMA foreign_key_check") == []
    gpu_polls = {row["poll_id"] for row in db.query("SELECT poll_id FROM gpu_samples")}
    assert gpu_polls <= set(
        row["poll_id"] for row in db.query("SELECT poll_id FROM polls")
    )


def test_downsample_history_never_touches_error_polls_or_latest_two(tmp_path):
    db = Database(tmp_path / "x.db")
    document = sample()
    db.snapshot("p0", "h", document, 100)
    db.snapshot("p1", "h", document, 200)
    db.record_error("e0", "h", "timeout", 150)
    deleted = db.downsample_history("h", 60)
    assert deleted == 0
    outcomes = {
        row["poll_id"]: row["outcome"]
        for row in db.query("SELECT poll_id, outcome FROM polls")
    }
    assert set(outcomes) == {"p0", "p1", "e0"}
    # A conservative interval below the actual spacing deletes nothing.
    db.snapshot("p2", "h", document, 300)
    db.snapshot("p3", "h", document, 400)
    assert db.downsample_history("h", 60) == 0
    assert len(db.query("SELECT * FROM polls")) == 5


def test_downsample_history_is_bounded_per_call(tmp_path):
    db = Database(tmp_path / "x.db")
    document = sample()
    for index in range(120):
        db.snapshot(f"p{index}", "h", document, index)
    first_pass = db.downsample_history("h", 60, batch=50)
    assert first_pass <= 50
    remaining = db.query("SELECT COUNT(*) AS n FROM polls")[0]["n"]
    while db.downsample_history("h", 60, batch=50) > 0:
        pass
    db.query("SELECT COUNT(*) AS n FROM polls")[0]["n"]
    stamps = [
        row["received_at"]
        for row in db.query(
            "SELECT ended_at AS received_at FROM polls WHERE target='h' AND outcome='ok' "
            "ORDER BY ended_at DESC"
        )
    ]
    assert len(stamps) < remaining
    # Beyond the newest two, fixed minute buckets are unique.
    buckets = [int(stamp // 60) for stamp in stamps[2:]]
    assert len(buckets) == len(set(buckets))


def test_downsample_history_validates_arguments(tmp_path):
    db = Database(tmp_path / "x.db")
    with pytest.raises(ValueError):
        db.downsample_history("h", float("nan"))
    with pytest.raises(ValueError):
        db.downsample_history("h", 60, keep=0)
    with pytest.raises(ValueError):
        db.downsample_history("bad\ntarget", 60)
