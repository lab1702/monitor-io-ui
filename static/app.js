/* NetMonitor dashboard front end. Talks to server.py's /api/* endpoints. */
"use strict";

const $ = (s) => document.querySelector(s);
const API = "api/";
const STATUS_MS = 10000;
const HISTORY_MS = 60000;
const SLOW_MS = 300000;

const state = {
  status: null,
  targets: [],              // [{name, kind}] in device order; colour follows index
  preset: 86400,            // seconds, "all", or null for a custom range
  start: 0, end: 0,
  firstTs: null,            // earliest sample available (for "All")
  metric: "avg",
  logScale: false,
  hidden: new Set(),
  series: null,
  incidents: [],
  incKind: "",
  allIncidents: [],
  daily: [],
  evOnly: "", evOffset: 0, evLimit: 50, evTotal: 0,
  charts: {},
};

// ------------------------------------------------------------------ utils --

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let palette = null;  // series colours; rerenderAll clears it when the theme may have changed
const colorOf = (name) => {
  palette ??= Array.from({ length: 8 }, (_, i) => cssVar(`--s${i + 1}`));
  const i = state.targets.findIndex((t) => t.name === name);
  return palette[Math.max(i, 0) % 8];
};
const isHost = (name) => state.targets.find((t) => t.name === name)?.kind === "host";
const alpha = (hex, a) => {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
};

const dtf = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dtfShort = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const dfDay = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
const fmtTime = (ts) => (ts ? dtf.format(new Date(ts * 1000)) : "—");
const fmtTimeShort = (ts) => (ts ? dtfShort.format(new Date(ts * 1000)) : "—");

function fmtDur(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60 ? (sec % 60) + "s" : ""}`.trim();
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ${Math.round((sec % 3600) / 60)}m`;
  return `${Math.floor(sec / 86400)}d ${Math.round((sec % 86400) / 3600)}h`;
}
const fmtAgo = (ts, now) => (ts ? fmtDur(Math.max(0, (now || Date.now() / 1000) - ts)) + " ago" : "never");
const fmtMs = (v) => (v == null ? "—" : v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : v.toFixed(0));
const fmtPct = (v, d = 0) => (v == null ? "—" : `${v.toFixed(d)}%`);

function toLocalInput(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function api(path, params) {
  const qs = params ? "?" + new URLSearchParams(params) : "";
  const r = await fetch(API + path + qs, { cache: "no-store" });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));  // e.g. a proxy's HTML error page
    throw new Error(j.error || r.statusText || `HTTP ${r.status}`);
  }
  return r.json();
}

const STATUS = {
  good: { ico: "✓", label: "OK" },
  warning: { ico: "!", label: "Degraded" },
  serious: { ico: "!", label: "Impaired" },
  critical: { ico: "✕", label: "Down" },
  unknown: { ico: "?", label: "Unknown" },
};
const ico = (st) => h("span", { class: "ico", "aria-hidden": "true" }, STATUS[st]?.ico || "?");
const badge = (st, label) => h("span", { class: `badge st-${st}` }, ico(st), label || STATUS[st].label);

// ------------------------------------------------------------ live status --

function targetHealth(t) {
  if (!t) return "unknown";
  if (t.loss == null) return "unknown";
  if (t.loss >= 100) return "critical";
  if (t.loss >= 30) return "serious";
  if (t.loss > 0) return "warning";
  return "good";
}

async function loadStatus() {
  try {
    const s = await api("status");
    state.status = s;
    if (!state.targets.length || s.targets.length !== state.targets.length) {
      state.targets = s.targets;
      renderChips();
    }
    renderStatus();
  } catch (e) {
    setFreshness("critical", "Dashboard server unreachable");
    showBanner(`Cannot reach the dashboard server: ${e.message}`);
  }
}

function setFreshness(st, text) {
  const p = $("#freshness");
  p.className = `pill st-${st}`;
  p.querySelector(".txt").textContent = text;
}
function showBanner(msg) {
  const b = $("#banner");
  b.hidden = !msg;
  b.textContent = msg || "";
}

