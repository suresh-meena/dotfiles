"""Small, read-only dashboard API.

The database argument is deliberately a query interface rather than a concrete
database class.  This keeps the HTTP surface usable while the hub database
implementation evolves: methods may return a list or a mapping and absent
methods produce an empty, clearly labelled response.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import html
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from itertools import islice
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..config import _is_loopback_bind, _is_vpn_bind
from ..database import MAX_CHART_POINTS, MAX_SPARK_HOSTS, MAX_SPARK_POINTS

MAX_ROWS = 500
MAX_CHART_SERIES = 40
MAX_RANGE_SECONDS = 30 * 24 * 3600
MAX_RANGE_HOURS = 30 * 24
MAX_AUTH_HEADER_BYTES = 4096
MAX_BASIC_CREDENTIAL_BYTES = 512
BASIC_USERNAME = "fleetmon"
MAX_QUERY_WORKERS = 2
MAX_QUERY_PENDING = 32
ROOT = Path(__file__).parent


def _invoke(source: Any, name: str, **kwargs: Any) -> Any:
    """Call one query method, without leaking query errors."""
    if source is None:
        return []
    try:
        return getattr(source, name)(**kwargs)
    except Exception:
        return []


async def _invoke_with_executor(
    executor: ThreadPoolExecutor,
    source: Any,
    name: str,
    capacity: threading.BoundedSemaphore | None = None,
    **kwargs: Any,
) -> Any:
    """Run one bounded query in the app's shared executor."""

    if capacity is not None and not capacity.acquire(blocking=False):
        raise HTTPException(503, "query capacity exhausted")
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(
            executor, partial(_invoke, source, name, **kwargs)
        )
    except BaseException:
        if capacity is not None:
            capacity.release()
        raise
    if capacity is not None:
        future.add_done_callback(lambda _future: capacity.release())
    return await future


def _bounded(value: Any, limit: int, offset: int = 0) -> list[Any]:
    if isinstance(value, dict):
        value = value.get("items", value.get("rows", []))
    if isinstance(value, (str, bytes, bytearray)) or value is None:
        return []
    try:
        if isinstance(value, (list, tuple)):
            return list(value[offset : offset + limit])
        return list(islice(value, offset, offset + limit))
    except Exception:
        return []


