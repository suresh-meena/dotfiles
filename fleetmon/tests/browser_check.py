"""Manual browser regression check; requires Playwright and its Chromium build.

Run from the fleetmon directory with PYTHONPATH=src python tests/browser_check.py.
All HTTP requests are served by a local TestClient with synthetic fleet data.
No fleet host is contacted and no listener is opened.
"""

import argparse
import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from test_service import config, gpu_document

from fleetmon.discovery import Inventory, Protocol, Target
from fleetmon.service import HubRuntime

parser = argparse.ArgumentParser(
    description="Check the dashboard using synthetic local data."
)
parser.add_argument("--output-dir", type=Path)
args = parser.parse_args()
root = args.output_dir or Path(tempfile.mkdtemp(prefix="fleetmon-browser-"))
root.mkdir(parents=True, exist_ok=True)
stage = "checked"
state = Path(tempfile.mkdtemp(prefix="browser-state-", dir=root))
inventory = Inventory(
    [
        Target(name, True, "compute", "direct")
        for name in ("gpu-01", "gpu-02", "workstation")
    ],
    {"direct": Protocol("direct", "direct")},
)
runtime = HubRuntime(
    config(state, polling_enabled=False), discover_fn=lambda *_: inventory
)
runtime.refresh_inventory()
now = time.time()
for index in range(25):
    doc = gpu_document(idle_gpus=2, busy_gpus=2)
    doc["memory"] = {"total_bytes": 128 * 2**30, "used_bytes": 68 * 2**30}
    doc["disks"] = [
        {"mount": "/", "total_bytes": 1800 * 2**30, "free_bytes": 800 * 2**30},
        {"mount": "/data", "total_bytes": 4000 * 2**30, "free_bytes": 900 * 2**30},
    ]
    doc["network"] = {"addresses": ["100.103.185.14", "10.0.0.21"]}
    doc["processes"] = [
        {
            "pid": 2200 + i,
            "username": "alice" if i % 2 else "bob",
            "name": "python",
            "executable": "python3.12",
            "cpu_cores": i / 4,
            "rss_bytes": (i + 1) * 2**30,
            "create_time": now - 3600,
            "gpu_allocations": [],
        }
        for i in range(18)
    ]
    for gpu in doc["gpus"]:
        gpu["model"] = "NVIDIA A100-SXM4-80GB"
        gpu["vram_total_bytes"] = 80 * 2**30
        gpu["vram_used_bytes"] = (0 if gpu["index"] < 2 else 48) * 2**30
    runtime.db.snapshot(f"gpu-{index}", "gpu-01", doc, now - (24 - index) * 60)
runtime.db.upsert_host("gpu-02", "compute", "direct", "unreachable")
doc["gpus"] = []
runtime.db.snapshot("workstation", "workstation", doc, now)

with TestClient(runtime.create_app()) as client, sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(request):
        path = request.request.url.split("fleetmon.test", 1)[1]
        response = client.get(path)
        request.fulfill(
            status=response.status_code,
            body=response.content,
            headers=dict(response.headers),
        )

    page.route("http://fleetmon.test/**", route)
    results = []
    for width in (1440, 768, 390, 320):
        page.set_viewport_size({"width": width, "height": 1000})
        for path in ("/overview", "/host/gpu-01", "/idle-gpus", "/jobs", "/hub-status"):
            page.goto("http://fleetmon.test" + path)
            page.wait_for_function(
                "!document.getElementById('status').textContent.includes('Loading')"
            )
            page.wait_for_timeout(150)
            results.append(
                {
                    "width": width,
                    "page": path,
                    **page.evaluate("""() => ({
                pageWidth: document.documentElement.scrollWidth,
                contentWidth: document.querySelector('#content').clientWidth,
                contentScroll: document.querySelector('#content').scrollWidth,
                mainTop: document.querySelector('main').getBoundingClientRect().top,
                headerHeight: document.querySelector('header').getBoundingClientRect().height
            })"""),
                }
            )
            if path == "/host/gpu-01" and width in (1440, 390):
                page.screenshot(
                    path=str(root / f"{stage}-host-{width}.png"), full_page=True
                )
            assert results[-1]["pageWidth"] <= width, results[-1]
            assert results[-1]["contentScroll"] <= results[-1]["contentWidth"] + 1, (
                results[-1]
            )
    page.set_viewport_size({"width": 1440, "height": 800})
    page.goto("http://fleetmon.test/host/gpu-01")
    search = page.get_by_role(
        "searchbox", name="Search workloads by user, name, or pid"
    )
    search.fill("alice")
    search.evaluate("el => el.setSelectionRange(1, 3)")
    page.wait_for_timeout(4500)
    focus = search.evaluate(
        "el => ({focused: document.activeElement === el, start: el.selectionStart, end: el.selectionEnd})"
    )
    print(
        json.dumps(
            {"layouts": results, "searchAfterRefresh": focus, "errors": errors},
            indent=2,
        )
    )
    assert focus == {"focused": True, "start": 1, "end": 3}, focus
    assert not errors
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto("http://fleetmon.test/host/gpu-01")
    page.get_by_role("searchbox").wait_for()
    scroller = page.locator(".table-scroll").first
    scroller.evaluate("el => el.scrollLeft = 150")
    page.wait_for_timeout(2400)
    assert scroller.evaluate("el => el.scrollLeft") == 150
    menu = page.get_by_role("button", name="Toggle navigation")
    assert menu.get_attribute("aria-expanded") == "false"
    menu.click()
    assert menu.get_attribute("aria-expanded") == "true"
    assert page.locator("nav").is_visible()
    assert (
        page.evaluate(
            "document.querySelector('main').getBoundingClientRect().top - document.querySelector('nav').getBoundingClientRect().bottom"
        )
        < 2
    )
    assert not errors
    browser.close()
runtime.close()