function renderStatus() {
  const s = state.status;
  $("#device-url").textContent = s.device;
  const last = s.last;
  const now = s.now;
  const interval = s.interval || 9;

  if (s.syncing) {
    // cached data can be hours old until the first sync after a restart finishes
    setFreshness("warning", "Syncing…");
    showBanner("");
  } else if (s.device_error) {
    setFreshness("critical", "Device unreachable");
    showBanner(`Device ${s.device} is not responding (${s.device_error}). Showing cached data from ${fmtAgo(s.device_ok_at, now)}.`);
  } else if (!last) {
    setFreshness("unknown", "No data");
    showBanner("");
  } else {
    const age = now - last.ts;
    if (age < interval * 3 + 30) setFreshness("good", "Live");
    else if (age < 300) setFreshness("warning", `Delayed ${fmtDur(age)}`);
    else setFreshness("critical", `Stale ${fmtDur(age)}`);
    showBanner(age >= 300 ? `No new samples for ${fmtDur(age)}. The monitor may have stopped logging.` : "");
  }
  $("#last-sample").textContent = (last ? `Last sample ${fmtTime(last.ts)} (${fmtAgo(last.ts, now)}), every ~${Math.round(interval)}s` : "") +
    (s.ignored_files?.length ? ` · Ignoring ${s.ignored_files.join(", ")} (implausible date; device clock was wrong)` : "");

  // -- layered health tiles: gateway -> internet -> DNS -> monitor
  const gw = state.targets.filter((t) => t.kind === "gateway");
  const ext = state.targets.filter((t) => t.kind === "ip");
  const hosts = state.targets.filter((t) => t.kind === "host");
  const T = last ? last.targets : {};
  const tiles = [];

  const gwT = gw.map((t) => T[t.name]).filter(Boolean);
  let gst = "unknown", gtext = "No data", gsub = "";
  if (gwT.length) {
    const worst = Math.max(...gwT.map((x) => x.loss ?? 0));
    gst = worst >= 100 ? "critical" : worst > 0 ? "warning" : "good";
    gtext = worst >= 100 ? "Unreachable" : worst > 0 ? `${worst}% loss` : "Reachable";
    gsub = gw.map((t) => `${t.name}: ${fmtMs(T[t.name]?.avg)} ms`).join(" · ");
  }
  tiles.push(["Local network", gst, gtext, gsub]);

  const extT = ext.map((t) => T[t.name]).filter(Boolean);
  let ist = "unknown", itext = "No data", isub = "";
  if (extT.length) {
    const losses = extT.map((x) => x.loss ?? 0);
    const avgs = extT.map((x) => x.avg).filter((v) => v != null);
    const meanAvg = avgs.length ? avgs.reduce((a, b) => a + b, 0) / avgs.length : null;
    if (last.lan_down) { ist = "critical"; itext = "Unknown (gateway down)"; }
    else if (losses.every((v) => v >= 100)) { ist = "critical"; itext = "Down"; }
    else if (Math.max(...losses) > 0) { ist = "warning"; itext = `Packet loss ${Math.max(...losses)}%`; }
    else if (meanAvg != null && meanAvg > 100) { ist = "warning"; itext = "High latency"; }
    else { ist = "good"; itext = "Up"; }
    isub = ext.map((t) => `${t.name}: ${fmtMs(T[t.name]?.avg)} ms`).join(" · ");
  }
  tiles.push(["Internet", ist, itext, isub]);

  let dst = "unknown", dtext = "No data", dsub = "";
  if (last) {
    const missing = last.missing || [];
    if (last.dns_fail || missing.length) {
      dst = "critical";
      dtext = missing.length === hosts.length ? "Failing" : "Partial failure";
      dsub = `Not resolving: ${missing.join(", ") || "hostname targets"}` +
        (s.dns_streak_start ? ` · since ${fmtTimeShort(s.dns_streak_start)}` : "");
    } else {
      dst = "good";
      dtext = "Resolving";
      dsub = s.last_dns_failure ? `Last failure ${fmtAgo(s.last_dns_failure.ts, now)}` : "No failures on record";
    }
  }
  tiles.push(["DNS", dst, dtext, dsub]);

  const l24 = s.last24 || {};
  const okPct = l24.samples ? 100 * (1 - l24.lossy_rows / l24.samples) : null;
  tiles.push(["Last 24 hours", l24.dns_rows || l24.wan_down_rows ? "warning" : "good",
    okPct == null ? "—" : `${okPct.toFixed(1)}% clean cycles`,
    `${(l24.samples || 0).toLocaleString()} cycles · ${l24.lossy_rows || 0} with loss · ${l24.dns_rows || 0} DNS failures · ${l24.wan_down_rows || 0} offline`]);

  $("#health").replaceChildren(...tiles.map(([label, st, text, sub]) =>
    h("div", { class: `card tile st-${st}` }, ico(st),
      h("div", {}, h("div", { class: "label" }, label), h("div", { class: "state" }, text), h("div", { class: "sub" }, sub)))));

  // -- per-target cards
  const logBy = {};
  for (const l of s.log?.lines || []) if (l.target) logBy[l.target] = l;
  $("#targets").replaceChildren(...state.targets.map((t) => {
    const v = T[t.name];
    const missing = last && (last.missing || []).includes(t.name);
    const st = missing ? "critical" : targetHealth(v);
    const kind = { gateway: "Gateway", ip: "Public IP", host: "Hostname" }[t.kind];
    const card = h("div", { class: "card tcard" },
      h("div", { class: "name" }, h("span", { class: "swatch", style: `background:${colorOf(t.name)}` }), t.name),
      h("div", { class: "kind" }, kind),
      h("span", { class: "badge-pos" }, badge(st, missing ? "DNS fail" : st === "good" ? "OK" : st === "unknown" ? "No data" : `${v.loss}% loss`)),
      missing
        ? h("div", { class: "big" }, "—", h("small", {}, " not resolved"))
        : h("div", { class: "big" }, fmtMs(v?.avg), h("small", {}, " ms avg")),
      h("div", { class: "meta" },
        h("span", {}, `min ${fmtMs(v?.min)}`), h("span", {}, `max ${fmtMs(v?.max)}`), h("span", {}, `loss ${fmtPct(v?.loss)}`)),
      sparkline(t.name));
    return card;
  }));

  renderDnsNow();
}

