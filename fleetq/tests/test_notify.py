"""Transactional notification delivery, digest, and conservative redaction."""

from __future__ import annotations

import asyncio
import json

from fleetq.db.store import Store
from fleetq.notify import Outbox


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
