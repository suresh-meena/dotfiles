"""Strict parsing and cached, fail-closed managed Slurm queue snapshots."""

from __future__ import annotations

import json

import pytest

from fleetq.db.store import Store
from fleetq.slurm import managed_snapshot as snapshot


@pytest.fixture()
def db(tmp_path):
    store = Store(tmp_path / "snapshot.db")
    store.open()
    config = {"site": {"managed_queue_snapshot": True, "managed_queue_interval_s": 45,
                       "managed_queue_stale_after_s": 90}}
    store.run_sync(lambda c: c.execute(
        "INSERT INTO nodes(id,backend,enabled,config_json,updated_at) VALUES('s','slurm',1,?,'2026-09-28T00:00:00Z')",
        (json.dumps(config),)))
    yield store
    store.close()


def test_json_parser_accepts_rows_and_normalizes_identifier():
    assert snapshot.parse_squeue_json('{"jobs":[{"job_id":123,"state":{"current":"RUNNING"}}]}') == [
        {"job_id": "123", "state": {"current": "RUNNING"}}]


@pytest.mark.parametrize("raw", [
    '{"jobs":[{"job_id":"ok"},{"state":"RUNNING"}]}',
    '{"jobs":[{"job_id":"bad\nvalue"}]}',
    '{"jobs":null}',
    '{"jobs":[{"job_id":"x","bad":NaN}]}',
])
def test_parser_rejects_incomplete_or_nonstandard_documents(raw):
    with pytest.raises((ValueError, TypeError)):
        snapshot.parse_squeue_json(raw)


def test_failed_refresh_keeps_last_complete_payload_but_marks_it_incomplete(db):
    good_at = "2026-09-28T00:00:00.000000Z"
    jobs = [{"job_id": "17", "job_state": {"current": "RUNNING"}}]
    db.run_sync(lambda c: snapshot.record_snapshot_success(
        c, "s", jobs=jobs, observed_at=good_at, output_bytes=71))
    db.run_sync(lambda c: snapshot.record_snapshot_failure(
        c, "s", attempted_at="2026-09-28T00:00:30.000000Z", error="timeout"))
    doc = db.run_sync(lambda c: snapshot.managed_slurm_document(c, now="2026-09-28T00:00:40.000000Z"))
    site = doc["sites"][0]
    assert site["complete"] is False and site["stale"] is False
    assert site["error"] == "timeout"
    assert site["last_success_at"] == good_at and site["jobs"] == jobs
    assert site["row_count"] == 1 and site["output_bytes"] == 71


def test_settings_are_opt_in_and_cadence_is_bounded():
    assert snapshot.site_snapshot_settings({})[0] is False
    enabled, interval, stale = snapshot.site_snapshot_settings({"site": {
        "managed_queue_snapshot": True, "managed_queue_interval_s": 1,
        "managed_queue_stale_after_s": 999999}})
    assert enabled and interval == snapshot.MIN_INTERVAL_S
    assert stale == snapshot.MAX_INTERVAL_S * 24


def test_invalid_snapshot_config_is_failed_closed(db):
    db.run_sync(lambda c: snapshot.record_snapshot_success(
        c, "s", jobs=[], observed_at="2026-09-28T00:00:00.000000Z", output_bytes=2))
    db.run_sync(lambda c: c.execute("UPDATE nodes SET config_json=? WHERE id='s'", (
        json.dumps({"site": {"managed_queue_snapshot": True, "managed_queue_interval_s": False}}),)))
    doc = db.run_sync(lambda c: snapshot.managed_slurm_document(c, now="2026-09-28T00:00:00.000000Z"))
    assert doc["sites"][0]["complete"] is False
    assert doc["sites"][0]["error"] == "invalid_config"