// recent 30 minutes of raw samples for the sparklines
let recent = null;
async function loadRecent() {
  const now = Date.now() / 1000;
  try {
    recent = await api("series", { start: now - 1800, end: now + 60, points: 400 });
    if (state.status) renderStatus();
  } catch (_) { /* status banner covers it */ }
}

function sparkline(name) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 100 36");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-label", `${name} latency, last 30 minutes`);
  const s = recent?.targets?.[name];
  if (!s || !recent.t.length) return svg;
  const t0 = recent.start, t1 = recent.end;
  const vals = s.avg.filter((v) => v != null);
  if (!vals.length) return svg;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.1 || 1;
  const y = (v) => 34 - ((v - lo + pad) / (hi - lo + 2 * pad)) * 32;
  let d = "", pen = false;
  recent.t.forEach((ts, i) => {
    const v = s.avg[i];
    if (v == null) { pen = false; return; }
    const x = ((ts - t0) / (t1 - t0)) * 100;
    d += `${pen ? "L" : "M"}${x.toFixed(2)},${y(v).toFixed(2)}`;
    pen = true;
  });
  const path = document.createElementNS(svgNS, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", colorOf(name));
  path.setAttribute("stroke-width", "1.5");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(path);
  // lost pings as ticks on the baseline
  recent.t.forEach((ts, i) => {
    const l = s.loss[i];
    if (l == null || l === 0) return;
    const r = document.createElementNS(svgNS, "rect");
    r.setAttribute("x", (((ts - t0) / (t1 - t0)) * 100 - 0.4).toFixed(2));
    r.setAttribute("y", "30"); r.setAttribute("width", "0.8"); r.setAttribute("height", "6");
    r.setAttribute("fill", cssVar(l >= 100 ? "--critical" : "--serious"));
    svg.append(r);
  });
  return svg;
}

// --------------------------------------------------------------------- DNS --

function renderDnsNow() {
  const s = state.status;
  if (!s) return;
  const last = s.last;
  const failing = last && (last.dns_fail || (last.missing || []).length);
  const dnsInc = state.allIncidents.filter((i) => i.kind === "dns");
  const since30 = s.now - 30 * 86400;
  const unparsed = (s.log?.lines || []).filter((l) => !l.target);
  const st = !last ? "unknown" : failing ? "critical" : "good";
  const kv = [
    ["Now", !last ? "No data" : failing ? `Not resolving ${(last.missing || []).join(", ") || "hostnames"}` : "All hostname targets resolving"],
    ["Failing since", failing && s.dns_streak_start ? `${fmtTime(s.dns_streak_start)} (${fmtAgo(s.dns_streak_start, s.now)})` : null],
    ["Last failure", s.last_dns_failure ? `${fmtTime(s.last_dns_failure.ts)} (${fmtAgo(s.last_dns_failure.ts, s.now)})` : "None recorded"],
    ["Last 24 h", `${s.last24?.dns_rows ?? 0} probe cycles with DNS failure`],
    ["Last 30 days", `${dnsInc.filter((i) => i.start >= since30).length} incidents · ` +
      `${dnsInc.filter((i) => i.start >= since30 && i.cause === "dns").length} DNS-only`],
    ["Log check", s.log_missing?.length ? `Latest results log has no line for ${s.log_missing.join(", ")}` : "Latest results log lists every target"],
  ].filter(([, v]) => v != null);
  $("#dns-now").replaceChildren(h("div", {},
    h("h3", {}, "Resolver status"),
    h("div", { class: `big st-${st}` }, ico(st), !last ? "Unknown" : failing ? "DNS failing" : "DNS healthy"),
    h("dl", { class: "kv" }, kv.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    unparsed.length ? h("div", {}, h("h3", { style: "margin-top:10px" }, "Unrecognised lines in latest results log"),
      h("pre", { class: "log" }, unparsed.map((l) => l.raw).join("\n"))) : null,
  ));
}

function renderDnsTable() {
  const rows = state.allIncidents.filter((i) => i.kind === "dns");
  $("#dns-inc-note").textContent = rows.length ? `${rows.length} on record` : "";
  const causeLabel = {
    dns: ["serious", "DNS-only (internet reachable)"],
    wan: ["critical", "Internet outage"],
    lan: ["critical", "Gateway / LAN outage"],
  };
  const tb = $("#dns-table tbody");
  if (!rows.length) {
    tb.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "empty" }, "No DNS failures recorded.")));
    return;
  }
  tb.replaceChildren(...rows.map((i) => {
    const [st, label] = causeLabel[i.cause] || ["unknown", i.cause];
    return h("tr", { class: "click", title: "Show in History", onclick: () => zoomTo(i.start, i.end) },
      h("td", {}, fmtTime(i.start), i.ongoing ? h("span", {}, " ", badge("critical", "Ongoing")) : null),
      h("td", {}, fmtDur(i.duration)),
      h("td", { class: "num" }, i.rows),
      h("td", {}, (i.hosts || []).join(", ")),
      h("td", {}, badge(st, label)));
  }));
}

