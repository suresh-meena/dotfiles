// Executes the real fleetmon.js against a minimal DOM shim with hostile
// fixtures. Asserts that every remote value is rendered as text (innerHTML
// throws in this shim), that cards/tables/charts have the expected structure,
// that utilization fractions are never divided by 100, unknown is never zero,
// and that polling pauses while the tab is hidden and refreshes once on return.
// Run directly (node tests/js_harness.mjs) or through tests/test_web.py.
import {readFileSync} from "node:fs";
import assert from "node:assert/strict";
import {fileURLToPath} from "node:url";
import path from "node:path";

const scriptPath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "src",
  "fleetmon",
  "web",
  "static",
  "fleetmon.js",
);
const script = readFileSync(scriptPath, "utf8");

class Node {
  constructor(tag, ns) {
    this.tagName = String(tag).toUpperCase();
    this.nodeType = 1;
    this.childNodes = [];
    this.parentNode = null;
    this._text = "";
    this._attrs = {};
    this.dataset = {};
    this.className = "";
    this.handlers = {};
    this.style = {};
  }
  get rows() {
    return this.childNodes;
  }
  get cells() {
    return this.childNodes;
  }
  get href() {
    return this._href || "";
  }
  set href(v) {
    this._href = String(v);
  }
  set scope(v) {
    this._attrs.scope = v;
  }
  get textContent() {
    // Text assigned before later prepend()/append() calls stays a child text
    // node in a real DOM; keep it in the join so the shim matches that.
    return (
      this._text +
      this.childNodes
        .map((c) => (c.nodeType === 3 ? c._text : c.textContent))
        .join("")
    );
  }
  set textContent(v) {
    this.childNodes = [];
    this._text = String(v);
  }
  append(...nodes) {
    for (const n of nodes) {
      if (typeof n !== "object" || n === null || !n.nodeType) {
        throw new Error(`append(non-node: ${JSON.stringify(n)})`);
      }
      if (n.parentNode) {
        const at = n.parentNode.childNodes.indexOf(n);
        if (at >= 0) n.parentNode.childNodes.splice(at, 1);
      }
      n.parentNode = this;
      this.childNodes.push(n);
    }
  }
  prepend(n) {
    n.parentNode = this;
    this.childNodes.unshift(n);
  }
  replaceChildren() {
    this.childNodes = [];
  }
  set innerHTML(_) {
    throw new Error("innerHTML must never be used with remote values");
  }
  get innerHTML() {
    throw new Error("innerHTML must never be used with remote values");
  }
  addEventListener(type, fn) {
    (this.handlers[type] ||= []).push(fn);
  }
  closest(selector) {
    const want = selector.match(/^([a-z-]+)(\[.+\])?$/);
    if (!want) throw new Error(`selector not shimmed: ${selector}`);
    let node = this;
    while (node) {
      if (node.nodeType === 1 && node.tagName === want[1].toUpperCase()) {
        if (!want[2] || Node.matchesAttr(node, want[2])) return node;
      }
      node = node.parentNode;
    }
    return null;
  }
  static matchesAttr(node, attrSpec) {
    const m = attrSpec.match(/^\[([a-z-]+)\]$/);
    if (!m) throw new Error(`attr selector not shimmed: ${attrSpec}`);
    const key = m[1].replace(/^data-/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return node.dataset[key] !== undefined || node._attrs[m[1]] !== undefined;
  }
  querySelectorAll(selector) {
    if (selector !== "[data-dir]") throw new Error(`selector not shimmed: ${selector}`);
    const out = [];
    const walk = (n) =>
      n.childNodes.forEach((c) => {
        if (c.nodeType === 1 && c.dataset.dir !== undefined) out.push(c);
        walk(c);
      });
    walk(this);
    return out;
  }
  querySelector(selector) {
    const want = selector.match(/^([a-z-]+)$/);
    if (!want) throw new Error(`selector not shimmed: ${selector}`);
    return this.childNodes.find((c) => c.nodeType === 1 && c.tagName === want[1].toUpperCase()) || null;
  }
  setAttribute(k, v) {
    this._attrs[k] = String(v);
  }
  removeAttribute(k) {
    delete this._attrs[k];
  }
  hasAttribute(k) {
    return this._attrs[k] !== undefined;
  }
}

const HOSTILE = '<script>alert(1)</script>';
// Receive times must be recent for freshness to be computable.
const RECENT = Math.floor(Date.now() / 1000) - 120;
const HOST = {
  target: HOSTILE,
  role: "compute",
  protocol: "direct",
  state: "stale",
  helper_version: "1",
  helper_path: null,
  last_error: "timeout",
  backoff: 300,
  items: [
    {
      poll_id: "p1",
      captured_at: "2026-09-04T12:00:00Z",
      received_at: RECENT,
      cpu_busy: 0.5,
      load1: 1.25,
      load5: 1,
      load15: 0.75,
      ram_total: 34 * 2 ** 30,
      ram_used: 17 * 2 ** 30,
      root_total: 500 * 2 ** 30,
      root_free: 250 * 2 ** 30,
      capture_skew_seconds: 900,
      collection_duration_seconds: 0.3,
      partial: 1,
      boot_id: "boot-abc",
      observation_duration_seconds: 0.25,
      visible_processes: 2048,
      emitted_processes: 80,
      permission_denied: 1,
      counters_truncated: 1,
      limits_truncated: 1,
      nvml_supported: 1,
      nvml_error: "uuid_unavailable",
      psutil_error: null,
    },
  ],
  processes: [
    {
      pid: 1234,
      create_time: 1_788_586_800,
      uid: 1000,
      username: HOSTILE,
      name: "<img src=x onerror=alert(1)>",
      executable: HOSTILE,
      cpu_cores: 1.5,
      rss: 4 * 2 ** 30,
      gpu_index: 0,
      vram: 2 * 2 ** 30,
      gpu_allocations: [{gpu_uuid: "GPU-0", gpu_index: 0, vram_bytes: 2 * 2 ** 30}],
    },
  ],
  gpus: [
    {
      uuid: "GPU-0",
      idx: 0,
      model: HOSTILE,
      utilization: 0.75,
      vram_total: 11 * 2 ** 30,
      vram_used: 8 * 2 ** 30,
      temperature_c: 65,
      power_watts: 180.5,
      compute_process_count: 2,
      supported: 1,
      error: null,
      mig_detected: 0,
      availability: "busy",
      reason: "compute processes",
      flag: "busy",
    },
    {
      uuid: "unknown-1",
      idx: 1,
      model: null,
      utilization: null,
      vram_total: null,
      vram_used: null,
      temperature_c: null,
      power_watts: null,
      compute_process_count: 0,
      supported: 0,
      error: "uuid_unavailable",
      mig_detected: 0,
    },
  ],
  users: [
    {
      uid: 1000,
      username: HOSTILE,
      cpu_cores: 1.75,
      rss: 12 * 2 ** 30,
      process_count: 42,
      gpu_process_count: 2,
      vram: 6 * 2 ** 30,
    },
    {
      uid: 65534,
      username: null,
      cpu_cores: 0.25,
      rss: 300 * 2 ** 20,
      process_count: 5,
      gpu_process_count: 0,
      vram: 0,
    },
  ],
};
const CHARTS = {
  bounded: true,
  series: [
    {
      chart: "cpu",
      label: "cpu used",
      unit: "percent",
      points: Array.from({length: 60}, (_, i) => [1_788_590_000 + i * 60, i / 100]),
    },
    {
      chart: "gpu_util",
      label: `gpu0 ${HOSTILE}`,
      unit: "percent",
      points: Array.from({length: 60}, (_, i) => [1_788_590_000 + i * 60, 0.5]),
    },
    {
      chart: "gpu_util",
      label: "gpu1 Quadro",
      unit: "percent",
      points: Array.from({length: 60}, (_, i) => [1_788_590_000 + i * 60, 0.25]),
    },
  ],
};
const SPARKS = {
  bounded: true,
  points: 60,
  series: [
    {
      target: "alpha",
      points: Array.from({length: 20}, (_, i) => [1_788_590_000 + i * 60, i / 40]),
    },
    {target: "beta", points: []},
  ],
};
const OVERVIEW = [
  {
    target: "alpha",
    state: "live",
    last_received: RECENT,
    cpu_busy: 0.5,
    load1: 1.25,
    ram_used: 17 * 2 ** 30,
    ram_total: 34 * 2 ** 30,
    root_total: 500 * 2 ** 30,
    root_free: 250 * 2 ** 30,
    gpu_count: 1,
    gpu_utilization: 0.75,
    gpu_vram_used: 8 * 2 ** 30,
    gpu_vram_total: 11 * 2 ** 30,
    visible_users: 2,
    last_error: null,
  },
  {
    target: "beta",
    state: "unreachable",
    last_received: null,
    cpu_busy: null,
    load1: null,
    ram_used: null,
    ram_total: null,
    root_total: null,
    root_free: null,
    gpu_count: 0,
    gpu_utilization: null,
    gpu_vram_used: null,
    gpu_vram_total: null,
    visible_users: 0,
    last_error: "timeout",
  },
];
const IDLE_URL = "/api/idle-gpus?limit=500&offset=0";
const IDLE = {
  items: [
    {
      target: "beta",
      uuid: "GPU-B",
      idx: 0,
      model: "A100",
      utilization: 0.95,
      vram_total: 80 * 2 ** 30,
      vram_used: 79 * 2 ** 30,
      vram_free: 1 * 2 ** 30,
      compute_process_count: 6,
      received_at: RECENT,
      age_seconds: 3,
      availability: "busy",
      reason: "compute processes",
      flag: "busy",
      owners: ["alice", "bob", "carol", "dave"],
    },
    {
      target: "gamma",
      uuid: "GPU-C",
      idx: 1,
      model: "A100",
      utilization: 0.01,
      vram_total: 80 * 2 ** 30,
      vram_used: 2 * 2 ** 30,
      vram_free: 78 * 2 ** 30,
      compute_process_count: 0,
      received_at: RECENT,
      age_seconds: 4,
      availability: "idle",
      reason: "two idle observations",
    },
    {
      target: "alpha",
      uuid: "GPU-A",
      idx: 2,
      model: "RTX 4090",
      utilization: null,
      vram_total: 24 * 2 ** 30,
      vram_used: null,
      vram_free: null,
      compute_process_count: null,
      received_at: RECENT - 3600,
      age_seconds: 3600,
    },
  ],
  summary: {idle: 1, busy: 1, unknown: 1},
  limit: 500,
  offset: 0,
};
const HUB = {
  status: "ready",
  polling_enabled: true,
  in_flight: 1,
  inventory_age_seconds: 12.5,
  inventory_error: null,
  free_disk_bytes: 4 * 2 ** 30,
  database_size_bytes: 4096 * 16,
  wal_size_bytes: 1024 * 512,
  hub_rss_bytes: 48 * 2 ** 20,
  backup_status: "backup_disabled",
  retention_days: 30,
  uptime_seconds: 3600,
  version: HOSTILE,
  recent_polls_total: 41,
  recent_poll_errors: 1,
  recent_error_rate: 0.0244,
  recent_avg_latency_seconds: 0.2,
};
const JOBS = [
  {
    cluster: "c1",
    job_id: "101",
    array_task_id: "",
    step_id: "",
    state: "RUNNING",
    updated_at: RECENT,
  },
];
// A host with more processes than the workload cap so filtering, show-more
// and state preservation across refreshes are exercised.
const HEAVY = {
  ...HOST,
  users: [],
  gpus: [],
  processes: Array.from({length: 12}, (_, i) => ({
    pid: 2000 + i,
    create_time: RECENT - 100,
    uid: 1000 + (i % 3),
    username: i % 3 === 0 ? "alice" : "bob",
    name: `train${i}`,
    executable: "/usr/bin/python",
    cpu_cores: 1,
    rss: 2 ** 30,
    gpu_index: i < 3 ? i : null,
    vram: i < 3 ? 2 ** 30 : null,
    gpu_allocations:
      i < 3 ? [{gpu_uuid: "GPU-0", gpu_index: i, vram_bytes: 2 ** 30}] : [],
  })),
};

const intervalFns = [];
const intervalSpecs = [];
globalThis.setInterval = (fn, ms) => {
  intervalFns.push(fn);
  intervalSpecs.push(ms);
  return intervalFns.length;
};
globalThis.clearInterval = () => {};

function makeDocument(page, target) {
  const byId = {};
  const make = (id, tag) => (byId[id] = new Node(tag));
  const content = make("content", "section");
  const status = make("status", "p");
  const menu = make("menu", "button");
  const nav = make("nav", "nav");
  make("host-list", "ul");
  nav.classList = {toggle: () => true};
  const doc = {
    createElement: (tag) => new Node(tag),
    createElementNS: (_ns, tag) => new Node(tag, "svg"),
    getElementById: (id) => byId[id] || null,
    addEventListener(type, fn) {
      (doc.handlers[type] ||= []).push(fn);
    },
    handlers: {},
    body: new Node("body"),
    hidden: false,
  };
  doc.body.dataset.page = page;
  doc.body.dataset.target = target || "";
  doc.fireVisibility = () => doc.handlers.visibilitychange.forEach((fn) => fn());
  return {doc, content, status};
}

async function run(page, target, payloads) {
  const shim = makeDocument(page, target);
  globalThis.document = shim.doc;
  const before = intervalFns.length;
  const fetches = [];
  globalThis.fetch = async (url) => {
    fetches.push(url);
    const body = payloads ? payloads[url] : undefined;
    if (!body) throw new Error(`no fixture for ${url}`);
    return {ok: true, status: 200, json: async () => body};
  };
  new Function(script)();
  await new Promise((resolve) => setImmediate(resolve));
  return {
    shim,
    fetches,
    ticks: intervalFns.slice(before),
    specs: intervalSpecs.slice(before),
  };
}

const tables = (root) => collect(root, "TABLE");
const textOf = (n) => n.textContent;
const visibleRows = (tbody) =>
  Array.from(tbody.rows).filter((r) => r.style.display !== "none");
const collect = (root, tag) => {
  const out = [];
  const walk = (n) => {
    if (n.tagName === tag) out.push(n);
    n.childNodes.forEach(walk);
  };
  walk(root);
  return out;
};
const cardValues = (content) =>
  collect(content, "DIV")
    .filter((n) => String(n.className).split(" ")[0] === "card")
    .map((n) => n.childNodes[0].textContent);

// ---- overview: summary cards, working sidebar, sparklines, fraction meters ----
{
  const {shim, fetches, specs} = await run("overview", "", {
    "/api/overview": OVERVIEW,
    "/api/overview/sparklines": SPARKS,
    [IDLE_URL]: IDLE,
  });
  assert.ok(fetches.includes(IDLE_URL), "overview fetches the idle API for summary");
  assert.equal(specs[0], 2000, "overview refresh interval is 2s");
  assert.equal(specs.length, 2, "overview has current and history timers");
  const sidebar = shim.doc.getElementById("host-list");
  assert.equal(sidebar.childNodes.length, 2, "sidebar populated on overview");
  assert.match(sidebar.childNodes[0].textContent, /alpha.*live/, "sidebar text labels");
  assert.deepEqual(cardValues(shim.content), ["1", "1", "1", "1"], "summary card values");
  const table = tables(shim.content)[0];
  const rows = table.querySelector("tbody").rows;
  assert.equal(rows.length, 2, "overview rows");
  const cells = rows[0].cells.map(textOf);
  assert.ok(cells.includes("1 stale"), `gpu availability join: ${cells}`);
  assert.equal(rows[1].cells[2].textContent, "no GPUs", "host without GPUs labelled");
  assert.ok(cells.some((c) => c.includes("75.0%")), `utilization is a fraction: ${cells}`);
  assert.ok(cells.some((c) => /GiB/.test(c)), `bytes: ${cells}`);
  const cpuFill = collect(rows[0].cells[4], "SPAN").find((n) =>
    String(n.className).startsWith("meter-fill"),
  );
  assert.equal(cpuFill.style.width, "50.0%", "cpu meter fill from fraction");
  const gpuFill = collect(rows[0].cells[9], "SPAN").find((n) =>
    String(n.className).startsWith("meter-fill"),
  );
  assert.equal(gpuFill.style.width, "75.0%", "gpu util meter never divides by 100");
  const sparkCells = rows.map((r) => collect(r.cells[5], "SVG"));
  assert.equal(sparkCells[0].length, 1, "alpha row has a sparkline svg");
  const sparkPaths = collect(sparkCells[0][0], "PATH");
  assert.equal(sparkPaths.length, 2, "sparkline area fill + line");
  assert.equal(collect(rows[1].cells[5], "PATH").length, 0, "no spark paths without points");
  const head = table.childNodes[0];
  const th = head.rows[0].cells[4];
  head.handlers.click[0]({target: th});
  assert.equal(th.dataset.dir, "asc", "column sort enabled");
  assert.equal(rows[0].cells[4].dataset.raw, "0.5", "cpu meter column sorts by fraction");
  assert.equal(rows[0].cells[7].dataset.raw, "0.5", "ram meter column sorts by fraction");
  assert.equal(rows[0].cells[9].dataset.raw, "0.75", "gpu util meter column sorts by fraction");
  assert.equal(rows[1].cells[4].dataset.raw, "null", "missing meter value sorts as absent");
  head.handlers.keydown[0]({target: head.rows[0].cells[0], key: "Enter"});
  assert.equal(head.rows[0].cells[0].dataset.dir, "asc", "keyboard sort control");
  assert.match(shim.status.textContent, /sample .+ ago · fetched /, "sample age separated from fetch time");
}

// overview without the idle API yet: unknown, never zero, no crash
{
  const {shim, fetches} = await run("overview", "", {
    "/api/overview": OVERVIEW,
    "/api/overview/sparklines": SPARKS,
  });
  assert.ok(fetches.includes(IDLE_URL), "idle API attempted");
  const values = cardValues(shim.content);
  assert.deepEqual(
    values.slice(0, 3),
    ["unknown", "unknown", "unknown"],
    `gpu counts unknown without API: ${values}`,
  );
  assert.equal(values[3], "1", "attention hosts still computed");
}

// ---- host: GPU cards, issues, disclosures, charts, hostile text stays text ----
{
  const {shim, fetches, specs} = await run("host", "alpha", {
    "/api/hosts/alpha?limit=1": HOST,
    "/api/hosts/alpha/charts?hours=24": CHARTS,
    "/api/overview": OVERVIEW,
  });
  assert.equal(specs[0], 2000, "host refresh interval is 2s");
  assert.deepEqual(
    specs.slice(1),
    [30000, 30000],
    "history (>=30s) and sidebar intervals, charts independent of data refresh",
  );
  assert.ok(
    ["/api/hosts/alpha?limit=1", "/api/hosts/alpha/charts?hours=24", "/api/overview"].every(
      (u) => fetches.includes(u),
    ),
    "one request each for host data, chart group, sidebar",
  );
  const content = shim.content;
  const text = textOf(content);
  assert.match(text, /host state stale/, "issue: non-live state");
  assert.match(text, /last poll error: timeout/, "issue: poll error");
  assert.match(text, /clock drift 900 s vs hub/, "issue: clock drift");
  const cards = collect(content, "ARTICLE").filter((n) => n.className === "gpu-card");
  assert.equal(cards.length, 2, "one card per GPU");
  const busy = textOf(cards[0]);
  assert.match(busy, /GPU 0 · /, "card names gpu by index");
  assert.match(busy, /busy/, "backend availability badge busy");
  assert.match(busy, /75\.0%/, "utilization fraction rendered directly");
  assert.match(busy, /3\.0 GiB free \/ 11\.0 GiB/, "free vram emphasised");
  assert.match(busy, /2 compute procs/, "compute process count");
  assert.match(busy, /owners: /, "owners listed when known");
  assert.match(busy, /65 °C/, "temperature");
  assert.match(busy, /180\.5 W/, "power");
  const unknown = textOf(cards[1]);
  assert.match(unknown, /stale/, "absent availability renders stale, never idle");
  assert.match(unknown, /error: uuid_unavailable/, "gpu error shown");
  const meterBoxes = collect(content, "DIV").filter((n) => n.className === "meter-box");
  assert.equal(meterBoxes.length, 3, "compact cpu/ram/disk meters only");
  const disclosures = collect(content, "DETAILS");
  assert.equal(disclosures.length, 2, "users and diagnostics collapsed");
  assert.ok(disclosures.every((d) => !d.hasAttribute("open")), "collapsed by default");
  assert.match(textOf(disclosures[0].childNodes[0]), /Users/);
  assert.match(textOf(disclosures[1].childNodes[0]), /Technical diagnostics/);
  const diag = textOf(disclosures[1]);
  for (const label of [
    "helper version",
    "last captured",
    "last received",
    "capture skew",
    "poll backoff",
    "nvml error",
    "last poll error",
  ]) {
    assert.ok(diag.includes(label), `diagnostics missing ${label}: ${diag}`);
  }
  assert.ok(/host clock offset/.test(diag), "capture skew note");
  assert.ok(/counters truncated, limits truncated/.test(diag), "truncation labels");
  assert.match(text, /History/, "history grid before long tables");
  const figures = collect(content, "FIGURE");
  assert.equal(figures.length, 2, "cpu + gpu_util figures; empty series skipped");
  const linePaths = collect(content, "PATH").filter((p) => p._attrs.class.includes("line"));
  const areaPaths = collect(content, "PATH").filter((p) => p._attrs.class.includes("area"));
  assert.equal(linePaths.length, 3, "one solid line per series");
  assert.equal(areaPaths.length, 3, "one flat area fill per series");
  areaPaths.forEach((p) => assert.ok(p._attrs.d.endsWith("Z"), "area closed to baseline"));
  const texts = collect(content, "TEXT").map((n) => n.textContent);
  assert.ok(
    texts.includes("0%") && texts.includes("50%") && texts.includes("100%"),
    "y-axis tick labels with units",
  );
  const inputs = collect(content, "INPUT");
  assert.equal(inputs.length, 2, "workload search + gpu-only toggle");
  assert.ok(text.includes(HOSTILE), "hostile username kept as literal text");
  assert.ok(!text.includes("undefined") && !text.includes("[object Object]"), "no raw JS values");
}

// host without samples stays explicit; no-GPU header derives from host.gpus
{
  const empty = {...HOST, items: [], gpus: [HOST.gpus[1]]};
  const {shim} = await run("host", "alpha", {
    "/api/hosts/alpha?limit=1": empty,
    "/api/hosts/alpha/charts?hours=24": {series: []},
    "/api/overview": OVERVIEW,
  });
  const text = textOf(shim.content);
  assert.ok(text.includes("no samples yet"), "explicit no-samples label");
  assert.ok(collect(shim.content, "ARTICLE").length === 1, "gpus render without samples");
}

// host with no GPUs and unknown meters: unknown is never rendered as zero
{
  const noGpu = {
    ...HOST,
    gpus: [],
    items: [{...HOST.items[0], cpu_busy: null, ram_used: null}],
  };
  const {shim} = await run("host", "alpha", {
    "/api/hosts/alpha?limit=1": noGpu,
    "/api/hosts/alpha/charts?hours=24": {series: []},
    "/api/overview": OVERVIEW,
  });
  const text = textOf(shim.content);
  assert.ok(text.includes("No current GPU readings"), "no-GPU label from host.gpus");
  assert.ok(text.includes("unknown"), "missing values read unknown");
  assert.ok(
    !/(^|[^0-9])0\.0%/.test(text),
    `no fabricated zero meters: ${text.slice(0, 400)}`,
  );
}

// ---- workloads: top 10 cap, show-more, gpu-only toggle, search survives refresh ----
{
  const {shim, ticks} = await run("host", "alpha", {
    "/api/hosts/alpha?limit=1": HEAVY,
    "/api/hosts/alpha/charts?hours=24": {series: []},
    "/api/overview": OVERVIEW,
  });
  const content = shim.content;
  const getTable = () => tables(content).find((t) => textOf(t.childNodes[0]).includes("pid"));
  const inputs = () => collect(content, "INPUT");
  const button = () => collect(content, "BUTTON").find((b) => b.className === "more-btn");
  assert.equal(visibleRows(getTable().querySelector("tbody")).length, 10, "initially top 10");
  assert.match(button().textContent, /Show all \(12\)/, "show-more with match count");
  button().handlers.click[0]();
  assert.equal(visibleRows(getTable().querySelector("tbody")).length, 12, "show all rows");
  assert.match(button().textContent, /Show top 10/, "collapse control after expanding");
  const [search, gpuBox] = inputs();
  gpuBox.checked = true;
  gpuBox.handlers.change[0]();
  assert.equal(visibleRows(getTable().querySelector("tbody")).length, 3, "gpu-only toggle");
  gpuBox.checked = false;
  gpuBox.handlers.change[0]();
  search.value = "alice";
  search.handlers.input[0]();
  assert.equal(visibleRows(getTable().querySelector("tbody")).length, 4, "search filters");
  const head = getTable().childNodes[0];
  head.handlers.click[0]({target: head.rows[0].cells[0]});
  assert.equal(head.rows[0].cells[0].dataset.dir, "asc", "sort by pid");
  ticks[0](); // next 2s refresh: search, sort and cap must survive the rebuild
  await new Promise((resolve) => setImmediate(resolve));
  const [searchAfter] = collect(content, "INPUT");
  assert.equal(searchAfter.value, "alice", "search text preserved across refresh");
  assert.equal(visibleRows(getTable().querySelector("tbody")).length, 4, "filter preserved");
  const pidTh = getTable().childNodes[0].rows[0].cells[0];
  assert.equal(pidTh.dataset.dir, "asc", "sort choice preserved across refresh");
  const firstPid = Number(visibleRows(getTable().querySelector("tbody"))[0].cells[0].textContent);
  assert.ok(firstPid < 3000, `sorted order retained: ${firstPid}`);
}

// ---- idle GPUs: idle first, filters, unknown-not-idle, freshness ----
{
  const {shim, fetches, specs} = await run("idle-gpus", "", {
    [IDLE_URL]: IDLE,
    "/api/overview": OVERVIEW,
  });
  assert.deepEqual(fetches, [IDLE_URL, "/api/overview"], "idle page fetches idle rows plus sidebar");
  assert.equal(specs[0], 2000, "idle page refresh interval is 2s");
  assert.deepEqual(cardValues(shim.content), ["1", "1", "1", "3"], "summary counts");
  const content = shim.content;
  assert.match(textOf(content), /not a reservation/, "observed-availability note");
  const rows = () => Array.from(tables(content)[0].querySelector("tbody").rows);
  assert.equal(rows().length, 1, "idle-only by default");
  assert.match(textOf(rows()[0]), /gamma/, "idle GPUs listed first");
  assert.match(textOf(rows()[0].cells[3]), /idle/, "idle badge");
  const [hostInput, modelInput, freeInput, busyBox, staleBox] = collect(content, "INPUT");
  busyBox.checked = true;
  busyBox.handlers.change[0]();
  assert.equal(rows().length, 2, "include-busy control");
  assert.match(textOf(rows()[1].cells[0]), /beta/, "busy after idle");
  assert.match(textOf(rows()[1].cells[3]), /busy/, "busy badge");
  assert.match(textOf(rows()[1].cells[4]), /alice, bob, carol \+1 more/, "owners capped with overflow");
  staleBox.checked = true;
  staleBox.handlers.change[0]();
  assert.equal(rows().length, 3, "include-stale control");
  assert.match(textOf(rows()[2].cells[0]), /alpha/, "stale last");
  assert.match(textOf(rows()[2].cells[3]), /stale/, "missing availability is stale, never idle");
  assert.match(textOf(rows()[2].cells[9]), /1 h/, "freshness shown from age");
  hostInput.value = "gam";
  hostInput.handlers.input[0]();
  assert.equal(rows().length, 1, "host filter");
  hostInput.value = "";
  hostInput.handlers.input[0]();
  modelInput.value = "rtx";
  modelInput.handlers.input[0]();
  assert.equal(rows().length, 1, "model filter");
  modelInput.value = "";
  modelInput.handlers.input[0]();
  freeInput.value = "70";
  freeInput.handlers.input[0]();
  assert.equal(rows().length, 1, "min free GiB filter");
  freeInput.value = "";
  freeInput.handlers.input[0]();
  assert.match(textOf(content), /showing 3 of 3 listed/, "clear shown-of counts");
}

// idle page without the backend API yet: graceful, unknown summary
{
  const {shim} = await run("idle-gpus", "", {"/api/overview": OVERVIEW});
  assert.match(shim.status.textContent, /Idle GPU API not available yet/);
  assert.match(textOf(shim.content), /not available yet/, "explicit integration note");
  assert.deepEqual(
    cardValues(shim.content).slice(0, 3),
    ["unknown", "unknown", "unknown"],
    "no independent idle declaration",
  );
}

// ---- hub status ----
{
  const {shim} = await run("hub-status", "", {"/api/hub-status": HUB});
  const text = textOf(shim.content);
  for (const label of [
    "status",
    "polling",
    "in-flight polls",
    "inventory age",
    "recent polls (1h)",
    "recent avg latency",
    "hub rss",
    "database size",
    "wal size",
    "free disk",
    "retention",
    "backup",
    "uptime",
    "version",
  ]) {
    assert.ok(text.includes(label), `hub status missing ${label}: ${text}`);
  }
  assert.ok(/MiB|GiB|KiB/.test(text), "byte units on hub status");
  assert.ok(text.includes(HOSTILE), "hub version as text");
}

// ---- jobs ----
{
  const {shim} = await run("jobs", "", {"/api/jobs": JOBS});
  assert.ok(textOf(shim.content).includes("101"), "job rows render");
}

// ---- polling pauses while hidden, refreshes once on return ----
{
  const {shim, fetches, ticks} = await run("overview", "", {
    "/api/overview": OVERVIEW,
    "/api/overview/sparklines": SPARKS,
    [IDLE_URL]: IDLE,
  });
  assert.equal(fetches.length, 3, "initial refresh (table + sparklines + idle)");
  shim.doc.hidden = true;
  globalThis.fetch = async () => {
    throw new Error("fetched while hidden");
  };
  ticks.forEach(tick => tick()); // all timers pause while hidden
  shim.doc.fireVisibility();
  assert.equal(fetches.length, 3, "no refresh while hidden");
  shim.doc.hidden = false;
  globalThis.fetch = async (url) => {
    fetches.push(url);
    const body = {"/api/overview": OVERVIEW}[url] || IDLE;
    return {ok: true, json: async () => body};
  };
  shim.doc.fireVisibility(); // visible again: exactly one immediate refresh
  await new Promise((r) => setImmediate(r));
  assert.equal(fetches.length, 6, "immediate refresh on return");
  shim.doc.fireVisibility(); // nothing missed: no duplicate refresh
  await new Promise((r) => setImmediate(r));
  assert.equal(fetches.length, 6, "no duplicate refresh");
}

// ---- fetch failure surfaces in the status line ----
{
  const {shim} = await run("overview", "", null);
  assert.match(shim.status.textContent, /Unable to load dashboard data/);
  assert.equal(shim.status.className, "error");
  assert.equal(tables(shim.content).length, 0);
}

console.log("js harness ok");
