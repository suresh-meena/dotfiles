"""Consumer of fleetmon's read-only capacity feed (§7).

fleetqd polls ``GET <fleetmon>/api/feed/v1/capacity`` every 10 seconds on
loopback. A feed that fails, is slow, or speaks an unknown schema version stops
refreshing the history, and shared placement then fails closed within
``feed_stale_s``. Exclusive nodes are unaffected; their launch gate is enough.
No feed request ever triggers a remote call in either direction (invariant 8).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from ..engine.idle import IdleHistory, ingest_capacity_feed
from .usage import UsageCache

log = logging.getLogger("fleetq.feed")
POLL_S = 10.0
TIMEOUT_S = 3.0
MAX_BYTES = 4 * 1024 * 1024


def fetch(url: str, token: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("capacity feed exceeds the size cap")
    return json.loads(raw)


def usage_url(capacity_url: str) -> str:
    parts = urlsplit(capacity_url)
    suffix = "/api/feed/v1/capacity"
    if not parts.path.endswith(suffix):
        raise ValueError("fleetmon_feed_url must end in /api/feed/v1/capacity for usage")
    return urlunsplit(parts._replace(path=parts.path[:-len(suffix)] + "/api/feed/v1/usage"))


async def poll_usage_forever(cache: UsageCache, url: str, token: str | None = None) -> None:
    while True:
        try:
            doc = await asyncio.to_thread(fetch, usage_url(url), token)
            cache.update(doc)
        except Exception:  # keep the last measurement; report the source outage
            cache.failed = True
            log.warning("usage feed unavailable")
        await asyncio.sleep(POLL_S)


async def poll_forever(history: IdleHistory, url: str, token: str | None = None) -> None:
    while True:
        try:
            doc = await asyncio.to_thread(fetch, url, token)
            ingest_capacity_feed(history, doc)
        except Exception as exc:  # stale feed = shared placement off, by construction
            log.warning("capacity feed unavailable: %s", exc)
        await asyncio.sleep(POLL_S)
