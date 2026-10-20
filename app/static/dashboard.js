/* Compact, read-only dashboard for Home Assistant (landscape tablet). Reads stored data only. */
"use strict";
const D = { map: null, layers: null, sel: null, data: null };

async function dashLoad() {
  try {
    if (!RT.meta) RT.meta = await api("/api/meta");
    D.data = await api("/api/v1/latest");
  } catch (e) {
    clear($("cols")).appendChild(el("div.dash-dir", el("b", e.status === 401 ? "Not authorised" : "Could not load data"),
      el("div.small.muted", e.status === 401 ? "Open this page with ?token=<EMBED_TOKEN>, or log in to 8star-routes in this browser first." : e.message)));
    return;
  }
  if (!D.map) { D.map = makeMap($("dmap")); D.layers = L.featureGroup().addTo(D.map); }
  if (D.sel === null) D.sel = D.data.directions.find(d => d.observed_utc)?.direction_id ?? D.data.directions[0]?.direction_id;
  render();
}

function render() {
  const cols = clear($("cols"));
  for (const d of D.data.directions.slice(0, 2)) {
    const card = el("div.dash-dir" + (d.direction_id === D.sel ? ".sel" : ""), { onclick: () => { D.sel = d.direction_id; render(); } });
    card.appendChild(el("div.hd", el("b", d.name), el("span.spacer"),
      D.data.data_source === "demo" ? el("span.badge.demo", "DEMO") : null,
      d.observed_utc ? el("span.badge" + (d.stale ? ".bad" : ".neutral"), d.stale ? "Stale · " + fmtAge(d.age_minutes * 60) : fmtTime(d.observed_utc)) : el("span.badge.bad", "No data"),
      d.partial ? el("span.badge.warn", "Partial") : null));
    const rec = d.routes.find(r => r.key === "recommended");
    if (rec && rec.status === "ok") {
      card.appendChild(el("div.hero", el("span.v", fmtMin(rec.travel_time_s)), el("span", "delay " + fmtDelay(rec.traffic_delay_s)),
        el("span.muted.small", rec.resembles ? "via " + rec.resembles : "TomTom recommendation")));
    } else card.appendChild(el("div.hero", el("span.v", "—"), el("span.muted", rec ? "recommendation failed" : "no recommendation stored")));
    const alt = el("div.alt");
    for (const r of d.routes.filter(r => r.key !== "recommended")) {
      const color = r.color || "#868e96";
      alt.appendChild(el("i", { style: { background: color } }));
      alt.appendChild(el("span", r.name, r.deviation_flag ? " ⚠" : ""));
      alt.appendChild(el("span.n", r.status === "ok" ? fmtMin(r.travel_time_s) : "no data"));
      alt.appendChild(el("span.n.muted", r.status === "ok" ? fmtDelay(r.traffic_delay_s) : ""));
    }
    card.appendChild(alt);
    const spark = el("div.chart.spark");
    card.appendChild(el("div.small.muted", "Today, estimated travel time per route"));
    card.appendChild(spark);
    cols.appendChild(card);
    drawSpark(spark, d.direction_id);
  }
  const foot = clear($("foot"));
  foot.appendChild(el("span", "TomTom estimates at observation time, not measured journeys."));
  foot.appendChild(el("span.spacer"));
  foot.appendChild(el("span", "Updated " + fmtTime(new Date().toISOString())));
  drawMap();
}

async function drawMap() {
  D.layers.clearLayers();
  if (D.sel == null) {
    $("cols").appendChild(el("div.dash-dir", "No journey configured. Open Setup in the main application."));
    return;
  }
  const latest = await api(`/api/latest?direction_id=${D.sel}`);
  const det = latest.detail;
  if (!det) return;
  D.colors = Object.fromEntries(det.routes.filter(r => r.key).map(r => [r.key, r.color]));
  const b = [];
  const order = det.routes.filter(r => r.status === "ok" && r.role !== "alternative").sort((a, c) => (a.role === "recommended") - (c.role === "recommended"));
  for (const r of order) {
    const coords = await variantCoords(r.variant_id);
    if (!coords) continue;
    routeLine(coords, r.color, { weight: r.role === "recommended" ? 7 : 4 }).addTo(D.layers);
    b.push(...coords);
  }
  if (b.length) D.map.fitBounds(L.latLngBounds(b), { padding: [20, 20] });
}

async function drawSpark(node, directionId) {
  const day = await api(`/api/day?direction_id=${directionId}&date=${localDate()}`);
  const slots = day.slots.filter(s => s.cycle_status !== "future");
  const xs = slots.map(s => Date.parse(s.slot_utc));
  const series = day.routes.map(r => ({ name: r.name, color: r.color,
    values: slots.map(s => { const o = s.routes[r.key]; return o && o.status === "ok" ? o.travel_time_s : null; }) }));
  lineChart(node, { xs, series, yFormat: v => fmtMin(v), xFormat: (t, long) => fmtTime(new Date(t).toISOString(), long),
    height: Math.max(90, Math.min(200, node.parentNode.clientHeight - 170)), unit: 60 });
}

dashLoad();
setInterval(() => { if (!document.hidden) dashLoad(); }, 60000);