function renderDnsDaily() {
  const days = state.daily;
  const el = $("#dns-daily-chart");
  const x = days.map((d) => new Date(d.date + "T12:00:00").getTime() / 1000);
  const y = days.map((d) => d.dns_rows);
  const crit = cssVar("--critical");
  destroyChart("dnsDaily");
  if (!days.length) return;
  state.charts.dnsDaily = new uPlot({
    width: el.clientWidth || 600, height: 130,
    padding: [8, 8, 0, 0],
    legend: { show: false },
    cursor: { drag: { x: false, y: false }, points: { show: false } },
    scales: { x: { time: true, range: (u, mn, mx) => [mn - 43200, mx + 43200] }, y: { range: (u, mn, mx) => [0, Math.max(mx, 4)] } },
    axes: [axis({ space: 60, values: (u, ts) => ts.map((t) => dfDay.format(new Date(t * 1000)).replace(/^\w+,?\s/, "")) }),
      axis({ size: 40, values: (u, v) => v.map((n) => (Number.isInteger(n) ? n : "")) })],
    series: [{}, { label: "DNS failures", stroke: crit, fill: crit, width: 0, paths: uPlot.paths.bars({ size: [0.6, 24], radius: 0.15 }), points: { show: false } }],
    plugins: [tooltipPlugin((u, i) => ({
      title: dfDay.format(new Date(u.data[0][i] * 1000)),
      rows: [[crit, `${u.data[1][i]}`, "cycles with DNS failure"],
        [null, fmtDur(days[i].dns_seconds), "affected time"]],
    }))],
  }, [x, y], el);
}

// --------------------------------------------------------------- history ---

function axis(extra) {
  return Object.assign({
    stroke: cssVar("--muted"),
    grid: { stroke: cssVar("--grid"), width: 1 },
    ticks: { stroke: cssVar("--axis"), width: 1, size: 4 },
    font: "11px system-ui, sans-serif",
  }, extra);
}

function destroyChart(k) {
  if (state.charts[k]) { state.charts[k].destroy(); delete state.charts[k]; }
}

function currentRange() {
  const now = Date.now() / 1000;
  if (state.preset === "all") return [state.firstTs || now - 30 * 86400, now + 30];
  if (state.preset) return [now - state.preset, now + 30];
  return [state.start, state.end];
}

function setPreset(p) {
  state.preset = p;
  document.querySelectorAll("#presets button").forEach((b) => b.classList.toggle("on", String(p) === b.dataset.p));
  state.evOffset = 0;
  loadHistory();
}

function setRange(start, end) {
  state.preset = null;
  state.start = start;
  state.end = end;
  document.querySelectorAll("#presets button").forEach((b) => b.classList.remove("on"));
  state.evOffset = 0;
  loadHistory();
}

// show an incident with some context either side
function zoomTo(start, end) {
  const pad = Math.max(600, (end - start) * 0.25);
  setRange(start - pad, end + pad);
  $("#hist-cards").scrollIntoView({ behavior: "smooth", block: "start" });
}

let historySeq = 0;
async function loadHistory() {
  const seq = ++historySeq;
  const [start, end] = currentRange();
  $("#from").value = toLocalInput(start);
  $("#to").value = toLocalInput(Math.min(end, Date.now() / 1000));
  $("#range-label").textContent = `${fmtTimeShort(start)} → ${fmtTimeShort(Math.min(end, Date.now() / 1000))}`;
  document.body.classList.add("loading");
  try {
    const width = $("#lat-chart").clientWidth || 900;
    const [series, inc] = await Promise.all([
      api("series", { start, end, points: Math.max(200, Math.min(1500, Math.round(width))) }),
      api("incidents", { start, end }),
    ]);
    if (seq !== historySeq) return;  // a newer range was requested meanwhile
    state.series = series;
    state.incidents = inc.incidents;
    renderHistory();
    renderIncidents();
    loadEvents();
  } catch (e) {
    if (seq === historySeq) showBanner(`History failed to load: ${e.message}`);
  } finally {
    if (seq === historySeq) document.body.classList.remove("loading");
  }
}

