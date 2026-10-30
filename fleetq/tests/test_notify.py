"""Transactional notification delivery, digest, redaction, and the persisted hourly cap."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import fleetq.notify as notify
from fleetq.db.store import Store
from fleetq.notify import MAX_PER_HOUR, Outbox


def _store(tmp_path):
    store = Store(tmp_path / "fleetq.db")
    store.open()
    return store


def test_deduplicated_event_redacts_untrusted_remote_text(tmp_path):
    store = _store(tmp_path)
    try:
        outbox = Outbox("https://example.invalid/notify")
        for _ in range(2):
            store.run_sync(lambda c: outbox.enqueue(c, "launch_unknown", {
                "incident": "same", "job": 1, "reason": "secret command --password=abc",
                "command": "secret command --password=abc"}))
        rows = store.run_sync(lambda c: c.execute("SELECT payload_json FROM outbox").fetchall())
        assert len(rows) == 1
        assert json.loads(rows[0][0]) == {"incident": "same", "job": 1, "kind": "launch_unknown"}
    finally:
        store.close()


def test_digest_sends_one_safe_message_and_persists_delivery(tmp_path):
    store = _store(tmp_path)
    try:
        outbox = Outbox("https://example.invalid/notify")
        seen = []
        outbox._post = lambda kind, payload: (seen.append((kind, payload)) or True)
        for job in range(6):
            store.run_sync(lambda c, job=job: outbox.enqueue(c, "job_finished", {"job": job}))
        assert asyncio.run(outbox.flush(store)) == 1
        assert seen == [("digest", {"kind": "digest", "count": 6,
                                    "events": {"job_finished": 6}})]
        assert store.run_sync(lambda c: c.execute(
            "SELECT COUNT(*) FROM outbox WHERE state='SENT'").fetchone()[0]) == 6
    finally:
        store.close()


def test_failed_delivery_retries_without_dropping_incident(tmp_path):
    store = _store(tmp_path)
    try:
        outbox = Outbox("https://example.invalid/notify")
        outbox._post = lambda _kind, _payload: False
        store.run_sync(lambda c: outbox.enqueue(c, "blocked", {"job": 7}))
        assert asyncio.run(outbox.flush(store)) == 0
        state, attempts = store.run_sync(lambda c: tuple(c.execute(
            "SELECT state,attempts FROM outbox").fetchone()))
        assert (state, attempts) == ("PENDING", 1)
    finally:
        store.close()


# A sustained local outbox load must respect its persisted hourly cap.
def test_notification_backlog_drains_at_the_hourly_cap_across_restart(tmp_path, monkeypatch):
    store = _store(tmp_path)
    try:
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        clock = [start]
        monkeypatch.setattr(notify, "utcnow", lambda: clock[0].strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        first = Outbox("https://example.invalid/notify")
        for job in range(300):
            store.run_sync(lambda conn, job=job: first.enqueue(conn, "job_finished", {"job": job}))

        posts = []
        first._post = lambda kind, payload: posts.append((kind, payload)) or True
        assert asyncio.run(first.flush(store)) == 1
        assert asyncio.run(first.flush(store)) == 0
        assert store.run_sync(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE state='SENT'").fetchone()[0]) == MAX_PER_HOUR

        # A restarted worker sees the same persisted cap and pending backlog.
        restarted = Outbox("https://example.invalid/notify")
        restarted._post = first._post
        assert asyncio.run(restarted.flush(store)) == 0
        for batch in range(1, 10):
            clock[0] = start + dt.timedelta(hours=batch, seconds=batch)
            assert asyncio.run(restarted.flush(store)) == 1
            assert asyncio.run(restarted.flush(store)) == 0
            assert store.run_sync(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE state='SENT'").fetchone()[0]) == (batch + 1) * MAX_PER_HOUR

        assert len(posts) == 10
        assert all(kind == "digest" and payload["count"] == MAX_PER_HOUR for kind, payload in posts)
        assert store.run_sync(lambda conn: conn.execute(
            "SELECT COUNT(DISTINCT dedupe_key) FROM outbox WHERE state='SENT'").fetchone()[0]) == 300
    finally:
        store.close()
