/* Fleetmon — small dashboard script. No framework, no build step.
   Every remote value is rendered through textContent; never innerHTML.
   Units: bytes are stored server-side and converted only here (binary).
   GPU utilization is a stored fraction — it is never divided by 100.
   Unknown is rendered as "unknown", never as zero. GPU state is always
   one hardened flag — busy, idle, or stale — and owner names render
   whenever the stored allocations name them.
   Polling pauses while the tab is hidden and refreshes once on return. */
(() => {
  "use strict";

  const page = document.body.dataset.page;
  const target = document.body.dataset.target;
  const REFRESH_MS = {overview: 2000, host: 2000, "idle-gpus": 2000, jobs: 15000, "hub-status": 30000};
  const SIDEBAR_MS = 30000;
  const HISTORY_MS = 30000;
  const FETCH_TIMEOUT_MS = 8000;
  const IDLE_ENDPOINT = "/api/idle-gpus?limit=500&offset=0";
  const STALE_SAMPLE_S = 300;
  const DISK_ISSUE_FRACTION = 0.9;
  const CLOCK_DRIFT_S = 120;
  const WORKLOAD_LIMIT = 10;
  const OK_STATES = {live: true, partial: true, scheduler: true, retired: true, polling_disabled: true};
  const hostEndpoint = page === "host" ? `/api/hosts/${encodeURIComponent(target)}` : null;
  const endpoint =
    (hostEndpoint ? `${hostEndpoint}?limit=1` : null) ||
    (page === "hub-status"
      ? "/api/hub-status"
      : page === "idle-gpus"
        ? IDLE_ENDPOINT
        : `/api/${page}`);
  const chartsEndpoint = hostEndpoint ? `${hostEndpoint}/charts` : null;
  const sparkEndpoint = page === "overview" ? "/api/overview/sparklines" : null;
  const RANGES = [["1h", 1], ["6h", 6], ["24h", 24], ["7d", 7 * 24]];
  let chartHours = 24;
  const status = document.getElementById("status");
  const content = document.getElementById("content");

  const UNKNOWN = "unknown";
  const DASH = "—";

  // ---- units ----
  function _number(v) {
    return typeof v === "number" && isFinite(v);
  }
  function _bytes(v) {
    if (!_number(v) || v < 0) return UNKNOWN;
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let size = v;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return unit === 0 ? `${v.toFixed(0)} ${units[0]}` : `${size.toFixed(1)} ${units[unit]}`;
  }
  // "used / total" in the total's unit — one unit instead of three tokens.
  function _pair(used, total) {
    if (!_number(used) || !_number(total) || total <= 0) return UNKNOWN;
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let unit = 0;
    let size = total;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    const scale = 1024 ** unit;
    const fmt = (value) => (unit === 0 ? value.toFixed(0) : (value / scale).toFixed(1));
    return `${fmt(used)} / ${fmt(total)} ${units[unit]}`;
  }
  function _pct(fraction) {
    return _number(fraction) ? `${(fraction * 100).toFixed(1)}%` : UNKNOWN;
  }
  function _pctOf(numerator, denominator) {
    return _number(numerator) && _number(denominator) && denominator > 0
      ? _pct(numerator / denominator)
      : UNKNOWN;
  }
  function _fixed(v, decimals) {
    return _number(v) ? v.toFixed(decimals) : UNKNOWN;
  }
  function _cores(v) {
    return _fixed(v, 2);
  }
  function _int(v) {
    return _number(v) ? String(v) : UNKNOWN;
  }
  function _dur(s) {
    if (!_number(s) || s < 0) return UNKNOWN;
    const total = Math.floor(s);
    if (total < 120) return `${s.toFixed(1)} s`;
    if (total < 3600) return `${Math.floor(total / 60)} m ${total % 60} s`;
    if (total < 86400) {
      return `${Math.floor(total / 3600)} h ${Math.floor((total % 3600) / 60)} m`;
    }
    return `${Math.floor(total / 86400)} d ${Math.floor((total % 86400) / 3600)} h`;
  }
  function _clock(epochSeconds) {
    if (!_number(epochSeconds)) return UNKNOWN;
    return new Date(epochSeconds * 1000).toLocaleString();
  }
  function _clockIso(iso) {
    if (typeof iso !== "string" || !iso) return UNKNOWN;
    const parsed = Date.parse(iso);
    return isFinite(parsed) ? new Date(parsed).toLocaleString() : UNKNOWN;
  }
  function _age(receivedAt) {
    return _number(receivedAt) ? _dur(Date.now() / 1000 - receivedAt) : UNKNOWN;
  }
  function _ageRaw(receivedAt) {
    return _number(receivedAt) ? Date.now() / 1000 - receivedAt : "";
  }
  function _yes(v) {
    if (v === 1 || v === true) return "yes";
    if (v === 0 || v === false) return "no";
    return UNKNOWN;
  }
  function _text(v) {
    if (v === null || v === undefined || v === "") return DASH;
    if (Array.isArray(v)) return `${v.length} item${v.length === 1 ? "" : "s"}`;
    if (typeof v === "object") {
      const keys = Object.keys(v);
      return keys.length ? `${keys.length} field${keys.length === 1 ? "" : "s"}` : DASH;
    }
    return String(v);
  }

  // ---- state tokens: lowercase text with a color class ----
  const TOKEN_COLORS = {
    live: "st-ok",
    partial: "st-partial",
    partial_flag: "st-partial",
    stale: "st-stale",
    slurm_stale: "st-stale",
    scheduler: "st-ok",
    polling_disabled: "st-stale",
    retired: "st-muted",
    unknown: "st-muted",
    unreachable: "st-bad",
    helper_missing: "st-bad",
    version_mismatch: "st-bad",
    unsupported: "st-bad",
    unsupported_python: "st-bad",
    timeout: "st-bad",
    output_overflow: "st-bad",
    transport: "st-bad",
    invalid_json: "st-bad",
    invalid_schema: "st-bad",
    backup_disabled: "st-muted",
    ready: "st-ok",
    critical: "st-bad",
  };
  function token(value, raw) {
    const text = typeof value === "string" && value ? value : UNKNOWN;
    const label = ({partial: "limited detail", retired: "not monitored", polling_disabled: "excluded", slurm_stale: "scheduler stale"})[text] || text;
    const span = el("span", "st " + (TOKEN_COLORS[text] || "st-bad"), label);
    span.dataset.raw = raw === undefined ? text : String(raw);
    return span;
  }

  // ---- DOM helpers ----
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function clear() {
    focusPairs.length = 0;
    content.replaceChildren();
  }
  function heading(text) {
    content.append(el("h3", null, text));
  }
  function note(text) {
    content.append(el("p", "note", text));
  }

  // ---- UI state preserved across refreshes ----
  const ui = {
    search: "",
    gpuOnly: false,
    processLimit: WORKLOAD_LIMIT,
    usersOpen: false,
    diagOpen: false,
    idleHost: "",
    idleModel: "",
    idleMinFree: "",
    idleBusy: false,
    idleStale: false,
    workloadSearchFocus: false,
    idleHostFocus: false,
    idleModelFocus: false,
    idleFreeFocus: false,
  };
  const sortState = new Map();
  const focusPairs = [];

  function trackFocus(node, flag) {
    focusPairs.push([node, flag]);
    node.addEventListener("focus", () => {
      ui[flag] = true;
    });
    node.addEventListener("blur", () => {
      ui[flag] = false;
    });
  }
  function refocusTracked() {
    focusPairs.forEach(([node, flag]) => {
      if (ui[flag] && typeof node.focus === "function") node.focus();
    });
  }

  function sortTbody(tbody, position, kind, direction) {
    const rows = Array.from(tbody.rows);
    rows.sort((a, b) => {
      const av = a.cells[position].dataset.raw;
      const bv = b.cells[position].dataset.raw;
      if (kind === "num") {
        const an = Number(av);
        const bn = Number(bv);
        const aMissing = av === "" || av === undefined || !isFinite(an);
        const bMissing = bv === "" || bv === undefined || !isFinite(bn);
        if (aMissing || bMissing) return (aMissing ? 1 : 0) - (bMissing ? 1 : 0);
        return (an - bn) * direction;
      }
      return String(av).localeCompare(String(bv)) * direction;
    });
    rows.forEach((row) => tbody.append(row));
  }

  function dataTable(columns, tableKey) {
    const table = el("table");
    const head = el("thead");
    const headRow = el("tr");
    columns.forEach((column, position) => {
      const th = el("th", column.num ? "num" : "", column.label);
      th.dataset.position = String(position);
      if (column.sort !== false) th.dataset.kind = column.num ? "num" : "text";
      th.setAttribute("tabindex", "0");
      headRow.append(th);
    });
    head.append(headRow);
    table.append(head);
    const tbody = el("tbody");
    table.append(tbody);
    const activate = (th) => {
      const kind = th.dataset.kind;
      const position = Number(th.dataset.position);
      const direction = th.dataset.dir === "asc" ? -1 : 1;
      headRow.querySelectorAll("[data-dir]").forEach((other) => {
        if (other !== th) other.removeAttribute("data-dir");
      });
      th.dataset.dir = direction === 1 ? "asc" : "desc";
      if (tableKey) sortState.set(tableKey, {position, kind, direction});
      sortTbody(tbody, position, kind, direction);
    };
    head.addEventListener("click", (event) => {
      const th = event.target.closest("th[data-kind]");
      if (th) activate(th);
    });
    head.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const th = event.target.closest("th[data-kind]");
      if (!th) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      activate(th);
    });
    // Re-apply the user's last sort after a refresh rebuilt the rows.
    tbody.applySavedSort = () => {
      const saved = tableKey ? sortState.get(tableKey) : null;
      if (!saved) return;
      const th = headRow.cells[saved.position];
      if (!th) return;
      th.dataset.dir = saved.direction === 1 ? "asc" : "desc";
      sortTbody(tbody, saved.position, saved.kind, saved.direction);
    };
    return tbody;
  }

  function cell(column, descriptor) {
    const td = el("td", column.num ? "num" : "");
    if (descriptor === null || descriptor === undefined) {
      td.textContent = DASH;
    } else if (descriptor.nodeType) {
      td.append(descriptor);
    } else if (descriptor.node) {
      td.append(descriptor.node);
    } else if (typeof descriptor === "object") {
      td.textContent = descriptor.text;
    } else {
      td.textContent = _text(descriptor);
    }
    const raw =
      descriptor && descriptor.raw !== undefined ? descriptor.raw : td.textContent;
    td.dataset.raw = String(raw);
    return td;
  }
  function num(text, raw) {
    return {text: _text(text), raw: raw === undefined ? "" : raw};
  }
  // btop-style proportion meter: flat threshold-colored fill, label kept as
  // text next to it (color is never the only cue). tone "auto" thresholds
  // at 60/85%; "neutral" is a single color (high utilization is not an
  // error state for GPUs).
  function meter(fraction, label, tone) {
    const pct =
      _number(fraction) && fraction >= 0 ? Math.min(fraction * 100, 100) : null;
    const cls =
      pct === null
        ? "unknown"
        : tone === "neutral"
          ? "neutral"
          : pct >= 85
            ? "high"
            : pct >= 60
              ? "warn"
              : "ok";
    const wrap = el("span", "meter");
    if (pct === null) {
      wrap.append(el("span", "meter-value", label === undefined ? UNKNOWN : label));
      return wrap;
    }
    const track = el("span", "meter-track");
    const fill = el("span", "meter-fill " + cls);
    fill.style.width = pct.toFixed(1) + "%";
    track.append(fill);
    wrap.append(track);
    wrap.append(el("span", "meter-value", label === undefined ? UNKNOWN : label));
    return wrap;
  }
  function meterBox(label, fraction, valueText, tone) {
    const box = el("div", "meter-box");
    box.append(el("span", "meter-label", label));
    box.append(
      meter(fraction, valueText === undefined ? UNKNOWN : valueText, tone),
    );
    return box;
  }
  function rowOf(tbody, columns, values) {
    const tr = el("tr");
    values.forEach((value, index) => tr.append(cell(columns[index], value)));
    tbody.append(tr);
    return tr;
  }

  function kvTable(rows) {
    const table = el("table", "kv");
    const tbody = el("tbody");
    rows.forEach(([label, value, cls]) => {
      const tr = el("tr");
      const th = el("th", null, label);
      th.scope = "row";
      const td = el("td", cls || "");
      if (value === null || value === undefined) td.textContent = DASH;
      else if (typeof value === "string") td.textContent = value;
      else if (value.nodeType) td.append(value);
      else if (value.node) td.append(value.node);
      else if (typeof value.text === "string") td.textContent = value.text;
      else td.textContent = DASH;
      tr.append(th, td);
      tbody.append(tr);
    });
    table.append(tbody);
    return table;
  }

  function detailsBlock(title, open, onToggle) {
    const det = el("details", "disclosure");
    if (open) det.setAttribute("open", "");
    det.append(el("summary", null, title));
    det.addEventListener("toggle", () => {
      if (onToggle) onToggle(det.hasAttribute("open"));
    });
    return det;
  }

  // Hardened three-value GPU flag: busy, idle, or stale. Anything the
  // backend cannot confirm from fresh, trusted observations is stale.
  function gpuFlag(value) {
    return value === "busy" || value === "idle" ? value : "stale";
  }
  function itemFlag(item) {
    if (!item || typeof item !== "object") return "stale";
    return gpuFlag(item.flag !== undefined ? item.flag : item.availability);
  }
  function availabilityToken(value, reason) {
    const flag = gpuFlag(value);
    const span = el("span", `badge badge-${flag}`, flag);
    if (reason) span.setAttribute("title", String(reason));
    return span;
  }

  function rowsOf(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.items)) return data.items;
    return [];
  }
  function hostLink(value) {
    if (typeof value !== "string" || !value) return DASH;
    const link = el("a", null, value);
    link.href = `/host/${encodeURIComponent(value)}`;
    return {node: link, raw: value};
  }
  function rootDisk(total, free) {
    if (!_number(total) || !_number(free) || total <= 0) return UNKNOWN;
    return `${_bytes(total - free)} / ${_bytes(total)} (${_pct((total - free) / total)})`;
  }
  function diskFraction(sample) {
    return _number(sample.root_total) && _number(sample.root_free) && sample.root_total > 0
      ? (sample.root_total - sample.root_free) / sample.root_total
      : null;
  }
  function ramFraction(sample) {
    return _number(sample.ram_total) && _number(sample.ram_used) && sample.ram_total > 0
      ? sample.ram_used / sample.ram_total
      : null;
  }
  function ramLabel(sample, withPct) {
    if (!_number(sample.ram_used) || !_number(sample.ram_total)) return UNKNOWN;
    const pair = _pair(sample.ram_used, sample.ram_total);
    return withPct ? `${pair} (${_pctOf(sample.ram_used, sample.ram_total)})` : pair;
  }

  // ---- summary cards ----
  function card(label, value, tone) {
    const node = el("div", `card${tone ? ` tone-${tone}` : ""}`);
    node.append(el("span", "card-value", value));
    node.append(el("span", "card-label", label));
    return node;
  }
  function summaryValue(idleDoc, key) {
    const summary =
      idleDoc && idleDoc.summary && typeof idleDoc.summary === "object" ? idleDoc.summary : null;
    return summary && _number(summary[key]) ? String(summary[key]) : UNKNOWN;
  }

  // ---- sidebar ----
  function renderSidebar(rows) {
    const listRoot = document.getElementById("host-list");
    if (!listRoot) return;
    listRoot.replaceChildren();
    rowsOf(rows).forEach((host) => {
      if (!host || typeof host !== "object" || !host.target) return;
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = `/host/${encodeURIComponent(host.target)}`;
      if (page === "host" && host.target === target) link.className = "active";
      link.append(el("span", "sb-name", host.target));
      link.append(token(host.state, ""));
      li.append(link);
      listRoot.append(li);
    });
  }

  let sidebarInFlight = false;
  async function refreshSidebar() {
    if (page === "overview" || sidebarInFlight) return;
    sidebarInFlight = true; // rendered from the page data itself
    try {
      const data = await fetchJson("/api/overview");
      renderSidebar(data && data.items);
    } catch (_error) {
      /* sidebar keeps its previous content */
    } finally { sidebarInFlight = false; }
  }

  // ---- overview ----
  function idleGpuCountsByHost(idleDoc) {
    const map = new Map();
    rowsOf(idleDoc).forEach((item) => {
      if (!item || typeof item !== "object" || !item.target) return;
      const entry = map.get(item.target) || {idle: 0, busy: 0, stale: 0};
      entry[itemFlag(item)] += 1;
      map.set(item.target, entry);
    });
    return map;
  }
  function hostNeedsAttention(host) {
    if (!host || typeof host !== "object") return false;
    const state = typeof host.state === "string" && host.state ? host.state : UNKNOWN;
    if (!OK_STATES[state]) return true;
    if (host.last_error) return true;
    const age = _ageRaw(host.last_received);
    if (_number(age) && age > STALE_SAMPLE_S) return true;
    const disk = diskFraction(host);
    if (disk !== null && disk >= DISK_ISSUE_FRACTION) return true;
    return false;
  }
  function gpuSummary(host, counts) {
    if (_number(host.gpu_count) && host.gpu_count === 0) {
      const none = el("span", "gpu-none", "no GPUs");
      return {node: none, raw: ""};
    }
    const entry = counts.get(host.target);
    if (!entry) return {text: UNKNOWN, raw: ""};
    const wrap = el("span", "gpu-sum");
    if (entry.idle) wrap.append(el("span", "st-ok", `${entry.idle} idle`));
    if (entry.busy) wrap.append(el("span", "st-busy", `${entry.busy} busy`));
    if (entry.stale) wrap.append(el("span", "st-stale", `${entry.stale} stale`));
    if (!wrap.childNodes.length) wrap.append(el("span", "st-muted", UNKNOWN));
    return {node: wrap, raw: String(entry.idle)};
  }

  function renderOverview(rows, idleDoc) {
    clear();
    const list = rowsOf(rows);
    renderSidebar(list);
    if (!list.length) {
      note("no hosts yet");
      return;
    }
    const counts = idleGpuCountsByHost(idleDoc);
    const attention = list.filter((host) => hostNeedsAttention(host)).length;
    const cards = el("div", "cards");
    cards.append(card("idle GPUs", summaryValue(idleDoc, "idle"), "ok"));
    cards.append(card("busy GPUs", summaryValue(idleDoc, "busy"), "busy"));
    cards.append(card("stale GPUs", summaryValue(idleDoc, "unknown"), "stale"));
    cards.append(card("needs attention", String(attention), attention ? "bad" : "ok"));
    content.append(cards);
    const sparks = sparkMap(lastSparks);
    const columns = [
      {label: "host"},
      {label: "state"},
      {label: "gpus"},
      {label: "age", num: true},
      {label: "cpu", num: true},
      {label: "cpu history"},
      {label: "load (1m)", num: true},
      {label: "ram", num: true},
      {label: "root disk", num: true},
      {label: "gpu util", num: true},
      {label: "gpu vram", num: true},
      {label: "error"},
    ];
    const tbody = dataTable(columns, "overview");
    list.forEach((host) => {
      if (!host || typeof host !== "object") return;
      const spark = sparks.get(host.target);
      rowOf(tbody, columns, [
        hostLink(host.target),
        token(host.state),
        gpuSummary(host, counts),
        num(_age(host.last_received), _ageRaw(host.last_received)),
        {node: meter(host.cpu_busy, _pct(host.cpu_busy)), raw: host.cpu_busy},        spark ? {node: sparkline(spark), raw: ""} : DASH,
        num(_cores(host.load1), host.load1),
        {
          node: meter(ramFraction(host), ramLabel(host)),
          raw: ramFraction(host),
        },
        {
          node: meter(
            diskFraction(host),
            _pair(
              _number(host.root_total) && _number(host.root_free)
                ? host.root_total - host.root_free
                : null,
              host.root_total,
            ),
          ),
          raw:
            _number(host.root_total) && _number(host.root_free)
              ? host.root_total - host.root_free
              : "",
        },
        {
          node: meter(
            _number(host.gpu_utilization) ? host.gpu_utilization : null,
            host.gpu_count ? _pct(host.gpu_utilization) : DASH,
            "neutral",
          ),
          raw: host.gpu_utilization,
        },
        {
          node: meter(
            _number(host.gpu_vram_used) &&
              _number(host.gpu_vram_total) &&
              host.gpu_vram_total > 0
              ? host.gpu_vram_used / host.gpu_vram_total
              : null,
            host.gpu_count ? _pair(host.gpu_vram_used, host.gpu_vram_total) : DASH,
          ),
          raw: host.gpu_vram_used,
        },
        host.last_error ? token(host.last_error) : DASH,
      ]);
    });
    tbody.applySavedSort();
    content.append(tbody.parentNode);
  }

  // ---- host ----
  function renderHost(data) {
    clear();
    const host = data && typeof data === "object" && !Array.isArray(data) ? data : {};
    const latest = Array.isArray(host.items) && host.items.length ? host.items[0] : null;
    renderHostBar(host, latest);
    renderIssues(host, latest);
    renderGpus(host.gpus, host.processes);
    renderMeters(latest);
    renderCharts();
    renderWorkloads(host.processes);
    renderUsers(host.users, latest);
    renderDiagnostics(host, latest);
    if (!latest) note("no samples yet for this host");
  }

  function renderHostBar(host, latest) {
    const bar = el("div", "host-bar");
    bar.append(el("span", "host-name", target));
    bar.append(token(host.state));
    if (host.last_error) bar.append(token(host.last_error));
    bar.append(
      el(
        "span",
        "sample-age",
        latest ? `sample ${_age(latest.received_at)} ago` : "no samples yet",
      ),
    );
    content.append(bar);
  }

  function collectIssues(host, latest) {
    const issues = [];
    const state = typeof host.state === "string" && host.state ? host.state : UNKNOWN;
    if (!OK_STATES[state]) issues.push(`host state ${state}`);
    if (host.last_error) issues.push(`last poll error: ${host.last_error}`);
    if (latest) {
      const disk = diskFraction(latest);
      if (disk !== null && disk >= DISK_ISSUE_FRACTION)
        issues.push(`Disk ${Math.round(disk * 100)}% full · ${_bytes(latest.root_free)} remaining`);
      const skew = latest.capture_skew_seconds;
      if (_number(skew) && Math.abs(skew) > CLOCK_DRIFT_S)
        issues.push(`clock drift ${skew.toFixed(0)} s vs hub`);
      const age = _ageRaw(latest.received_at);
      if (_number(age) && age > STALE_SAMPLE_S) issues.push(`sample ${_dur(age)} old`);
    }
    return issues;
  }
  function renderIssues(host, latest) {
    const issues = collectIssues(host, latest);
    const wrap = el("div", issues.length ? "issues" : "issues none");
    if (issues.length) {
      issues.forEach((text) => wrap.append(el("span", "issue", text)));
    } else {
      wrap.append(el("span", "issue-ok", "no issues flagged"));
    }
    content.append(wrap);
  }

  function gpuProcessMap(processes) {
    const byUuid = new Map();
    (Array.isArray(processes) ? processes : []).forEach((process) => {
      if (!process || !Array.isArray(process.gpu_allocations)) return;
      process.gpu_allocations.forEach((alloc) => {
        if (!alloc || !alloc.gpu_uuid) return;
        const entries = byUuid.get(alloc.gpu_uuid) || [];
        if (entries.length < 4) {
          const vram = alloc.vram_bytes ? ` (${_bytes(alloc.vram_bytes)})` : "";
          entries.push(`${_int(process.pid)} ${process.name || "unknown"}${vram}`);
        }
        byUuid.set(alloc.gpu_uuid, entries);
      });
    });
    return byUuid;
  }
  function gpuOwnerMap(processes) {
    const byUuid = new Map();
    (Array.isArray(processes) ? processes : []).forEach((process) => {
      if (!process || typeof process !== "object") return;
      const owner = process.username
        ? String(process.username)
        : _number(process.uid)
          ? `uid ${process.uid}`
          : null;
      if (!owner) return;
      (Array.isArray(process.gpu_allocations) ? process.gpu_allocations : []).forEach(
        (alloc) => {
          if (!alloc || !alloc.gpu_uuid) return;
          const owners = byUuid.get(alloc.gpu_uuid) || [];
          if (!owners.includes(owner)) owners.push(owner);
          byUuid.set(alloc.gpu_uuid, owners);
        },
      );
    });
    return byUuid;
  }

  function meterRow(label, meterNode) {
    const row = el("div", "gpu-meter");
    row.append(el("span", "meter-label", label));
    row.append(meterNode);
    return row;
  }
  // Owner names, capped at three with an overflow count.
  function ownersText(owners) {
    const shown = owners.slice(0, 3).join(", ");
    return owners.length > 3 ? `${shown} +${owners.length - 3} more` : shown;
  }

  function gpuCard(gpu, procEntries, owners) {
    const card = el("article", "gpu-card");
    const head = el("div", "gpu-head");
    head.append(el("span", "gpu-name", `GPU ${_int(gpu.idx)} · ${_text(gpu.model)}`));
    head.append(availabilityToken(itemFlag(gpu), gpu.reason));
    card.append(head);
    card.append(
      meterRow(
        "utilization",
        meter(
          _number(gpu.utilization) ? gpu.utilization : null,
          _pct(gpu.utilization),
          "neutral",
        ),
      ),
    );
    const vramPair =
      _number(gpu.vram_total) && gpu.vram_total > 0 && _number(gpu.vram_used);
    card.append(
      meterRow(
        "vram",
        meter(
          vramPair ? gpu.vram_used / gpu.vram_total : null,
          vramPair
            ? `${_pair(gpu.vram_used, gpu.vram_total)} (${_pctOf(gpu.vram_used, gpu.vram_total)})`
            : UNKNOWN,
        ),
      ),
    );
    const parts = [`${_int(gpu.compute_process_count)} compute procs`];
    if (owners && owners.length) {
      parts.push(`owners: ${ownersText(owners)}`);
    }
    if (procEntries && procEntries.length) parts.push(procEntries.join(", "));
    const ownerless =
      (!owners || !owners.length) &&
      (!procEntries || !procEntries.length) &&
      _number(gpu.compute_process_count) &&
      gpu.compute_process_count > 0;
    if (ownerless) parts.push("owner detail unavailable");
    if (_number(gpu.temperature_c)) parts.push(`${gpu.temperature_c.toFixed(0)} °C`);
    if (_number(gpu.power_watts)) parts.push(`${gpu.power_watts.toFixed(1)} W`);
    if (gpu.mig_detected) parts.push("mig");
    card.append(el("div", "gpu-procs", parts.join(" · ")));
    if (gpu.error) card.append(el("div", "gpu-error", `error: ${gpu.error}`));
    return card;
  }

  function renderGpus(gpus, processes) {
    heading("GPUs");
    if (!Array.isArray(gpus) || !gpus.length) {
      note(
        Array.isArray(gpus)
          ? "No current GPU readings"
          : "GPU data unknown for this host",
      );
      return;
    }
    const procsByUuid = gpuProcessMap(processes);
    const ownersByUuid = gpuOwnerMap(processes);
    const grid = el("div", "gpu-grid");
    gpus.forEach((gpu) => {
      if (!gpu || typeof gpu !== "object") return;
      grid.append(
        gpuCard(gpu, procsByUuid.get(gpu.uuid), ownersByUuid.get(gpu.uuid)),
      );
    });
    content.append(grid);
  }

  function renderMeters(latest) {
    const sample = latest || {};
    const panel = el("div", "meters");
    panel.append(meterBox("cpu", sample.cpu_busy, _pct(sample.cpu_busy)));
    panel.append(meterBox("ram", ramFraction(sample), ramLabel(sample, true)));
    panel.append(
      meterBox(
        "disk",
        diskFraction(sample),
        rootDisk(sample.root_total, sample.root_free),
      ),
    );
    heading("Meters");
    content.append(panel);
  }

  // ---- workloads: searchable, GPU-only toggle, capped with show-more ----
  function workloadMatches(process) {
    if (!process || typeof process !== "object") return false;
    if (ui.gpuOnly) {
      const hasGpu =
        (Array.isArray(process.gpu_allocations) && process.gpu_allocations.length) ||
        _number(process.gpu_index);
      if (!hasGpu) return false;
    }
    const query = ui.search.trim().toLowerCase();
    if (!query) return true;
    return [process.pid, process.username, process.name, process.executable]
      .filter((v) => v !== null && v !== undefined && v !== "")
      .some((v) => String(v).toLowerCase().includes(query));
  }

  function applyWorkloadFilter() {
    if (!workloadTbody) return;
    let matched = 0;
    let shown = 0;
    Array.from(workloadTbody.rows).forEach((row) => {
      const process = workloadAll[Number(row.dataset.i)];
      const isMatch = workloadMatches(process);
      if (isMatch) matched += 1;
      const visible = isMatch && shown < ui.processLimit;
      if (visible) shown += 1;
      row.style.display = visible ? "" : "none";
    });
    if (workloadBtn) {
      const collapse = ui.processLimit === Infinity && matched > WORKLOAD_LIMIT;
      const expand = matched > Math.min(ui.processLimit, WORKLOAD_LIMIT);
      workloadBtn.textContent =
        ui.processLimit === Infinity
          ? `Show top ${WORKLOAD_LIMIT}`
          : `Show all (${matched})`;
      workloadBtn.style.display = collapse || expand ? "" : "none";
    }
  }

  function buildWorkloadBar() {
    const bar = el("div", "toolbar");
    const search = el("input", "search");
    search.type = "search";
    search.id = "workload-search";
    search.placeholder = "search workloads";
    search.setAttribute("aria-label", "Search workloads by user, name, or pid");
    search.value = ui.search;
    search.addEventListener("input", () => {
      ui.search = search.value;
      applyWorkloadFilter();
    });
    trackFocus(search, "workloadSearchFocus");
    const label = el("label", "toggle");
    const box = el("input");
    box.type = "checkbox";
    box.checked = ui.gpuOnly;
    box.setAttribute("aria-label", "Show only GPU workloads");
    box.addEventListener("change", () => {
      ui.gpuOnly = box.checked;
      applyWorkloadFilter();
    });
    label.append(box, el("span", null, "GPU only"));
    bar.append(search, label);
    return bar;
  }

  function renderWorkloads(processes) {
    heading("Workloads");
    workloadAll = Array.isArray(processes)
      ? processes.filter((p) => p && typeof p === "object")
      : [];
    if (!workloadAll.length) {
      workloadTbody = null;
      workloadBtn = null;
      note("no current process snapshot");
      return;
    }
    if (!workloadBar) workloadBar = buildWorkloadBar();
    content.append(workloadBar);
    refocusTracked();
    const columns = [
      {label: "pid", num: true},
      {label: "user"},
      {label: "name"},
      {label: "executable"},
      {label: "started", num: true},
      {label: "cpu cores", num: true},
      {label: "sampled rss", num: true},
      {label: "gpu", num: true},
      {label: "gpu vram", num: true},
      {label: "allocations", num: true},
    ];
    const tbody = dataTable(columns, "workloads");
    workloadAll.forEach((process, index) => {
      const tr = rowOf(tbody, columns, [
        num(_int(process.pid), process.pid),
        process.username ? _text(process.username) : `uid ${_int(process.uid)}`,
        _text(process.name),
        _text(process.executable),
        num(_clock(process.create_time), process.create_time),
        num(_cores(process.cpu_cores), process.cpu_cores),
        num(_bytes(process.rss), process.rss),
        num(_number(process.gpu_index) ? _int(process.gpu_index) : DASH, process.gpu_index),
        num(_number(process.vram) ? _bytes(process.vram) : DASH, process.vram),
        num(
          Array.isArray(process.gpu_allocations)
            ? String(process.gpu_allocations.length)
            : "",
          process.gpu_allocations,
        ),
      ]);
      tr.dataset.i = String(index);
    });
    workloadTbody = tbody;
    tbody.applySavedSort();
    content.append(tbody.parentNode);
    const button = el("button", "more-btn", "Show all");
    button.type = "button";
    button.addEventListener("click", () => {
      ui.processLimit = ui.processLimit === Infinity ? WORKLOAD_LIMIT : Infinity;
      applyWorkloadFilter();
    });
    workloadBtn = button;
    content.append(button);
    applyWorkloadFilter();
    note(`bounded current-process snapshot (${_int(workloadAll.length)} rows)`);
  }

  function renderUsers(users, latest) {
    const list = Array.isArray(users) ? users : [];
    const block = detailsBlock("Users", ui.usersOpen, (open) => {
      ui.usersOpen = open;
    });
    if (!list.length) {
      block.append(el("p", "note", "no user data in the latest sample"));
    } else {
      const gpuVramTotal = latest ? latest.gpu_vram_total : null;
      const columns = [
        {label: "user"},
        {label: "cpu cores", num: true},
        {label: "sampled rss", num: true},
        {label: "processes", num: true},
        {label: "gpu procs", num: true},
        {label: "gpu vram", num: true},
      ];
      const tbody = dataTable(columns, "users");
      list.forEach((user) => {
        if (!user || typeof user !== "object") return;
        rowOf(tbody, columns, [
          user.username ? _text(user.username) : `uid ${_int(user.uid)}`,
          num(_cores(user.cpu_cores), user.cpu_cores),
          num(_bytes(user.rss), user.rss),
          num(_int(user.process_count), user.process_count),
          num(_int(user.gpu_process_count), user.gpu_process_count),
          num(
            user.vram
              ? `${_bytes(user.vram)}${gpuVramTotal ? ` (${_pctOf(user.vram, gpuVramTotal)})` : ""}`
              : DASH,
            user.vram,
          ),
        ]);
      });
      tbody.applySavedSort();
      block.append(tbody.parentNode);
    }
    content.append(block);
  }

  function renderDiagnostics(host, latest) {
    const sample = latest || {};
    const block = detailsBlock("Technical diagnostics", ui.diagOpen, (open) => {
      ui.diagOpen = open;
    });
    const processes = `${_int(sample.emitted_processes)} / ${_int(sample.visible_processes)} emitted / visible`;
    block.append(
      kvTable([
        ["helper version", _text(host.helper_version)],
        ["helper path", _text(host.helper_path)],
        ["boot id", _text(sample.boot_id)],
        ["last captured", _clockIso(sample.captured_at)],
        [
          "last received",
          `${_clock(sample.received_at)} (${_age(sample.received_at)} ago)`,
        ],
        ["receive age", num(_age(sample.received_at), _ageRaw(sample.received_at))],
        [
          "capture skew",
          `${_fixed(sample.capture_skew_seconds, 1)} s${
            _number(sample.capture_skew_seconds) &&
            Math.abs(sample.capture_skew_seconds) > CLOCK_DRIFT_S
              ? " (host clock offset)"
              : ""
          }`,
        ],
        ["collection duration", `${_dur(sample.collection_duration_seconds)}`],
        ["observation window", `${_dur(sample.observation_duration_seconds)}`],
        ["poll backoff", `${_dur(host.backoff)}`],
        ["processes", processes],
        ["permission denied", _yes(sample.permission_denied)],
        [
          "truncation",
          [
            sample.counters_truncated ? "counters truncated" : null,
            sample.limits_truncated ? "limits truncated" : null,
          ]
            .filter(Boolean)
            .join(", ") || "no",
        ],
        ["partial", _yes(sample.partial)],
        ["cpu busy", num(_pct(sample.cpu_busy), sample.cpu_busy)],
        [
          "load (1m/5m/15m)",
          `${_cores(sample.load1)} / ${_cores(sample.load5)} / ${_cores(sample.load15)}`,
        ],
        [
          "ram",
          `${_bytes(sample.ram_used)} / ${_bytes(sample.ram_total)} (${_pctOf(sample.ram_used, sample.ram_total)})`,
        ],
        ["root disk", rootDisk(sample.root_total, sample.root_free)],
        ["nvml supported", _yes(sample.nvml_supported)],
        ["nvml error", sample.nvml_error ? token(sample.nvml_error) : DASH],
        ["psutil error", sample.psutil_error ? token(sample.psutil_error) : DASH],
        ["last poll error", host.last_error ? token(host.last_error) : DASH],
      ]),
    );
    content.append(block);
  }

  // ---- idle GPUs page ----
  function idleCounts(doc) {
    if (!doc) return {idle: UNKNOWN, busy: UNKNOWN, stale: UNKNOWN};
    const items = rowsOf(doc).filter((item) => item && typeof item === "object");
    const computed = {idle: 0, busy: 0, stale: 0};
    items.forEach((item) => {
      computed[itemFlag(item)] += 1;
    });
    const summary =
      doc.summary && typeof doc.summary === "object" ? doc.summary : null;
    const pick = (key) =>
      summary && _number(summary[key]) ? String(summary[key]) : String(computed[key]);
    return {idle: pick("idle"), busy: pick("busy"), stale: pick("unknown")};
  }
  function idleRank(avail) {
    return avail === "idle" ? 0 : avail === "busy" ? 1 : 2;
  }
  function idleItems() {
    const items = rowsOf(lastIdleDoc).filter((item) => item && typeof item === "object");
    const minFree = parseFloat(ui.idleMinFree);
    const minBytes = _number(minFree) && minFree > 0 ? minFree * 2 ** 30 : 0;
    const hostQ = ui.idleHost.trim().toLowerCase();
    const modelQ = ui.idleModel.trim().toLowerCase();
    return items
      .filter((item) => {
        const flag = itemFlag(item);
        if (flag === "busy" && !ui.idleBusy) return false;
        if (flag === "stale" && !ui.idleStale) return false;
        if (hostQ && !String(item.target || "").toLowerCase().includes(hostQ)) return false;
        if (modelQ && !String(item.model || "").toLowerCase().includes(modelQ)) return false;
        if (minBytes && !(_number(item.vram_free) && item.vram_free >= minBytes)) return false;
        return true;
      })
      .sort((a, b) => {
        const rank = idleRank(itemFlag(a)) - idleRank(itemFlag(b));
        if (rank) return rank;
        const af = _number(a.vram_free) ? a.vram_free : -1;
        const bf = _number(b.vram_free) ? b.vram_free : -1;
        return bf - af;
      });
  }
  function idleAge(item) {
    if (_number(item.received_at)) return _age(item.received_at);
    return _dur(item.age_seconds);
  }
  function buildIdleFilters() {
    const bar = el("div", "filters");
    const hostInput = el("input");
    hostInput.type = "search";
    hostInput.placeholder = "filter by host";
    hostInput.value = ui.idleHost;
    hostInput.setAttribute("aria-label", "Filter GPUs by host name");
    hostInput.addEventListener("input", () => {
      ui.idleHost = hostInput.value;
      renderIdleTable();
    });
    trackFocus(hostInput, "idleHostFocus");
    const modelInput = el("input");
    modelInput.type = "search";
    modelInput.placeholder = "filter by model";
    modelInput.value = ui.idleModel;
    modelInput.setAttribute("aria-label", "Filter GPUs by model");
    modelInput.addEventListener("input", () => {
      ui.idleModel = modelInput.value;
      renderIdleTable();
    });
    trackFocus(modelInput, "idleModelFocus");
    const freeInput = el("input");
    freeInput.type = "number";
    freeInput.min = "0";
    freeInput.placeholder = "min free GiB";
    freeInput.value = ui.idleMinFree;
    freeInput.setAttribute("aria-label", "Minimum free VRAM in GiB");
    freeInput.addEventListener("input", () => {
      ui.idleMinFree = freeInput.value;
      renderIdleTable();
    });
    trackFocus(freeInput, "idleFreeFocus");
    const busyLabel = el("label", "toggle");
    const busyBox = el("input");
    busyBox.type = "checkbox";
    busyBox.checked = ui.idleBusy;
    busyBox.setAttribute("aria-label", "Include busy GPUs");
    busyBox.addEventListener("change", () => {
      ui.idleBusy = busyBox.checked;
      renderIdleTable();
    });
    busyLabel.append(busyBox, el("span", null, "include busy"));
    const staleLabel = el("label", "toggle");
    const staleBox = el("input");
    staleBox.type = "checkbox";
    staleBox.checked = ui.idleStale;
    staleBox.setAttribute("aria-label", "Include GPUs with a stale flag");
    staleBox.addEventListener("change", () => {
      ui.idleStale = staleBox.checked;
      renderIdleTable();
    });
    staleLabel.append(staleBox, el("span", null, "include stale"));
    bar.append(hostInput, modelInput, freeInput, busyLabel, staleLabel);
    return bar;
  }
  function renderIdleTable() {
    if (!idleWrap || !lastIdleDoc) return;
    idleWrap.replaceChildren();
    const shown = idleItems();
    if (!shown.length) {
      idleWrap.append(el("p", "note", "no GPUs match the current filters"));
      return;
    }
    const columns = [
      {label: "host"},
      {label: "idx", num: true},
      {label: "model"},
      {label: "flag"},
      {label: "owners"},
      {label: "utilization", num: true},
      {label: "vram free", num: true},
      {label: "vram total", num: true},
      {label: "procs", num: true},
      {label: "age", num: true},
    ];
    const tbody = dataTable(columns, "idle-gpus");
    shown.forEach((item) => {
      const flag = itemFlag(item);
      const owners = Array.isArray(item.owners) ? item.owners.filter((o) => typeof o === "string" && o) : [];
      rowOf(tbody, columns, [
        hostLink(item.target),
        num(_int(item.idx), item.idx),
        _text(item.model),
        {node: availabilityToken(flag, item.reason), raw: String(idleRank(flag))},
        owners.length ? {text: ownersText(owners), raw: owners.join(",")} : DASH,
        num(_pct(item.utilization), item.utilization),
        num(_bytes(item.vram_free), item.vram_free),
        num(_bytes(item.vram_total), item.vram_total),
        num(_int(item.compute_process_count), item.compute_process_count),
        num(idleAge(item), _ageRaw(item.received_at)),
      ]);
    });
    tbody.applySavedSort();
    idleWrap.append(tbody.parentNode);
    idleWrap.append(
      el(
        "p",
        "note",
        `showing ${shown.length} of ${rowsOf(lastIdleDoc).length} listed GPUs — observed availability, not a reservation`,
      ),
    );
  }
  function renderIdle(doc) {
    clear();
    lastIdleDoc = doc;
    idleWrap = null;
    const items = rowsOf(doc).filter((item) => item && typeof item === "object");
    const counts = idleCounts(doc);
    const cards = el("div", "cards");
    cards.append(card("idle GPUs", counts.idle, "ok"));
    cards.append(card("busy GPUs", counts.busy, "busy"));
    cards.append(card("stale GPUs", counts.stale, "stale"));
    cards.append(card("GPUs listed", String(items.length), ""));
    content.append(cards);
    if (!doc) {
      note("Idle GPU data is not available yet (requires the new hub API)");
      return;
    }
    if (!idleFilterBar) idleFilterBar = buildIdleFilters();
    content.append(idleFilterBar);
    refocusTracked();
    idleWrap = el("div");
    content.append(idleWrap);
    renderIdleTable();
  }

  // ---- jobs ----
  // Scheduler states are uppercase and can carry a suffix ("CANCELLED by 100056").
  function jobStateToken(state) {
    const text = typeof state === "string" && state ? state : UNKNOWN;
    const cls = text.startsWith("CANCELLED")
      ? "st-muted"
      : {COMPLETED: "st-ok", RUNNING: "st-busy", PENDING: "st-partial", FAILED: "st-bad"}[
          text
        ] || "st-muted";
    const span = el("span", `st ${cls}`, text);
    span.dataset.raw = text;
    return span;
  }

  function renderJobs(rows) {
    clear();
    const list = rowsOf(rows);
    if (!list.length) {
      note("no jobs in range");
      return;
    }
    const columns = [
      {label: "cluster"},
      {label: "job id", num: true},
      {label: "array task"},
      {label: "step"},
      {label: "state"},
      {label: "updated", num: true},
    ];
    const tbody = dataTable(columns, "jobs");
    list.forEach((job) => {
      if (!job || typeof job !== "object") return;
      rowOf(tbody, columns, [
        _text(job.cluster),
        num(_text(job.job_id), job.job_id),
        _text(job.array_task_id),
        _text(job.step_id),
        jobStateToken(job.state),
        num(_clock(job.updated_at), job.updated_at),
      ]);
    });
    tbody.applySavedSort();
    content.append(tbody.parentNode);
    note(`showing ${list.length} jobs — bounded, latest first`);
  }

  // ---- hub status ----
  function renderHubStatus(doc) {
    clear();
    const hub = doc && typeof doc === "object" && !Array.isArray(doc) ? doc : {};
    const rate = _number(hub.recent_error_rate)
      ? ` (${(hub.recent_error_rate * 100).toFixed(1)}% errors)`
      : "";
    content.append(
      kvTable([
        ["status", token(hub.status), ""],
        [
          "polling",
          hub.polling_enabled ? "enabled" : el("span", "st st-stale", "polling_disabled"),
        ],
        ["in-flight polls", _int(hub.in_flight)],
        ["inventory age", _dur(hub.inventory_age_seconds)],
        ["inventory error", hub.inventory_error ? token(hub.inventory_error) : DASH],
        [
          "recent polls (1h)",
          hub.recent_polls_total
            ? `${_int(hub.recent_polls_total)} polls, ${_int(hub.recent_poll_errors)} errors${rate}`
            : "no polls",
        ],
        ["recent avg latency", _dur(hub.recent_avg_latency_seconds)],
        [
          "hub cpu",
          meter(
            _number(hub.hub_cpu_percent) ? hub.hub_cpu_percent / 100 : null,
            _number(hub.hub_cpu_percent) ? `${hub.hub_cpu_percent.toFixed(2)}%` : UNKNOWN,
          ),
        ],
        [
          "hub budget",
          hub.hub_overloaded
            ? el("span", "st st-bad", "overloaded")
            : el("span", "st st-ok", "within budget"),
        ],
        ["hub rss", _bytes(hub.hub_rss_bytes)],
        ["database size", _bytes(hub.database_size_bytes)],
        ["wal size", _bytes(hub.wal_size_bytes)],
        ["free disk", _bytes(hub.free_disk_bytes)],
        ["retention", `${_int(hub.retention_days)} days`],
        ["backup", hub.backup_status ? token(hub.backup_status) : DASH],
        ["uptime", _dur(hub.uptime_seconds)],
        ["version", _text(hub.version)],
      ]),
    );
  }

  // ---- charts: one tiny local SVG renderer, one request per chart group ----
  const svgNS = "http://www.w3.org/2000/svg";
  const W = 640;
  const H = 150;
  const PAD = {l: 46, r: 12, t: 8, b: 18};
  const PALETTE = ["c1", "c2", "c3", "c4", "c5"];
  const CHART_COLORS = {cpu: "c1", ram: "c2", disk: "c3"};
  const X_LABELS = 4;
  const SPARK = {w: 64, h: 22};
  const CHARTS = [
    ["cpu", "cpu"],
    ["ram", "ram used"],
    ["disk", "root disk used"],
    ["gpu_util", "gpu utilization"],
    ["gpu_vram", "gpu vram used"],
  ];
  function svg(tag, cls) {
    const node = document.createElementNS(svgNS, tag);
    if (cls) node.setAttribute("class", cls);
    return node;
  }
  function chartsUrl() {
    return chartHours ? `${chartsEndpoint}?hours=${chartHours}` : chartsEndpoint;
  }
  function seriesColor(chart, index) {
    return CHART_COLORS[chart] || PALETTE[index % PALETTE.length];
  }
  function unitText(unit, value) {
    return unit === "percent" ? _pct(value) : _fixed(value, 2);
  }
  function yTicks(unit, chartSeries) {
    if (unit === "percent") {
      return [[0, "0%"], [0.5, "50%"], [1, "100%"]];
    }
    let min = Infinity;
    let max = -Infinity;
    chartSeries.forEach((item) => {
      item.points.forEach(([, v]) => {
        if (v < min) min = v;
        if (v > max) max = v;
      });
    });
    if (!isFinite(min) || max <= min) {
      return [[min, _fixed(min, 2)]];
    }
    const mid = (min + max) / 2;
    return [[min, _fixed(min, 2)], [mid, _fixed(mid, 2)], [max, _fixed(max, 2)]];
  }
  function nearestPoint(points, time) {
    let best = null;
    let distance = Infinity;
    for (const point of points) {
      const gap = Math.abs(point[0] - time);
      if (gap < distance) [distance, best] = [gap, point];
    }
    return best;
  }

  function renderRanges() {
    const wrap = el("div", "ranges");
    RANGES.forEach(([label, hours]) => {
      const button = el("button", null, label);
      button.type = "button";
      button.setAttribute("aria-pressed", String(hours === chartHours));
      button.addEventListener("click", () => {
        if (hours === chartHours) return;
        chartHours = hours;
        refreshCharts();
      });
      wrap.append(button);
    });
    return wrap;
  }

  let chartPanel = null;
  let chartPanelSource = null;
  function renderCharts() {
    heading("History");
    if (chartPanel && chartPanelSource === lastCharts) {
      content.append(renderRanges(), chartPanel);
      return;
    }
    content.append(renderRanges());
    if (!chartsLoaded) {
      note("loading history…");
      return;
    }
    const series = Array.isArray(lastCharts && lastCharts.series) ? lastCharts.series : [];
    const groups = new Map();
    series.forEach((item) => {
      if (!item || !Array.isArray(item.points) || !item.points.length) return;
      const key = typeof item.chart === "string" ? item.chart : "unknown";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    if (!groups.size) {
      note("no history in range yet");
      return;
    }
    chartPanel = el("div", "chart-grid");
    chartPanelSource = lastCharts;
    CHARTS.forEach(([key, title]) => {
      const chartSeries = groups.get(key);
      if (!chartSeries) return;
      chartPanel.append(chartFigure(title, chartSeries));
    });
    content.append(chartPanel);
  }

  function appendSeries(root, coords, color, lineWidth, baseline) {
    if (coords.length > 1) {
      const first = coords[0].slice(1);
      const lastX = coords[coords.length - 1].slice(1).split(" ")[0];
      const area = svg("path", `area ${color}`);
      area.setAttribute(
        "d",
        `M${first}${coords.slice(1).join("")}L${lastX} ${baseline}L${first.split(" ")[0]} ${baseline}Z`
      );
      root.append(area);
    }
    const line = svg("path", `line ${color}`);
    line.setAttribute("stroke-width", lineWidth);
    line.setAttribute("d", coords.join(""));
    root.append(line);
  }

  function chartFigure(title, chartSeries) {
    const fig = el("figure", "chart");
    const caption = el(
      "figcaption",
      null,
      chartSeries.length > 1 ? `${title} ` : title,
    );
    if (chartSeries.length > 1) {
      chartSeries.forEach((item, index) => {
        const label = el("span", "series", item.label);
        label.prepend(el("span", `swatch ${seriesColor(item.chart, index)}`));
        caption.append(label);
      });
    }
    fig.append(caption);
    let x0 = Infinity;
    let x1 = -Infinity;
    chartSeries.forEach((item) => {
      item.points.forEach(([t]) => {
        if (t < x0) x0 = t;
        if (t > x1) x1 = t;
      });
    });
    if (!isFinite(x0) || x1 <= x0) {
      fig.append(el("p", "note", "not enough points"));
      return fig;
    }
    const unit = chartSeries.some((item) => item.unit === "percent")
      ? "percent"
      : "value";
    const plot = {w: W - PAD.l - PAD.r, h: H - PAD.t - PAD.b};
    const root = svg("svg");
    root.setAttribute("viewBox", `0 0 ${W} ${H}`);
    root.setAttribute("width", "100%");
    root.setAttribute("role", "img");
    root.setAttribute("aria-label", `${title} chart`);
    const clampedY = (v) => {
      const value = unit === "percent" ? Math.min(1, Math.max(0, v)) : v;
      return Math.min(PAD.t + plot.h, Math.max(PAD.t, PAD.t + (1 - value) * plot.h));
    };
    yTicks(unit, chartSeries).forEach(([value, label]) => {
      const y = clampedY(value);
      const grid = svg("line", "grid");
      grid.setAttribute("x1", PAD.l);
      grid.setAttribute("x2", W - PAD.r);
      grid.setAttribute("y1", y);
      grid.setAttribute("y2", y);
      const tick = svg("text");
      tick.textContent = label;
      tick.setAttribute("x", PAD.l - 4);
      tick.setAttribute("y", y + 3);
      tick.setAttribute("text-anchor", "end");
      root.append(grid, tick);
    });
    const span = x1 - x0;
    for (let index = 0; index < X_LABELS; index += 1) {
      const fraction = index / (X_LABELS - 1);
      const tick = svg("text");
      tick.textContent = axisTime(x0 + span * fraction, span);
      tick.setAttribute("x", PAD.l + plot.w * fraction);
      tick.setAttribute("y", H - 3);
      tick.setAttribute("text-anchor", index === 0 ? "start" : index === X_LABELS - 1 ? "end" : "middle");
      root.append(tick);
    }
    chartSeries.forEach((item, index) => {
      const coords = item.points.map(([t, v], point) => {
        const x = PAD.l + ((t - x0) / span) * plot.w;
        return `${point ? "L" : "M"}${x.toFixed(1)} ${clampedY(v).toFixed(1)}`;
      });
      appendSeries(
        root,
        coords,
        seriesColor(item.chart, index),
        "2",
        (PAD.t + plot.h).toFixed(1)
      );
    });
    const crosshair = svg("line", "crosshair");
    crosshair.setAttribute("y1", PAD.t);
    crosshair.setAttribute("y2", PAD.t + plot.h);
    crosshair.setAttribute("display", "none");
    root.append(crosshair);
    const overlay = svg("rect", "overlay");
    overlay.setAttribute("x", PAD.l);
    overlay.setAttribute("y", PAD.t);
    overlay.setAttribute("width", plot.w);
    overlay.setAttribute("height", plot.h);
    overlay.addEventListener("mousemove", (event) => {
      const rect = root.getBoundingClientRect();
      if (!rect.width) return;
      const viewX = ((event.clientX - rect.left) / rect.width) * W;
      const fraction = Math.min(1, Math.max(0, (viewX - PAD.l) / plot.w));
      const time = x0 + fraction * span;
      const x = PAD.l + fraction * plot.w;
      crosshair.setAttribute("x1", x);
      crosshair.setAttribute("x2", x);
      crosshair.removeAttribute("display");
      tip.replaceChildren();
      tip.append(el("span", "t", new Date(time * 1000).toLocaleTimeString()));
      chartSeries.forEach((item) => {
        const point = nearestPoint(item.points, time);
        if (point) {
          tip.append(el("div", null, `${item.label} ${unitText(unit, point[1])}`));
        }
      });
      tip.classList.add("on");
    });
    overlay.addEventListener("mouseleave", () => {
      crosshair.setAttribute("display", "none");
      tip.classList.remove("on");
    });
    root.append(overlay);
    const tip = el("div", "tip");
    fig.append(root, tip);
    return fig;
  }

  function axisTime(epoch, spanSeconds) {
    const date = new Date(epoch * 1000);
    return spanSeconds > 48 * 3600
      ? date.toLocaleDateString(undefined, {month: "short", day: "numeric"})
      : date.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"});
  }

  // ---- overview sparklines: one small inline SVG per host row ----
  function sparkMap(doc) {
    const map = new Map();
    const series = doc && Array.isArray(doc.series) ? doc.series : [];
    series.forEach((item) => {
      if (item && typeof item.target === "string" && Array.isArray(item.points)) {
        map.set(item.target, item.points);
      }
    });
    return map;
  }

  function sparkline(points) {
    const root = svg("svg", "spark");
    root.setAttribute("viewBox", `0 0 ${SPARK.w} ${SPARK.h}`);
    root.setAttribute("width", SPARK.w);
    root.setAttribute("height", SPARK.h);
    root.setAttribute("role", "img");
    root.setAttribute("aria-label", "cpu history");
    const sampled = (Array.isArray(points) ? points : []).filter(
      (pair) => Array.isArray(pair) && _number(pair[0]) && _number(pair[1])
    );
    let x0 = Infinity;
    let x1 = -Infinity;
    sampled.forEach(([t]) => {
      if (t < x0) x0 = t;
      if (t > x1) x1 = t;
    });
    if (sampled.length < 2 || x1 <= x0) return root;
    const coords = sampled.map(([t, v], point) => {
      const x = 1 + ((t - x0) / (x1 - x0)) * (SPARK.w - 2);
      const y = 2 + (1 - Math.min(1, Math.max(0, v))) * (SPARK.h - 4);
      return `${point ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    });
    appendSeries(root, coords, "c1", "1.5", String(SPARK.h - 2));
    return root;
  }

  // ---- rendering dispatch and bounded polling ----
  function renderAll() {
    if (page === "host") renderHost(lastData);
    else if (page === "overview") renderOverview(lastData, lastIdleDoc);
    else if (page === "idle-gpus") renderIdle(lastIdleDoc);
    else if (page === "jobs") renderJobs(lastData);
    else renderHubStatus(lastData);
  }

  function sampleAgeText() {
    let newest = null;
    const track = (value) => {
      if (_number(value) && (newest === null || value > newest)) newest = value;
    };
    if (page === "host") {
      const items = lastData && Array.isArray(lastData.items) ? lastData.items : [];
      if (items[0]) track(items[0].received_at);
    } else if (page === "overview") {
      rowsOf(lastData).forEach((row) => track(row && row.last_received));
    } else if (page === "jobs") {
      rowsOf(lastData).forEach((row) => track(row && row.updated_at));
    } else if (page === "idle-gpus") {
      rowsOf(lastIdleDoc).forEach((row) => track(row && row.received_at));
    }
    return newest === null ? "" : `${_dur(Date.now() / 1000 - newest)} ago`;
  }
  function setStatusOk() {
    // The fetch time is the browser's; the sample age is the telemetry's.
    const age = sampleAgeText();
    const fetched = new Date().toLocaleTimeString();
    status.textContent = age ? `sample ${age} · fetched ${fetched}` : `fetched ${fetched}`;
    status.className = "";
  }

  async function fetchJson(url) {
    const controller =
      typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS) : null;
    try {
      const response = await fetch(url, controller ? {signal: controller.signal} : undefined);
      if (!response.ok) throw Error(`request failed: ${response.status}`);
      return await response.json();
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  let refreshInFlight = false;
  let rangeInFlight = false;
  let missedWhileHidden = false;
  let missedChartsWhileHidden = false;
  let lastData = null;
  let lastSparks = null;
  let lastIdleDoc = null;
  let lastCharts = null;
  let chartsLoaded = false;
  let workloadBar = null;
  let workloadTbody = null;
  let workloadBtn = null;
  let workloadAll = [];
  let idleFilterBar = null;
  let idleWrap = null;

  async function refresh() {
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      const idlePromise =
        page === "overview" ? fetchJson(IDLE_ENDPOINT).catch(() => null) : null;
      let data = null;
      if (page === "idle-gpus") {
        // A missing idle API is an integration state, not a hard failure.
        try {
          data = await fetchJson(endpoint);
        } catch (_error) {
          data = null;
        }
      } else {
        data = await fetchJson(endpoint);
      }
      const idle = idlePromise ? await idlePromise : null;
      if (idle) lastIdleDoc = idle;
      if (page === "idle-gpus") lastIdleDoc = data;
      lastData = data;
      if (!document.hidden) {
        if (page === "idle-gpus" && data === null) {
          renderIdle(null);
          status.textContent = "Idle GPU API not available yet";
          status.className = "error";
        } else {
          renderAll();
          setStatusOk();
        }
      }
      missedWhileHidden = false;
    } catch (_error) {
      if (!document.hidden) {
        status.textContent = "Unable to load dashboard data";
        status.className = "error";
      }
    } finally {
      refreshInFlight = false;
    }
  }

  async function refreshCharts() {
    // Independent of the main refresh: a slow chart group can never delay
    // current data, and the last good chart group is kept on failure.
    if ((!chartsEndpoint && !sparkEndpoint) || rangeInFlight) return;
    rangeInFlight = true;
    try {
      if (sparkEndpoint) {
        lastSparks = await fetchJson(sparkEndpoint);
        if (!document.hidden && lastData) renderAll();
        return;
      }
      const requestedHours = chartHours;
      const charts = await fetchJson(chartsUrl());
      if (requestedHours !== chartHours) return;
      chartsLoaded = true;
      lastCharts = charts;
      if (!document.hidden && lastData) {
        renderHost(lastData);
        setStatusOk();
      }
    } catch (_error) {
      /* keep the last good charts */
    } finally {
      rangeInFlight = false;
    }
  }

  document.getElementById("menu").addEventListener("click", () => {
    const open = document.getElementById("nav").classList.toggle("open");
    document.getElementById("menu").setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("visibilitychange", () => {
    // Pause while hidden; on return, refresh immediately (at most once each).
    if (document.hidden) return;
    if (missedWhileHidden) {
      missedWhileHidden = false;
      refresh();
    }
    if (missedChartsWhileHidden) {
      missedChartsWhileHidden = false;
      refreshCharts();
    }
  });
  refresh();
  setInterval(() => {
    if (document.hidden) missedWhileHidden = true;
    else refresh();
  }, REFRESH_MS[page] || 60000);
  if (chartsEndpoint || sparkEndpoint) {
    refreshCharts();
    setInterval(() => {
      if (document.hidden) missedChartsWhileHidden = true;
      else refreshCharts();
    }, HISTORY_MS);
  }
  if (page !== "overview") {
    refreshSidebar();
    setInterval(() => {
      if (!document.hidden) refreshSidebar();
    }, SIDEBAR_MS);
  }
})();