function bandPlugin() {
  return {
    hooks: {
      drawClear: (u) => {
        const ctx = u.ctx;
        const [x0, x1] = [u.scales.x.min, u.scales.x.max];
        const colors = {
          wan_down: alpha(cssVar("--critical"), 0.16), lan_down: alpha(cssVar("--critical"), 0.16),
          dns: alpha(cssVar("--s7"), 0.18), loss: alpha(cssVar("--warning"), 0.22), gap: alpha(cssVar("--muted"), 0.15),
        };
        ctx.save();
        for (const i of state.incidents) {
          if (i.end < x0 || i.start > x1) continue;
          const a = u.valToPos(Math.max(i.start, x0), "x", true);
          const b = u.valToPos(Math.min(i.end, x1), "x", true);
          ctx.fillStyle = colors[i.kind] || colors.gap;
          ctx.fillRect(a, u.bbox.top, Math.max(b - a, 2 * devicePixelRatio), u.bbox.height);
        }
        ctx.restore();
      },
    },
  };
}

// one floating tooltip shared by all charts; values lead, labels follow
function tooltipPlugin(build) {
  const tip = $("#tooltip");
  return {
    hooks: {
      setCursor: (u) => {
        const i = u.cursor.idx;
        if (i == null || u.cursor.left < 0) { tip.hidden = true; return; }
        const c = build(u, i);
        if (!c) { tip.hidden = true; return; }
        tip.replaceChildren(h("div", { class: "tt-time" }, c.title),
          ...c.rows.map(([color, val, label]) =>
            h("div", { class: "row" }, h("span", { class: "ln", style: color ? `background:${color}` : "" }), h("b", {}, val), h("span", {}, label))));
        tip.hidden = false;
        const r = u.over.getBoundingClientRect();
        let left = r.left + u.cursor.left + 14;
        let top = r.top + u.cursor.top + 14;
        if (left + tip.offsetWidth > innerWidth - 8) left = r.left + u.cursor.left - tip.offsetWidth - 14;
        if (top + tip.offsetHeight > innerHeight - 8) top = innerHeight - tip.offsetHeight - 8;
        tip.style.left = `${Math.max(8, left)}px`;
        tip.style.top = `${top}px`;
      },
    },
  };
}

// synced charts mirror a drag-select; only the chart the drag started in zooms
let dragSource = null;
function zoomPlugin() {
  return {
    hooks: {
      init: (u) => u.over.addEventListener("mousedown", () => { dragSource = u; }),
      setSelect: (u) => {
        if (u !== dragSource) {
          if (u.select.width) u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          return;
        }
        dragSource = null;  // don't keep a chart that is about to be destroyed
        if (u.select.width > 6) {
          const a = u.posToVal(u.select.left, "x");
          const b = u.posToVal(u.select.left + u.select.width, "x");
          u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          setRange(a, b);
        }
      },
    },
  };
}

function timeTitle(u, i) {
  const b = state.series.bucket;
  const t = u.data[0][i];
  return b ? `${fmtTimeShort(t - b / 2)} – ${fmtTimeShort(t + b / 2)} (${fmtDur(b)} avg)` : fmtTime(t);
}

