/* Small SVG charts: line chart with crosshair tooltip, status band, heatmap. */
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

function niceTicks(min, max, n = 4) {
  if (min === max) { max = min + 1; }
  const span = max - min;
  const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 5, 10].map(f => f * mag).find(s => s >= step0) || mag * 10;
  const out = [];
  for (let v = Math.floor(min / step) * step; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

function makeTooltip(container) {
  let tip = container.querySelector(".tooltip");
  if (!tip) { tip = el("div.tooltip.hidden"); container.appendChild(tip); }
  return tip;
}

function placeTip(container, tip, x, y) {
  tip.classList.remove("hidden");
  const w = container.clientWidth;
  const tw = tip.offsetWidth;
  tip.style.left = Math.min(Math.max(0, x + 12), w - tw) + "px";
  tip.style.top = Math.max(0, y - 10) + "px";
}

/**
 * Line chart. xs: array of ms timestamps. series: [{name, color, values: [number|null]}].
 * Null values break the line (missing data is a gap, never zero).
 */
function lineChart(container, { xs, series, yFormat, xFormat, height = 220, yZero = false, onPick, unit = 1 }) {
  clear(container);
  const W = Math.max(300, container.clientWidth || 600), H = height;
  const m = { l: 52, r: 12, t: 10, b: 24 };
  const all = series.flatMap(s => s.values).filter(v => v !== null && v !== undefined);
  if (!xs.length || !all.length) { container.appendChild(el("p.muted.small", "No successful observations in this range.")); return; }
  let ymin = yZero ? 0 : Math.min(...all), ymax = Math.max(...all);
  const pad = (ymax - ymin) * 0.08 || 60; ymin = yZero ? 0 : ymin - pad; ymax += pad;
  const ticks = niceTicks(ymin / unit, ymax / unit).map(t => t * unit);
  ymin = Math.min(ymin, ticks[0]); ymax = Math.max(ymax, ticks[ticks.length - 1]);
  const x0 = xs[0], x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
  const X = t => m.l + (t - x0) / (x1 - x0) * (W - m.l - m.r);
  const Y = v => m.t + (1 - (v - ymin) / (ymax - ymin)) * (H - m.t - m.b);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img" });
  const grid = svg("g", { class: "grid" }), axis = svg("g", { class: "axis" });
  for (const t of ticks) {
    grid.appendChild(svg("line", { x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t) }));
    const tx = svg("text", { x: m.l - 6, y: Y(t) + 4, "text-anchor": "end" }); tx.textContent = yFormat(t); axis.appendChild(tx);
  }
  const hours = Math.max(1, Math.round((x1 - x0) / 3600e3));
  const stepH = hours > 30 ? 24 : hours > 12 ? 3 : hours > 6 ? 2 : 1;
  for (let i = 0; i < xs.length; i++) {
    const d = new Date(xs[i]);
    const parts = new Intl.DateTimeFormat("en-GB", { timeZone: tz(), hour: "numeric", minute: "numeric", hour12: false }).formatToParts(d);
    const hh = +parts.find(p => p.type === "hour").value, mm = +parts.find(p => p.type === "minute").value;
    if (mm === 0 && hh % stepH === 0) {
      const tx = svg("text", { x: X(xs[i]), y: H - 6, "text-anchor": "middle" }); tx.textContent = xFormat(xs[i]); axis.appendChild(tx);
    }
  }
  root.appendChild(grid); root.appendChild(axis);
  for (const s of series) {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += (pen ? "L" : "M") + X(xs[i]).toFixed(1) + "," + Y(v).toFixed(1); pen = true;
    });
    root.appendChild(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    // isolated points (neighbours missing) still need a visible mark
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) return;
      const prev = s.values[i - 1], next = s.values[i + 1];
      if ((prev === null || prev === undefined) && (next === null || next === undefined))
        root.appendChild(svg("circle", { cx: X(xs[i]), cy: Y(v), r: 3, fill: s.color }));
    });
  }
  const hair = svg("line", { class: "hair hidden", y1: m.t, y2: H - m.b, visibility: "hidden" });
  root.appendChild(hair);
  const hit = svg("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent", tabindex: 0 });
  root.appendChild(hit);
  container.appendChild(root);
  const tip = makeTooltip(container);
  const pick = (evtX) => {
    const rect = root.getBoundingClientRect();
    const px = (evtX - rect.left) * (W / rect.width);
    const t = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0);
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - t) < Math.abs(xs[best] - t)) best = i;
    return best;
  };
  const show = (i, clientX) => {
    hair.setAttribute("x1", X(xs[i])); hair.setAttribute("x2", X(xs[i])); hair.setAttribute("visibility", "visible");
    clear(tip);
    tip.appendChild(el("div.small.muted", xFormat(xs[i], true)));
    for (const s of series) {
      const v = s.values[i];
      tip.appendChild(el("div.t-row", el("span.t-key", { style: { background: s.color } }),
        el("b", v === null || v === undefined ? "no data" : yFormat(v)), el("span.muted", s.name)));
    }
    const rect = container.getBoundingClientRect();
    placeTip(container, tip, X(xs[i]) * rect.width / W, 10);
  };
  hit.addEventListener("pointermove", e => show(pick(e.clientX), e.clientX));
  hit.addEventListener("pointerleave", () => { tip.classList.add("hidden"); hair.setAttribute("visibility", "hidden"); });
  if (onPick) hit.addEventListener("click", e => onPick(pick(e.clientX)));
  let focusIdx = 0;
  hit.addEventListener("keydown", e => {
    if (e.key === "ArrowRight") focusIdx = Math.min(xs.length - 1, focusIdx + 1);
    else if (e.key === "ArrowLeft") focusIdx = Math.max(0, focusIdx - 1);
    else return;
    e.preventDefault(); show(focusIdx);
  });
}

