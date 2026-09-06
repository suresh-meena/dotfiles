from base64 import b64encode

import httpx
import pytest

fastapi = pytest.importorskip("fastapi")
create_app = pytest.importorskip("fleetmon.web.app").create_app


class Queries:
    def overview(self, **kwargs):
        return [{"target": "alpha", "state": "live"}]

    def host(self, **kwargs):
        return {
            "target": kwargs["target"],
            "items": (item for item in range(10_000)),
            "processes": list(range(10_000)),
        }


def request(app, method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    import asyncio

    return asyncio.run(run())


def test_pages_and_health():
    app = create_app(Queries())
    assert request(app, "GET", "/healthz").json() == {"status": "ok"}
    assert request(app, "GET", "/readyz").status_code == 200
    assert "Fleetmon" in request(app, "GET", "/overview").text
    host = request(app, "GET", "/host/alpha")
    assert host.status_code == 200
    assert 'data-target="alpha"' in host.text


def test_api_is_bounded_and_ranges_validated():
    app = create_app(Queries())
    assert request(app, "GET", "/api/overview?limit=501").status_code == 400
    assert (
        request(
            app,
            "GET",
            "/api/overview?start=2025-01-01T00:00:00Z&end=2025-02-01T00:00:01Z",
        ).status_code
        == 400
    )
    body = request(app, "GET", "/api/overview?limit=1").json()
    assert body["bounded"] is True and body["count"] == 1


def test_non_loopback_requires_authentication():
    with pytest.raises(ValueError):
        create_app(bind="0.0.0.0")


def test_authenticated_mode_enforces_bearer_and_keeps_health_minimal():
    token = "s" * 32
    app = create_app(Queries(), bind="0.0.0.0", authenticated=True, auth_token=token)
    assert request(app, "GET", "/api/overview").status_code == 401
    assert (
        request(
            app, "GET", "/api/overview", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )
    assert request(app, "GET", "/healthz").json() == {"status": "ok"}


def test_authenticated_mode_supports_browser_basic_auth():
    token = "s" * 32
    app = create_app(Queries(), bind="0.0.0.0", authenticated=True, auth_token=token)
    basic = b64encode(f"fleetmon:{token}".encode()).decode()
    page = request(app, "GET", "/overview", headers={"Authorization": f"Basic {basic}"})
    assert page.status_code == 200
    denied = request(app, "GET", "/overview")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == 'Basic realm="Fleetmon"'


def test_host_collections_are_bounded_without_materializing_all_rows():
    app = create_app(Queries())
    response = request(app, "GET", "/api/hosts/alpha?limit=10")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 10
    assert len(body["processes"]) == 500


def test_remote_target_is_contextually_escaped_in_html():
    app = create_app(Queries())
    # Target names cannot contain whitespace or slashes, so the hostile
    # payload must also be whitespace-free to pass admission.
    response = request(app, "GET", "/host/%3Cscript%3Ealert%281%29")
    assert response.status_code == 200
    assert "<script>alert(1)" not in response.text
    assert "&lt;script&gt;alert(1)" in response.text


class ChartQueries:
    def host_charts(self, **kwargs):
        assert kwargs["target"] == "alpha"
        return {
            "series": [
                {
                    "chart": "cpu",
                    "label": "cpu used",
                    "unit": "percent",
                    "points": [[i, i / 100] for i in range(5000)],
                },
                {
                    "chart": "bad",
                    "label": "bogus",
                    "points": [[0, "NaN"], [0, None], [1, 2], ["x", 1], 5, [1, 2, 3]],
                },
            ]
        }


def test_host_charts_are_bounded_finite_and_one_request_per_group():
    app = create_app(ChartQueries())
    body = request(app, "GET", "/api/hosts/alpha/charts").json()
    assert body["bounded"] is True and body["target"] == "alpha"
    assert body["points"] == 2000
    assert len(body["series"]) == 2
    cpu = body["series"][0]
    assert cpu["label"] == "cpu used" and len(cpu["points"]) == 2000
    assert all(len(pair) == 2 for pair in cpu["points"])
    # The malformed series keeps only the one safe [1, 2] pair.
    assert body["series"][1]["points"] == [[1, 2]]


def test_host_charts_validate_points_and_range():
    app = create_app(ChartQueries())
    assert request(app, "GET", "/api/hosts/alpha/charts?points=2001").status_code == 400
    assert request(app, "GET", "/api/hosts/alpha/charts?points=0").status_code == 400
    assert (
        request(
            app,
            "GET",
            "/api/hosts/alpha/charts?start=2025-01-01T00:00:00Z&end=2025-02-01T00:00:01Z",
        ).status_code
        == 400
    )
    assert (
        request(app, "GET", "/api/hosts/alpha/charts?points=10").json()["points"] == 10
    )


class RangeQueries:
    def __init__(self):
        self.kwargs = None

    def host_charts(self, **kwargs):
        self.kwargs = kwargs
        return {"series": []}


def test_host_charts_accept_a_capped_hours_range():
    queries = RangeQueries()
    app = create_app(queries)
    body = request(app, "GET", "/api/hosts/alpha/charts?hours=24").json()
    assert body["bounded"] is True
    assert queries.kwargs["start"] is not None and queries.kwargs["end"] is not None
    assert request(app, "GET", "/api/hosts/alpha/charts?hours=0").status_code == 400
    assert request(app, "GET", "/api/hosts/alpha/charts?hours=721").status_code == 400
    explicit = "2025-01-01T00:00:00Z"
    request(
        app, "GET", f"/api/hosts/alpha/charts?hours=24&start={explicit}&end={explicit}"
    )
    assert queries.kwargs["start"] == explicit and queries.kwargs["end"] == explicit


class SparkQueries:
    def sparklines(self, **kwargs):
        return {
            "series": [
                {"target": "alpha", "points": [[i, i / 100] for i in range(5000)]},
                {"target": "beta", "points": [[0, "NaN"], [1, 2], 5]},
                7,
                {"points": [[1, 2]]},
            ]
        }


def test_overview_sparklines_are_bounded_finite_and_one_request():
    app = create_app(SparkQueries())
    body = request(app, "GET", "/api/overview/sparklines").json()
    assert body["bounded"] is True and body["points"] == 60
    assert len(body["series"]) == 2
    alpha = body["series"][0]
    assert alpha["target"] == "alpha" and len(alpha["points"]) == 60
    assert all(len(pair) == 2 for pair in alpha["points"])
    # The malformed series keeps only the one safe [1, 2] pair.
    assert body["series"][1] == {"target": "beta", "points": [[1, 2]]}


class HostCollections:
    def host(self, **kwargs):
        return {
            "target": kwargs["target"],
            "items": [1, 2, 3],
            "processes": list(range(600)),
            "gpus": list(range(600)),
            "users": list(range(600)),
            "jobs": list(range(600)),
        }


def test_host_gpu_and_user_collections_are_bounded():
    app = create_app(HostCollections())
    body = request(app, "GET", "/api/hosts/alpha?limit=2").json()
    assert len(body["items"]) == 2
    for key in ("processes", "gpus", "users", "jobs"):
        assert len(body[key]) == 500


def test_hub_status_passes_provider_values_through():
    class Hub:
        def hub_status(self):
            return {
                "status": "ready",
                "polling_enabled": True,
                "recent_polls_total": 5,
                "recent_error_rate": 0.2,
                "backup_status": "backup_disabled",
            }

    body = request(app=create_app(Hub()), method="GET", path="/api/hub-status").json()
    assert body["recent_polls_total"] == 5
    assert body["backup_status"] == "backup_disabled"


def test_pages_carry_the_security_headers():
    app = create_app(Queries())
    response = request(app, "GET", "/overview")
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    api = request(app, "GET", "/api/overview")
    assert api.headers["content-security-policy"].startswith("default-src 'self'")


def test_dashboard_script_renders_text_only_and_pauses_when_hidden():
    """Run the real dashboard script against a DOM shim and hostile fixtures."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    harness = Path(__file__).parent / "js_harness.mjs"
    result = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(Path(__file__).parent.parent),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "js harness ok" in result.stdout


class IdleGpuQueries:
    def idle_gpus(self, **kwargs):
        items = []
        for index in range(700):
            items.append(
                {
                    "target": f"host-{index % 3}",
                    "uuid": f"GPU-{index}",
                    "idx": index,
                    "model": "A100",
                    "utilization": 0.0,
                    "vram_total": 1000,
                    "vram_used": 10,
                    "vram_free": 990,
                    "compute_process_count": 0,
                    "received_at": 1000.0,
                    "age_seconds": 0.5,
                    "availability": "idle" if index % 3 == 0 else "busy",
                    "reason": "consecutive_idle_observations",
                }
            )
        return {
            "items": items,
            "summary": {"idle": 234, "busy": 466, "unknown": 0},
        }


def test_idle_gpus_endpoint_is_bounded_and_carries_summary():
    app = create_app(IdleGpuQueries())
    body = request(app, "GET", "/api/idle-gpus?limit=50").json()
    assert body["bounded"] is True
    assert body["count"] == 50 and len(body["items"]) == 50
    assert body["offset"] == 0 and body["limit"] == 50
    # The summary still reflects every known GPU, not only the page.
    assert body["summary"] == {"idle": 234, "busy": 466, "unknown": 0}
    row = body["items"][0]
    for field in (
        "target",
        "uuid",
        "idx",
        "model",
        "utilization",
        "vram_total",
        "vram_used",
        "vram_free",
        "compute_process_count",
        "received_at",
        "age_seconds",
        "availability",
        "reason",
    ):
        assert field in row
    assert request(app, "GET", "/api/idle-gpus?limit=501").status_code == 400
    assert request(app, "GET", "/api/idle-gpus?limit=0").status_code == 400
    assert request(app, "GET", "/api/idle-gpus?offset=-1").status_code == 400


def test_idle_gpus_endpoint_sanitizes_hostile_summaries():
    class Hostile:
        def idle_gpus(self, **kwargs):
            return {
                "items": [{"target": "t", "availability": "idle"}],
                "summary": {"idle": "many", "busy": -5, "unknown": True, "x": 1},
            }

    body = request(
        app=create_app(Hostile()), method="GET", path="/api/idle-gpus"
    ).json()
    assert body["summary"] == {"idle": 0, "busy": 0, "unknown": 0}
    assert body["count"] == 1


def test_idle_gpus_degrades_to_empty_when_provider_lacks_the_method():
    app = create_app(Queries())
    body = request(app, "GET", "/api/idle-gpus").json()
    assert body["items"] == []
    assert body["summary"] == {"idle": 0, "busy": 0, "unknown": 0}


def test_idle_gpus_page_route_renders():
    app = create_app(IdleGpuQueries())
    response = request(app, "GET", "/idle-gpus")
    assert response.status_code == 200
    assert "Fleetmon" in response.text


def test_mesh_vpn_bind_serves_without_token():
    app = create_app(Queries(), bind="100.103.185.102")
    response = request(app, "GET", "/api/overview")
    assert response.status_code == 200