function renderHistory() {
  const S = state.series;
  const names = state.targets.map((t) => t.name).filter((n) => S.targets[n]);
  const visible = names.filter((n) => !state.hidden.has(n));
  const m = state.metric;
  const [xmin, xmax] = currentRange();
  const xRange = () => [xmin, Math.min(xmax, Math.max(xmin + 60, Date.now() / 1000))];
  const common = () => ({
    padding: [8, 8, 0, 0],
    legend: { show: false },
    cursor: { sync: { key: "hist" }, drag: { x: true, y: false, setScale: false }, points: { size: 7 } },
  });
  $("#lat-sub").textContent = `ms, ${m === "avg" ? "average" : m === "max" ? "worst ping" : "best ping"}` +
    (S.bucket ? ` per ${fmtDur(S.bucket)}` : " per probe cycle");
  $("#dns-sub").textContent = S.bucket ? `probe cycles with a DNS failure per ${fmtDur(S.bucket)}` : "probe cycle had a DNS failure";

  // latency
  const latData = [S.t, ...visible.map((n) => S.targets[n][m].map((v) => (state.logScale && v != null && v <= 0 ? null : v)))];
  destroyChart("lat");
  const latEl = $("#lat-chart");
  latEl.querySelector(".no-data")?.remove();
  state.charts.lat = new uPlot(Object.assign(common(), {
    width: latEl.clientWidth, height: 260,
    scales: { x: { time: true, range: xRange }, y: state.logScale ? { distr: 3, log: 10 } : { range: (u, mn, mx) => [0, mx * 1.05 || 1] } },
    axes: [axis({ space: 70 }), axis({ size: 50, values: (u, v) => v.map((x) => (x == null ? "" : fmtMs(x))) })],
    series: [{}, ...visible.map((n) => ({ label: n, stroke: colorOf(n), width: 2, spanGaps: false, points: { show: false } }))],
    plugins: [bandPlugin(), zoomPlugin(), tooltipPlugin((u, i) => ({
      title: timeTitle(u, i),
      rows: visible.map((n, k) => [colorOf(n), u.data[k + 1][i] != null ? `${fmtMs(u.data[k + 1][i])} ms`
        : S.dns[i] && isHost(n) ? "DNS" : "—", n]),
    }))],
  }), latData, latEl);

  // loss
  const lossData = [S.t, ...visible.map((n) => S.targets[n].loss)];
  destroyChart("loss");
  const lossEl = $("#loss-chart");
  state.charts.loss = new uPlot(Object.assign(common(), {
    width: lossEl.clientWidth, height: 170,
    scales: { x: { time: true, range: xRange }, y: { range: (u, mn, mx) => [0, Math.max(10, Math.min(100, Math.ceil((mx * 1.1) / 10) * 10))] } },
    axes: [axis({ space: 70 }), axis({ size: 50, values: (u, v) => v.map((x) => `${x}%`) })],
    series: [{}, ...visible.map((n) => ({ label: n, stroke: colorOf(n), width: 2, points: { show: false }, paths: uPlot.paths.stepped({ align: 1 }) }))],
    plugins: [bandPlugin(), zoomPlugin(), tooltipPlugin((u, i) => ({
      title: timeTitle(u, i),
      rows: visible.map((n, k) => [colorOf(n), u.data[k + 1][i] != null ? fmtPct(u.data[k + 1][i], S.bucket ? 1 : 0)
        : S.dns[i] && isHost(n) ? "DNS" : "—", n]),
    }))],
  }), lossData, lossEl);

  // DNS failures
  destroyChart("dnsh");
  const dnsEl = $("#dnsh-chart");
  const crit = cssVar("--s7");
  state.charts.dnsh = new uPlot(Object.assign(common(), {
    width: dnsEl.clientWidth, height: 130,
    scales: { x: { time: true, range: xRange }, y: { range: (u, mn, mx) => [0, Math.max(1, mx)] } },
    axes: [axis({ space: 70 }), axis({ size: 50, values: (u, v) => v.map((n) => (Number.isInteger(n) ? n : "")) })],
    series: [{}, { label: "DNS failures", stroke: crit, fill: crit, width: 0, paths: uPlot.paths.bars({ size: [0.9, 18], align: 0 }), points: { show: false } }],
    plugins: [bandPlugin(), zoomPlugin(), tooltipPlugin((u, i) => ({
      title: timeTitle(u, i),
      rows: [[crit, String(S.dns[i]), S.bucket ? `of ${S.samples[i]} cycles had DNS failure` : (S.dns[i] ? "DNS failure" : "resolved OK")]],
    }))],
  }), [S.t, S.dns], dnsEl);

  if (!S.t.length) {
    latEl.prepend(h("div", { class: "muted no-data", style: "padding:8px" }, "No samples in this range."));
  }
}

function renderChips() {
  $("#target-chips").replaceChildren(...state.targets.map((t) => {
    const off = state.hidden.has(t.name);
    return h("button", {
      class: `chip${off ? " off" : ""}`, "aria-pressed": String(!off), title: off ? "Show" : "Hide",
      onclick: () => {
        if (state.hidden.has(t.name)) state.hidden.delete(t.name); else state.hidden.add(t.name);
        renderChips();
        if (state.series) renderHistory();
      },
    }, h("span", { class: "swatch", style: `background:${colorOf(t.name)}` }), t.name);
  }));
}

const KIND = {
  wan_down: ["critical", "Internet down"],
  lan_down: ["critical", "Gateway down"],
  dns: ["serious", "DNS failure"],
  loss: ["warning", "Packet loss"],
  gap: ["unknown", "Monitoring gap"],
};

function renderIncidents() {
  const rows = state.incidents.filter((i) => !state.incKind || i.kind === state.incKind);
  const tb = $("#inc-table tbody");
  if (!rows.length) {
    tb.replaceChildren(h("tr", {}, h("td", { colspan: 4, class: "empty" }, "No incidents in this range.")));
    return;
  }
  tb.replaceChildren(...rows.slice(0, 500).map((i) => {
    const [st, label] = KIND[i.kind] || ["unknown", i.kind];
    return h("tr", { class: "click", title: "Zoom to incident", onclick: () => zoomTo(i.start, i.end) },
      h("td", {}, badge(st, label)),
      h("td", {}, fmtTime(i.start), i.ongoing ? h("span", {}, " ", badge("critical", "Ongoing")) : null),
      h("td", {}, fmtDur(i.duration)),
      h("td", {}, i.detail));
  }));
}

