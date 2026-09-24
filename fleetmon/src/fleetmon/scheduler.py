"""Read-only client for fleetqd, the fleet queue, behind the Queue page.

Fleetmon never changes the queue: it only reads fleetqd's API with a token
that should carry the ``read``/``read_all`` scopes and nothing else. Every read
is bounded (short timeout, size cap, no proxies, no redirects) and cached for
a moment, so a dashboard left open costs fleetqd one request per couple of
seconds, and a scheduler that is down or slow only greys out the page.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 2.0
CACHE_SECONDS = 2.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CACHE_ENTRIES = 64


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


class SchedulerClient:
    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout: float = TIMEOUT_SECONDS,
        ttl: float = CACHE_SECONDS,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def get(self, path: str) -> dict[str, Any]:
        """One API document, or ``{"available": False, ...}``; never raises."""

        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(path)
            if hit is not None and now - hit[0] <= self.ttl:
                return hit[1]
        value = self._fetch(path)
        with self._lock:
            if len(self._cache) >= MAX_CACHE_ENTRIES:
                self._cache.clear()
            self._cache[path] = (time.monotonic(), value)
        return value

    def _fetch(self, path: str) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url + path, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # A refusal (wrong token, 404, a redirect we won't follow) is as
            # unusable as silence: the page must say why, never show "no jobs".
            return {"available": False, "error": _error_code(exc), "status": exc.code}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"available": False, "error": f"scheduler unreachable: {type(exc).__name__}"}
        if len(body) > MAX_RESPONSE_BYTES:
            return {"available": False, "error": "scheduler response too large"}
        try:
            document = json.loads(body)
        except ValueError:
            return {"available": False, "error": "scheduler answered with invalid JSON"}
        if not isinstance(document, dict):
            return {"available": False, "error": "scheduler answered with a non-object"}
        return {"available": True, **document}


def _error_code(exc: urllib.error.HTTPError) -> str:
    try:
        document = json.loads(exc.read(64 * 1024))
        return str(document.get("error", {}).get("code") or f"HTTP {exc.code}")
    except Exception:
        return f"HTTP {exc.code}"
