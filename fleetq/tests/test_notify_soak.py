"""A sustained local outbox load must respect its persisted hourly cap."""

import asyncio
import datetime as dt

from fleetq.db.store import Store
from fleetq.notify import MAX_PER_HOUR, Outbox


def test_notification_backlog_drains_at_the_hourly_cap_across_restart(tmp_path, monkeypatch):
    import fleetq.notify as notify

    store = Store(tmp_path / "fleetq.db")
    store.open()
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
