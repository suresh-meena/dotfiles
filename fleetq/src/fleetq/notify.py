"""Notifications through a persistent, transactional outbox (§7).

Events are enqueued in the same transaction as the state change that caused
them, deduplicated by incident key, and delivered with backoff and a global
rate cap. Payloads carry no secrets and no full command lines.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import sqlite3
import urllib.request
from typing import Any

from .util import parse_utc, utcnow

log = logging.getLogger("fleetq.notify")
BACKOFF_S = (60, 300, 900)
MAX_PER_HOUR = 30
DIGEST_THRESHOLD = 5
TITLES = {
    "job_finished": "fleetq job finished",
    "launch_unknown": "fleetq: launch outcome unknown",
    "lost_contact": "fleetq: lost contact with a running job",
    "fence_conflict": "fleetq: another controller fenced a target",
    "orphans_discovered": "fleetq: unknown remote attempts found",
    "submission_unknown": "fleetq: sbatch outcome unknown",
    "blocked": "fleetq: job blocked",
    "foreign_process": "fleetq: foreign process on an allocated GPU",
    "gpu_escape": "fleetq: GPU process outside its reservation",
    "gpu_attribution_unknown": "fleetq: GPU process attribution incomplete",
    "digest": "fleetq: incident digest",
}


class Outbox:
    def __init__(self, url: str | None) -> None:
        self.url = url

    def enqueue(self, conn: sqlite3.Connection, kind: str, detail: dict[str, Any]) -> None:
        """Called inside the state-change transaction."""
        key = (f"{kind}:{detail.get('incident')}" if detail.get("incident") else
               f"{kind}:{detail.get('job') or detail.get('target') or ''}:{detail.get('attempt') or ''}")
        # The public notification transport is intentionally a short pointer
        # to the authenticated control plane, not a copy of remote error text,
        # paths, command lines, or other potentially secret details.
        safe = {name: value for name, value in detail.items()
                if name in {"job", "target", "attempt", "gpu", "pid", "outcome",
                            "success", "artifacts", "incident", "seen"}
                and isinstance(value, (str, int, bool, type(None)))
                and (not isinstance(value, str) or len(value) <= 160)}
        conn.execute(
            "INSERT INTO outbox (kind, dedupe_key, payload_json, next_at, created_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(dedupe_key) DO NOTHING",
            (kind, key, json.dumps({"kind": kind, **safe}, sort_keys=True), utcnow(), utcnow()),
        )

    async def run(self, store) -> None:
        while True:
            try:
                await self.flush(store)
            except Exception:
                log.exception("outbox flush failed")
            await asyncio.sleep(10)

    async def flush(self, store) -> int:
        if not self.url:
            return 0
        now = utcnow()
        hour_ago = (parse_utc(now) - _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        def due(conn):
            sent = conn.execute("SELECT COUNT(*) FROM outbox WHERE state='SENT' AND next_at >= ?", (hour_ago,)).fetchone()[0]
            budget = max(0, MAX_PER_HOUR - sent)
            return [dict(r) for r in conn.execute(
                "SELECT * FROM outbox WHERE state='PENDING' AND next_at <= ? ORDER BY id LIMIT ?", (now, budget))]
        rows = await store.run(due)
        groups = []
        if len(rows) >= DIGEST_THRESHOLD:
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["kind"]] = counts.get(row["kind"], 0) + 1
            groups.append((rows, "digest", {"kind": "digest", "count": len(rows), "events": counts}))
        else:
            groups.extend(([row], row["kind"], json.loads(row["payload_json"])) for row in rows)
        sent = 0
        for members, kind, payload in groups:
            ok = await asyncio.to_thread(self._post, kind, payload)

            def record(conn):
                for row in members:
                    if ok:
                        conn.execute("UPDATE outbox SET state='SENT', next_at=? WHERE id=?", (utcnow(), row["id"]))
                    else:
                        wait = BACKOFF_S[min(row["attempts"], len(BACKOFF_S) - 1)]
                        later = (parse_utc(utcnow()) + _dt.timedelta(seconds=wait)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                        conn.execute("UPDATE outbox SET attempts=attempts+1, next_at=? WHERE id=?", (later, row["id"]))
            await store.run(record)
            sent += int(ok)
        return sent

    def _post(self, kind: str, payload: dict[str, Any]) -> bool:
        body = json.dumps(payload).encode()[:2048]
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Title": TITLES.get(kind, f"fleetq: {kind}"),
                                              "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False
