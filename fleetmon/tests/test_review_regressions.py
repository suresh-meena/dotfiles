from test_database import sample
from test_notify import gpu_row, observation

from fleetmon import notify
from fleetmon.database import Database


def test_fast_history_preserves_fixed_buckets_across_every_collection(tmp_path):
    db = Database(tmp_path / "history.db")
    for i in range(100):
        db.snapshot(str(i), "host", sample(), i * 2)
        db.downsample_history("host", 60)
    stamps = [
        r["ended_at"] for r in db.query("SELECT ended_at FROM polls ORDER BY ended_at")
    ]
    assert stamps == [58, 118, 178, 196, 198]
    db.close()


def test_idle_requires_distinct_fresh_known_boot_observations():
    newest = observation(gpu_row(), received_at=100)
    duplicate = observation(gpu_row(), received_at=100)
    assert (
        notify.classify_gpu(newest, duplicate, now=101, freshness_seconds=10)[0]
        == "unknown"
    )
    older = observation(gpu_row(), received_at=90)
    assert (
        notify.classify_gpu(newest, older, now=101, freshness_seconds=10)[0]
        == "unknown"
    )
    older["received_at"] = 98
    assert (
        notify.classify_gpu(newest, older, now=101, freshness_seconds=10)[0] == "idle"
    )
    newest["boot_id"] = older["boot_id"] = "unknown"
    assert (
        notify.classify_gpu(newest, older, now=101, freshness_seconds=10)[0]
        == "unknown"
    )


def test_nan_gpu_reading_is_never_idle():
    newest = observation(gpu_row(utilization=float("nan")), received_at=100)
    older = observation(gpu_row(), received_at=98)
    assert notify.classify_gpu(newest, older)[0] == "unknown"


def test_failed_poll_prevents_reusing_an_idle_pair(tmp_path):
    db = Database(tmp_path / "history.db")
    doc = sample()
    doc["gpus"] = [
        {
            "uuid": "u",
            "index": 0,
            "supported": True,
            "utilization_fraction": 0,
            "compute_process_count": 0,
        }
    ]
    db.snapshot("one", "host", doc, 98)
    db.snapshot("two", "host", doc, 100)
    db.record_error("failed", "host", "transport", 101)
    assert (
        notify.classify_gpu_slots(
            db.gpu_recent("host")["u"], now=102, freshness_seconds=10
        )[0]
        == "unknown"
    )
    db.close()
