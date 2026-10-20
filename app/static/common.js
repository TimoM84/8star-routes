/* Shared helpers for the main app and the dashboard. No framework. */
"use strict";

const BASE = document.body.dataset.base || "";
const RT = {
  meta: null,
  embedToken: new URLSearchParams(location.search).get("token") || "",
  variantCache: new Map(),
};

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, opts = {}) {
  const headers = { "Accept": "application/json", ...(opts.headers || {}) };
  if (RT.embedToken) headers["X-Embed-Token"] = RT.embedToken;
  if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; }
  if (opts.method && opts.method !== "GET") headers["X-Requested-With"] = "8star-routes";
  const res = await fetch(BASE + path, {
    method: opts.method || "GET", headers, credentials: "same-origin",
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.reason || data.message)) || `HTTP ${res.status}`;
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

/* DOM builder: el("div.card", {onclick}, child, "text"). Text is always set via textContent. */
function el(spec, attrs, ...children) {
  const [tag, ...classes] = spec.split(".");
  const n = document.createElement(tag || "div");
  if (classes.length) n.className = classes.join(" ");
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) { children.unshift(attrs); attrs = null; }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
    else if (k === "dataset") Object.assign(n.dataset, v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
function $(id) { return document.getElementById(id); }

function toast(msg, ms = 3500) {
  const t = el("div.toast", msg);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

/* ---- formatting: original precision is stored; rounding happens only here ---- */
function tz() { return (RT.meta && RT.meta.timezone) || "Europe/Amsterdam"; }
function fmtMin(s) { return s === null || s === undefined ? "—" : `${Math.round(s / 60)} min`; }
function fmtDelay(s) {
  if (s === null || s === undefined) return "—";
  if (s === 0) return "0 min";
  return s > 0 && s < 30 ? "<1 min" : `+${Math.round(s / 60)} min`;
}
function fmtSigned(s) {
  if (s === null || s === undefined) return "—";
  if (s === 0) return "±0 min";
  if (Math.abs(s) < 30) return (s > 0 ? "+" : "−") + "<1 min";
  const m = Math.round(s / 60); return (m > 0 ? "+" : "−") + Math.abs(m) + " min";
}
function fmtKm(m) { return m === null || m === undefined ? "—" : `${(m / 1000).toFixed(1)} km`; }
function fmtPct(x) { return x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`; }
function fmtTime(iso, withDate = false) {
  if (!iso) return "—";
  const d = new Date(iso);
  const o = { timeZone: tz(), hour: "2-digit", minute: "2-digit", hour12: false };
  if (withDate) Object.assign(o, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  return new Intl.DateTimeFormat("en-GB", o).format(d);
}
function fmtOffsetTime(iso) {
  // TomTom departure/arrival keep their own offset; show local wall time.
  return iso ? fmtTime(iso) : "—";
}
function fmtAge(sec) {
  if (sec === null || sec === undefined) return "unknown age";
  if (sec < 90) return "just now";
  const m = Math.round(sec / 60);
  if (m < 90) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}
function localDate(d = new Date()) {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: tz(), year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
  return p; // YYYY-MM-DD
}
function addDays(ymd, n) {
  const d = new Date(ymd + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10);
}
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/* ---- map helpers ---- */
function tileUrl(template) {
  if (!template) return null;
  return RT.embedToken ? template + (template.includes("?") ? "&" : "?") + "embed=" + encodeURIComponent(RT.embedToken) : template;
}

function makeMap(node) {
  const m = L.map(node, { zoomControl: true, attributionControl: true, preferCanvas: false });
  const mm = RT.meta && RT.meta.map;
  if (mm && mm.tiles_available && mm.tile_url) {
    L.tileLayer(tileUrl(mm.tile_url), { maxZoom: 19, tileSize: 256, attribution: "© TomTom", crossOrigin: false }).addTo(m);
  } else {
    m.attributionControl.addAttribution("No base map configured");
  }
  m.setView([52.1, 5.3], 7);
  return m;
}

async function variantCoords(id) {
  if (!id) return null;
  if (RT.variantCache.has(id)) return RT.variantCache.get(id);
  const v = await api(`/api/variants/${id}`);
  RT.variantCache.set(id, v.coords);
  if (RT.variantCache.size > 400) RT.variantCache.delete(RT.variantCache.keys().next().value);
  return v.coords;
}

function routeLine(coords, color, opts = {}) {
  const weight = opts.weight || 5;
  const group = L.featureGroup();
  // White casing keeps overlapping routes legible.
  L.polyline(coords, { color: "#ffffff", weight: weight + 3, opacity: opts.opacity ?? .9, interactive: false }).addTo(group);
  L.polyline(coords, { color, weight, opacity: opts.opacity ?? 1, dashArray: opts.dash || null }).addTo(group);
  return group;
}

/* Traffic sections stored with the observation (TomTom sectionType=traffic at that time). */
function sectionLines(coords, sections) {
  const g = L.featureGroup();
  for (const s of sections || []) {
    if (s.start === null || s.end === null || s.start === undefined) continue;
    const seg = coords.slice(s.start, s.end + 1);
    if (seg.length < 2) continue;
    const color = s.category === "ROAD_CLOSURE" ? "#000000" : (s.magnitude >= 3 ? "#a61e1e" : s.magnitude === 2 ? "#d03b3b" : "#ec835a");
    L.polyline(seg, { color, weight: 3, opacity: 1, interactive: false }).addTo(g);
  }
  return g;
}