// ------------------------------------------------------------ daily table --

async function loadDaily() {
  try {
    const d = await api("daily");
    state.daily = d.days;
    if (d.days.length) state.firstTs = d.days[0].first;
    renderDaily();
    renderDnsDaily();
  } catch (e) { /* banner handled by status */ }
  try {
    const all = await api("incidents", { start: 0, end: Date.now() / 1000 + 60 });
    state.allIncidents = all.incidents;
    renderDnsTable();
    renderDnsNow();
  } catch (_) { }
}

function renderDaily() {
  const names = state.targets.map((t) => t.name);
  $("#daily-table thead").replaceChildren(h("tr", {},
    h("th", {}, "Date"), h("th", { class: "num" }, "Coverage"),
    ...names.map((n) => h("th", { class: "num", title: `${n} average latency (ms)` },
      h("span", { class: "swatch", style: `display:inline-block;width:10px;height:3px;border-radius:2px;vertical-align:middle;margin-right:4px;background:${colorOf(n)}` }), n)),
    h("th", { class: "num" }, "Worst loss"), h("th", { class: "num" }, "DNS fail cycles"),
    h("th", { class: "num" }, "DNS affected"), h("th", { class: "num" }, "Internet down"), h("th", { class: "num" }, "Gateway down")));
  const rows = [...state.daily].reverse();
  $("#daily-table tbody").replaceChildren(...rows.map((d) => {
    const losses = Object.values(d.targets).map((t) => t.loss ?? 0);
    const worst = Math.max(0, ...losses);
    const start = new Date(d.date + "T00:00:00").getTime() / 1000;
    const isToday = d === state.daily.at(-1) && Date.now() / 1000 - start < 86400;
    const cov = isToday ? d.span_coverage : d.coverage;
    return h("tr", { class: "click", title: "Show this day in History", onclick: () => {
      setRange(start, start + 86400);
      $("#hist-cards").scrollIntoView({ behavior: "smooth" });
    } },
      h("td", { style: "white-space:nowrap" }, dfDay.format(new Date(d.date + "T12:00:00"))),
      h("td", { class: `num${cov < 0.95 ? " cell-warn" : ""}`, title: isToday ? "Share of the day so far" : "Share of the day with samples" }, fmtPct(cov * 100, 1)),
      ...names.map((n) => {
        const t = d.targets[n];
        return h("td", { class: "num", title: t ? `p95 ${fmtMs(t.p95)} ms · max ${fmtMs(t.max)} ms · loss ${fmtPct(t.loss, 2)}` : "" }, t ? fmtMs(t.avg) : "—");
      }),
      h("td", { class: `num${worst >= 1 ? " cell-bad" : ""}` }, fmtPct(worst, 2)),
      h("td", { class: `num${d.dns_rows ? " cell-bad" : ""}` }, d.dns_rows || "0"),
      h("td", { class: "num" }, d.dns_seconds ? fmtDur(d.dns_seconds) : "—"),
      h("td", { class: `num${d.wan_down_seconds ? " cell-bad" : ""}` }, d.wan_down_seconds ? fmtDur(d.wan_down_seconds) : "—"),
      h("td", { class: `num${d.lan_down_seconds ? " cell-bad" : ""}` }, d.lan_down_seconds ? fmtDur(d.lan_down_seconds) : "—"));
  }));
}

// ---------------------------------------------------------- device events --

let eventsSeq = 0;
async function loadEvents() {
  const seq = ++eventsSeq;
  const [start, end] = currentRange();
  try {
    const r = await api("events", { start, end, offset: state.evOffset, limit: state.evLimit, only: state.evOnly });
    if (seq !== eventsSeq) return;
    if (r.total && state.evOffset >= r.total) {  // the range slid past this page: show the last one
      state.evOffset = Math.floor((r.total - 1) / state.evLimit) * state.evLimit;
      return loadEvents();
    }
    state.evTotal = r.total;
    renderEvents(r.rows);
  } catch (e) { /* ignore */ }
}