def _range(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    if not start and not end:
        return None, None
    try:
        s = datetime.fromisoformat((start or end or "").replace("Z", "+00:00"))
        e = datetime.fromisoformat((end or start or "").replace("Z", "+00:00"))
        if s.tzinfo is None or e.tzinfo is None:
            raise ValueError
        if e < s or (e - s).total_seconds() > MAX_RANGE_SECONDS:
            raise ValueError
    except (AttributeError, ValueError, TypeError):
        raise HTTPException(400, "invalid time range") from None
    return start, end


def _page(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1 or limit > MAX_ROWS or offset < 0 or offset > 1_000_000:
        raise HTTPException(400, "invalid pagination")
    return limit, offset


def _page_doc(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "count": len(items),
        "bounded": True,
    }


def _valid_target(target: Any) -> bool:
    return (
        isinstance(target, str)
        and bool(target)
        and len(target) <= 256
        and target == target.strip()
        and not target.startswith("-")
        and not any(
            char.isspace() or ord(char) < 32 or ord(char) == 0x7F for char in target
        )
    )


def _finite_pair(pair: Any) -> list[float] | None:
    if (
        not isinstance(pair, (list, tuple))
        or len(pair) != 2
        or isinstance(pair[0], bool)
        or isinstance(pair[1], bool)
    ):
        return None
    time_value, data_value = pair
    if not isinstance(time_value, (int, float)) or not isinstance(
        data_value, (int, float)
    ):
        return None
    if not (math.isfinite(time_value) and math.isfinite(data_value)):
        return None
    return [time_value, data_value]


def _bounded_charts(value: Any, target: str, points: int) -> dict[str, Any]:
    """Shape a provider chart group into bounded, finite series.

    Even a custom query provider that forgot a SQL LIMIT cannot exceed the
    chart bounds here: at most ``MAX_CHART_SERIES`` series, at most ``points``
    finite ``[time, value]`` pairs per series, one request per chart group.
    """

    raw = value.get("series") if isinstance(value, dict) else value
    series: list[dict[str, Any]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw[:MAX_CHART_SERIES]:
            if not isinstance(item, dict):
                continue
            label = item.get("label")
            chart = item.get("chart")
            unit = item.get("unit")
            sampled = item.get("points")
            if not isinstance(label, str) or not isinstance(sampled, (list, tuple)):
                continue
            chart_points = [
                finite
                for finite in (_finite_pair(pair) for pair in sampled[:points])
                if finite is not None
            ]
            series.append(
                {
                    "chart": chart if isinstance(chart, str) else "unknown",
                    "label": label[:256],
                    "unit": unit if isinstance(unit, str) else "unknown",
                    "points": chart_points,
                }
            )
    return {"target": target, "bounded": True, "points": points, "series": series}


def _bounded_sparklines(value: Any) -> dict[str, Any]:
    """Shape a provider sparkline group into bounded per-host CPU series.

    The overview sparkline response carries one capped series per host in a
    single document; the same fail-closed shaping applies even to a custom
    provider that forgot its own limits.
    """

    raw = value.get("series") if isinstance(value, dict) else value
    series: list[dict[str, Any]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw[:MAX_SPARK_HOSTS]:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            sampled = item.get("points")
            if not isinstance(target, str) or not isinstance(sampled, (list, tuple)):
                continue
            spark_points = [
                finite
                for finite in (
                    _finite_pair(pair) for pair in sampled[:MAX_SPARK_POINTS]
                )
                if finite is not None
            ]
            series.append({"target": target[:256], "points": spark_points})
    return {"bounded": True, "points": MAX_SPARK_POINTS, "series": series}


def _valid_credentials(supplied: str, token: str | None) -> bool:
    """Accept Bearer API tokens and browser-friendly Basic credentials."""

    if token is None or len(supplied) > MAX_AUTH_HEADER_BYTES:
        return False
    scheme, separator, value = supplied.partition(" ")
    if not separator or not value:
        return False
    if scheme.lower() == "bearer":
        return bool(
            hmac.compare_digest(scheme.lower(), "bearer")
            & hmac.compare_digest(value, token)
        )
    if scheme.lower() != "basic" or len(value) > MAX_BASIC_CREDENTIAL_BYTES:
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) > MAX_BASIC_CREDENTIAL_BYTES:
            return False
        credentials = decoded.decode("utf-8")
        username, password = credentials.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    # Evaluate both comparisons so a wrong username does not skip the token
    # comparison. The fixed username is intentionally not user-configurable.
    return bool(
        hmac.compare_digest(username, BASIC_USERNAME)
        & hmac.compare_digest(password, token)
    )


def _html_page(page: str, title: str, target: str = "") -> HTMLResponse:
    template = (ROOT / "templates" / "page.html").read_text(encoding="utf-8")
    return HTMLResponse(
        template.replace("{{TITLE}}", html.escape(title))
        .replace("{{PAGE}}", html.escape(page))
        .replace("{{TARGET}}", html.escape(target))
    )


def create_app(
    query: Any = None,
    *,
    bind: str = "127.0.0.1",
    authenticated: bool = False,
    auth_token: str | None = None,
) -> FastAPI:
    """Build the app. Non-VPN non-loopback binds require authentication."""

    if not _is_loopback_bind(bind) and not _is_vpn_bind(bind) and not authenticated:
        raise ValueError("non-loopback web binding requires authentication")
    if authenticated and (
        not isinstance(auth_token, str)
        or not auth_token.strip()
        or len(auth_token) < 32
        or len(auth_token.encode("utf-8")) > MAX_AUTH_HEADER_BYTES
        or any(ord(char) < 32 or ord(char) == 0x7F for char in auth_token)
    ):
        raise ValueError(
            "authenticated mode requires a token of at least 32 characters"
        )
    app = FastAPI(title="Fleetmon", docs_url=None, redoc_url=None)
    app.state.query = query
    app.state.query_executor = ThreadPoolExecutor(
        max_workers=MAX_QUERY_WORKERS, thread_name_prefix="fleetmon-web"
    )
    # ThreadPoolExecutor's work queue is unbounded. Keep the number of
    # running plus queued provider calls finite, and fail fast under a burst
    # instead of allowing requests to accumulate indefinitely.
    app.state.query_capacity = threading.BoundedSemaphore(
        MAX_QUERY_WORKERS + MAX_QUERY_PENDING
    )

    @app.on_event("shutdown")
    async def close_query_executor() -> None:
        app.state.query_executor.shutdown(wait=True, cancel_futures=True)

    app.state.ready = True
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.middleware("http")
    async def require_token(request, call_next):
        if authenticated and request.url.path not in {"/healthz", "/readyz"}:
            supplied = request.headers.get("authorization", "")
            if not _valid_credentials(supplied, auth_token):
                from fastapi.responses import JSONResponse

                response = JSONResponse(
                    {"detail": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Fleetmon"'},
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        return response

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(503, "not ready")
        return {"status": "ready"}

    @app.get("/api/overview")
    async def overview(
        limit: int = Query(100),
        offset: int = Query(0),
        start: str | None = None,
        end: str | None = None,
    ):
        limit, offset = _page(limit, offset)
        start, end = _range(start, end)
        rows = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "overview",
            capacity=app.state.query_capacity,
            start=start,
            end=end,
        )
        return _page_doc(_bounded(rows, limit, offset), limit, offset)

    @app.get("/api/overview/sparklines")
    async def overview_sparklines():
        value = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "sparklines",
            capacity=app.state.query_capacity,
        )
        return _bounded_sparklines(value)

    @app.get("/api/idle-gpus")
    async def idle_gpus(
        limit: int = Query(100),
        offset: int = Query(0),
    ):
        limit, offset = _page(limit, offset)
        value = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "idle_gpus",
            capacity=app.state.query_capacity,
        )
        raw_items = value.get("items", []) if isinstance(value, dict) else value
        raw_summary = value.get("summary") if isinstance(value, dict) else None
        summary = {"idle": 0, "busy": 0, "unknown": 0}
        if isinstance(raw_summary, dict):
            for key in summary:
                count = raw_summary.get(key)
                if (
                    isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                ):
                    summary[key] = count
        document = _page_doc(_bounded(raw_items, limit, offset), limit, offset)
        document["summary"] = summary
        return document

    @app.get("/api/hosts")
    async def hosts(
        limit: int = Query(100),
        offset: int = Query(0),
        start: str | None = None,
        end: str | None = None,
    ):
        limit, offset = _page(limit, offset)
        start, end = _range(start, end)
        rows = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "hosts",
            capacity=app.state.query_capacity,
            start=start,
            end=end,
        )
        return _page_doc(_bounded(rows, limit, offset), limit, offset)

    @app.get("/api/hosts/{target}")
    async def host(
        target: str,
        limit: int = Query(100),
        offset: int = Query(0),
        start: str | None = None,
        end: str | None = None,
    ):
        if not _valid_target(target):
            raise HTTPException(404, "host not found")
        limit, offset = _page(limit, offset)
        start, end = _range(start, end)
        result = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "host",
            capacity=app.state.query_capacity,
            target=target,
            start=start,
            end=end,
            # Fetch enough for the requested page while retaining the chart
            # ceiling. The response is sliced again below for providers that
            # do not implement offset pagination.
            limit=min(MAX_CHART_POINTS, limit + offset),
        )
        if isinstance(result, dict):
            result = dict(result)
            result["items"] = _bounded(result.get("items", []), limit, offset)
            # Current processes and other collection fields are also remote
            # data. Keep every returned collection bounded, even for a custom
            # query provider that forgot to apply a SQL LIMIT.
            for key in ("processes", "gpus", "users", "jobs"):
                if key in result:
                    result[key] = _bounded(result[key], MAX_ROWS)
            return result
        return _page_doc(_bounded(result, limit, offset), limit, offset)

    @app.get("/api/hosts/{target}/charts")
    async def host_charts(
        target: str,
        points: int = Query(MAX_CHART_POINTS),
        hours: int | None = Query(None),
        start: str | None = None,
        end: str | None = None,
    ):
        if not _valid_target(target):
            raise HTTPException(404, "host not found")
        if points < 1 or points > MAX_CHART_POINTS:
            raise HTTPException(400, "invalid chart point count")
        if hours is not None and (hours < 1 or hours > MAX_RANGE_HOURS):
            raise HTTPException(400, "invalid hours range")
        if hours is not None and start is None and end is None:
            # A compact range selector: derive the window from "now" once,
            # still capped by the same 30-day maximum as explicit ranges.
            end = datetime.now(timezone.utc).isoformat()
            start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        start, end = _range(start, end)
        value = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "host_charts",
            capacity=app.state.query_capacity,
            target=target,
            start=start,
            end=end,
            points=points,
        )
        return _bounded_charts(value, target, points)

    @app.get("/api/jobs")
    async def jobs(
        limit: int = Query(100),
        offset: int = Query(0),
        start: str | None = None,
        end: str | None = None,
    ):
        limit, offset = _page(limit, offset)
        start, end = _range(start, end)
        rows = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "jobs",
            capacity=app.state.query_capacity,
            start=start,
            end=end,
        )
        return _page_doc(_bounded(rows, limit, offset), limit, offset)

    @app.get("/api/hub-status")
    async def hub_status():
        value = await _invoke_with_executor(
            app.state.query_executor,
            app.state.query,
            "hub_status",
            capacity=app.state.query_capacity,
        )
        return (
            value
            if isinstance(value, dict)
            else {"status": "unknown", "details": value if value else {}}
        )

    @app.get("/host/{target}", response_class=HTMLResponse)
    async def host_page(target: str):
        if not _valid_target(target):
            raise HTTPException(404, "host not found")
        return _html_page("host", f"Host: {target}", target)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/{page}", response_class=HTMLResponse)
    async def page(page: str = "overview"):
        if page not in {"overview", "jobs", "hub-status", "idle-gpus"}:
            raise HTTPException(404, "page not found")
        return _html_page(page, page.replace("-", " ").title())

    return app