/** One rect per slot. items: [{color, label, tooltip:[lines]}]. */
function bandChart(container, items, { height = 28, onPick, selected = -1 } = {}) {
  clear(container);
  const W = Math.max(300, container.clientWidth || 600), H = height;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, height: H, preserveAspectRatio: "none" });
  const w = W / Math.max(1, items.length);
  items.forEach((it, i) => {
    const r = svg("rect", { x: i * w + 0.5, y: 2, width: Math.max(1, w - 1), height: H - 4, rx: Math.min(2, w / 3), fill: it.color });
    if (it.hatch) r.setAttribute("fill-opacity", ".35");
    if (i === selected) { r.setAttribute("stroke", "var(--text)"); r.setAttribute("stroke-width", "2"); }
    root.appendChild(r);
  });
  container.appendChild(root);
  const tip = makeTooltip(container);
  const idx = e => { const rc = root.getBoundingClientRect(); return Math.max(0, Math.min(items.length - 1, Math.floor((e.clientX - rc.left) / rc.width * items.length))); };
  root.addEventListener("pointermove", e => {
    const i = idx(e); clear(tip);
    for (const line of items[i].tooltip || []) tip.appendChild(el("div", line));
    const rc = container.getBoundingClientRect();
    placeTip(container, tip, (i + 0.5) * rc.width / items.length, H + 4);
  });
  root.addEventListener("pointerleave", () => tip.classList.add("hidden"));
  if (onPick) root.addEventListener("click", e => onPick(idx(e)));
}

/** Weekday x bucket heatmap using a single-hue sequential ramp. */
const RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];
function heatmapChart(container, data, fmt) {
  clear(container);
  const buckets = 1440 / data.bucket_minutes;
  const vals = data.cells.map(c => c.median).filter(v => v !== null);
  if (!vals.length) { container.appendChild(el("p.muted.small", "No successful observations for this route in the selected range.")); return; }
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const grid = el("div.heat", { style: { gridTemplateColumns: `36px repeat(${buckets}, minmax(14px, 1fr))`, minWidth: `${36 + buckets * 16}px` } });
  const byKey = new Map(data.cells.map(c => [`${c.weekday}:${c.bucket}`, c]));
  grid.appendChild(el("div"));
  for (let b = 0; b < buckets; b++) {
    const m = b * data.bucket_minutes;
    grid.appendChild(el("div.lab", m % 180 === 0 ? String(m / 60).padStart(2, "0") : ""));
  }
  const tip = makeTooltip(container);
  container.style.position = "relative";
  for (let wd = 0; wd < 7; wd++) {
    grid.appendChild(el("div.lab", WEEKDAYS[wd]));
    for (let b = 0; b < buckets; b++) {
      const c = byKey.get(`${wd}:${b}`);
      const cell = el("div.cell");
      if (!c || c.median === null) cell.classList.add("empty");
      else {
        const t = hi === lo ? 0.5 : (c.median - lo) / (hi - lo);
        cell.style.background = RAMP[Math.round(t * (RAMP.length - 1))];
      }
      const hh = String(Math.floor(b * data.bucket_minutes / 60)).padStart(2, "0"), mm = String(b * data.bucket_minutes % 60).padStart(2, "0");
      cell.addEventListener("pointerenter", () => {
        clear(tip);
        tip.appendChild(el("div", el("b", c && c.median !== null ? fmt(c.median) : "no data"), " median"));
        if (c && c.p90 !== null) tip.appendChild(el("div", el("b", fmt(c.p90)), " 90th percentile"));
        tip.appendChild(el("div.muted", `${WEEKDAYS[wd]} ${hh}:${mm} · ${c ? c.n : 0} of ${c ? c.expected : 0} slots with data`));
        const rc = container.getBoundingClientRect(), cr = cell.getBoundingClientRect();
        placeTip(container, tip, cr.left - rc.left + container.scrollLeft, cr.bottom - rc.top + 6);
      });
      cell.addEventListener("pointerleave", () => tip.classList.add("hidden"));
      grid.appendChild(cell);
    }
  }
  container.appendChild(grid);
  container.appendChild(el("div.legend", { style: { marginTop: "6px" } }, el("span", fmt(lo)),
    el("span", { style: { display: "inline-block", width: "120px", height: "10px", borderRadius: "3px", background: `linear-gradient(90deg, ${RAMP[0]}, ${RAMP[RAMP.length - 1]})` } }),
    el("span", fmt(hi))));
}