function renderEvents(rows) {
  const names = state.targets.map((t) => t.name);
  $("#ev-table thead").replaceChildren(h("tr", {}, h("th", {}, "Time"), h("th", {}, "Flags"),
    ...names.map((n) => h("th", {}, n))));
  const p0 = state.evOffset + 1, p1 = Math.min(state.evOffset + state.evLimit, state.evTotal);
  $("#ev-page").textContent = state.evTotal ? `${p0}–${p1} of ${state.evTotal.toLocaleString()}` : "0";
  $("#ev-prev").disabled = state.evOffset === 0;
  $("#ev-next").disabled = p1 >= state.evTotal;
  const tb = $("#ev-table tbody");
  if (!rows.length) {
    tb.replaceChildren(h("tr", {}, h("td", { colspan: names.length + 2, class: "empty" }, "No device events in this range.")));
    return;
  }
  tb.replaceChildren(...rows.map((r) => h("tr", { class: "click", onclick: () => zoomTo(r.ts - 300, r.ts + 300) },
    h("td", { style: "white-space:nowrap" }, fmtTime(r.ts)),
    h("td", {}, r.lan_down ? badge("critical", "Gateway down") : null, r.wan_down ? badge("critical", "Internet down") : null,
      r.dns ? badge("serious", "DNS") : null),
    ...names.map((n) => {
      const t = r.targets[n];
      if (!t) return h("td", { class: "cell-bad" }, r.missing.includes(n) ? "not resolved" : "—");
      const cls = t.loss >= 100 ? "cell-bad" : t.loss > 0 ? "cell-warn" : "";
      return h("td", { class: cls, style: "white-space:nowrap" },
        t.loss ? `${t.loss}% loss` : `${fmtMs(t.avg)} ms`,
        t.max != null ? h("span", { class: "muted" }, ` (max ${fmtMs(t.max)})`) : null);
    }))));
}

// ------------------------------------------------------------------ wiring --

function wire() {
  document.querySelectorAll("#presets button").forEach((b) =>
    b.addEventListener("click", () => setPreset(b.dataset.p === "all" ? "all" : Number(b.dataset.p))));
  $("#apply-range").addEventListener("click", () => {
    const a = new Date($("#from").value).getTime() / 1000;
    const b = new Date($("#to").value).getTime() / 1000;
    if (!(a < b)) return showBanner("Pick a 'From' time earlier than 'To'.");
    showBanner("");
    setRange(a, b);
  });
  document.querySelectorAll("#lat-metric button").forEach((b) => b.addEventListener("click", () => {
    state.metric = b.dataset.m;
    document.querySelectorAll("#lat-metric button").forEach((x) => x.classList.toggle("on", x === b));
    renderHistory();
  }));
  $("#log-scale").addEventListener("change", (e) => { state.logScale = e.target.checked; renderHistory(); });
  document.querySelectorAll("#inc-filter button").forEach((b) => b.addEventListener("click", () => {
    state.incKind = b.dataset.k;
    document.querySelectorAll("#inc-filter button").forEach((x) => x.classList.toggle("on", x === b));
    renderIncidents();
  }));
  document.querySelectorAll("#ev-filter button").forEach((b) => b.addEventListener("click", () => {
    state.evOnly = b.dataset.o; state.evOffset = 0;
    document.querySelectorAll("#ev-filter button").forEach((x) => x.classList.toggle("on", x === b));
    loadEvents();
  }));
  $("#ev-prev").addEventListener("click", () => { state.evOffset = Math.max(0, state.evOffset - state.evLimit); loadEvents(); });
  $("#ev-next").addEventListener("click", () => { state.evOffset += state.evLimit; loadEvents(); });
  $("#theme-btn").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("netmon-theme", document.documentElement.dataset.theme); } catch (_) { }
    rerenderAll();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rerenderAll);
  document.addEventListener("mouseleave", () => { $("#tooltip").hidden = true; });

  // charts are sized by width only; height changes from content must not rebuild them
  let rt, lastWidth = 0;
  new ResizeObserver(([entry]) => {
    const w = Math.round(entry.contentRect.width);
    if (w === lastWidth) return;
    lastWidth = w;
    clearTimeout(rt);
    rt = setTimeout(rerenderAll, 150);
  }).observe($("main"));
}

function rerenderAll() {
  palette = null;
  renderChips();
  if (state.status) renderStatus();
  if (state.series) renderHistory();
  if (state.daily.length) { renderDaily(); renderDnsDaily(); }
}

async function init() {
  try {
    const t = localStorage.getItem("netmon-theme");
    if (t) document.documentElement.dataset.theme = t;
  } catch (_) { }
  wire();
  await loadStatus();
  await Promise.all([loadRecent(), loadDaily()]);
  await loadHistory();

  // a tick is skipped while the previous one is still waiting on the server
  const poll = (fn, ms) => {
    let busy = false;
    setInterval(async () => {
      if (busy || !$("#auto-refresh").checked) return;
      busy = true;
      try { await fn(); } finally { busy = false; }
    }, ms);
  };
  poll(() => Promise.all([loadStatus(), loadRecent()]), STATUS_MS);
  poll(() => (state.preset ? loadHistory() : null), HISTORY_MS);
  poll(loadDaily, SLOW_MS);
}

init();
