/* 8star-routes main interface. Reads stored observations only; routing requests
   happen exclusively through explicit, labelled buttons (Collect now, Preview, Check live). */
"use strict";

const S = {
  view: null,
  role: null,
  map: null, mapLayers: null, trafficLayer: null, trafficTimer: null,
  mode: "current", direction: null, cycleId: null, day: null, daySlots: null,
  highlight: null,
  setupMap: null, setupLayers: null, draft: null,
};

/* ---------------- boot ---------------- */
async function boot() {
  const sess = await api("/api/session").catch(() => ({ role: null }));
  if (!sess.role && !RT.embedToken) { showLogin(); return; }
  S.role = sess.role || "viewer";
  try { RT.meta = await api("/api/meta"); }
  catch (e) { if (e.status === 401) { showLogin(); return; } throw e; }
  $("logout").classList.toggle("hidden", !(sess.role === "admin" && sess.auth_mode === "password"));
  const badge = $("mode-badge");
  badge.textContent = RT.meta.mode === "demo" ? "DEMO DATA" : "Live data";
  badge.className = "badge " + (RT.meta.mode === "demo" ? "demo" : "live");
  for (const sel of ["m-direction", "a-direction", "p-direction"]) fillDirections($(sel));
  S.direction = RT.meta.directions.find(d => d.enabled)?.id || RT.meta.directions[0]?.id;
  wireMap(); wireAnalysis(); wirePlan();
  window.addEventListener("hashchange", route);
  route();
  renderBanners();
  setInterval(async () => { try { RT.meta = await api("/api/meta"); renderBanners(); if (S.view === "map" && S.mode === "current") loadCurrent(); } catch (e) {} }, 60000);
}

function showLogin() {
  $("login").classList.remove("hidden");
  $("login-pw").focus();
}
$("login-form").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: { password: $("login-pw").value } });
    location.reload();
  } catch (err) { $("login-err").textContent = err.message; }
});
$("logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST", body: {} }); location.reload(); });

function fillDirections(sel) {
  clear(sel);
  for (const d of RT.meta.directions) sel.appendChild(el("option", { value: d.id }, d.name + (d.enabled ? "" : " (disabled)")));
  if (S.direction) sel.value = S.direction;
}
function setDirection(id) {
  S.direction = +id;
  for (const s of ["m-direction", "a-direction", "p-direction"]) $(s).value = S.direction;
}

function route() {
  const v = (location.hash || "#map").slice(1).split("?")[0];
  S.view = ["map", "analysis", "plan", "setup", "budget"].includes(v) ? v : "map";
  if (!RT.meta.directions.length && S.view !== "budget") S.view = "setup";
  document.querySelectorAll(".view").forEach(n => n.classList.toggle("active", n.id === "view-" + S.view));
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.view === S.view));
  if (S.view !== "map") stopTraffic();
  if (S.view === "map") enterMap();
  if (S.view === "analysis") loadAnalysis();
  if (S.view === "plan") initPlan();
  if (S.view === "setup") loadSetup();
  if (S.view === "budget") loadBudget();
}

function renderBanners() {
  const b = clear($("banners")), m = RT.meta;
  if (!m.directions.length) b.appendChild(el("div.banner.info", "No journey configured. Enter your private locations in Setup to start collecting routes."));
  if (m.mode === "demo") b.appendChild(el("div.banner.demo", "Demo mode: all data on this page is SYNTHETIC (generated, not from TomTom). Geometry is approximate and travel times are simulated."));
  if (m.demo_generating) b.appendChild(el("div.banner.info", "Generating demo history… reload in a minute."));
  if (!m.tomtom_key_configured) b.appendChild(el("div.banner.bad", "No TomTom API key is configured (TOMTOM_API_KEY). Nothing can be collected."));
  if (!m.collection_enabled) b.appendChild(el("div.banner.warn", "Scheduled collection is switched off. Missing slots are recorded as skipped."));
  else if (!m.collection_allowed) b.appendChild(el("div.banner.bad", `Collection paused: ${m.blocked_message}`));
  else if (m.budget_warning) b.appendChild(el("div.banner.warn", "Routing budget above the warning threshold. Automatic retries are disabled. See Budget."));
  if (m.pending_drafts) b.appendChild(el("div.banner.info", `${m.pending_drafts} corridor draft(s) wait for inspection in Setup. Drafts are not collected until you confirm them.`));
  if (m.open_deviations) b.appendChild(el("div.banner.warn", `${m.open_deviations} corridor observation(s) in the last 7 days deviated from their confirmed geometry. Inspect them in Setup.`));
}

/* ---------------- map view ---------------- */
function wireMap() {
  $("m-direction").addEventListener("change", e => { setDirection(e.target.value); S.mode === "current" ? loadCurrent() : loadDay(S.day, S.cycleTime); });
  $("m-current").addEventListener("click", () => setMode("current"));
  $("m-history").addEventListener("click", () => setMode("history"));
  $("m-date").addEventListener("change", e => loadDay(e.target.value));
  $("m-time").addEventListener("change", e => loadCycle(+e.target.value));
  $("m-prev").addEventListener("click", () => stepSlot(-1, false));
  $("m-next").addEventListener("click", () => stepSlot(1, false));
  $("m-prev-change").addEventListener("click", () => stepSlot(-1, true));
  $("m-next-change").addEventListener("click", () => stepSlot(1, true));
  $("m-traffic").addEventListener("change", e => e.target.checked ? startTraffic() : stopTraffic());
  document.addEventListener("visibilitychange", () => { if (document.hidden) pauseTraffic(); else if ($("m-traffic").checked && S.view === "map" && S.mode === "current") startTraffic(); });
  window.addEventListener("offline", pauseTraffic);
  window.addEventListener("online", () => { if ($("m-traffic").checked && S.view === "map" && S.mode === "current") startTraffic(); });
  const collect = $("m-collect");
  collect.classList.toggle("hidden", S.role !== "admin");
  collect.textContent = `Collect now (${RT.meta.requests_per_cycle} requests)`;
  collect.addEventListener("click", collectNow);
}

function enterMap() {
  if (!S.map) {
    S.map = makeMap($("map"));
    S.mapLayers = L.featureGroup().addTo(S.map);
    const mm = RT.meta.map;
    if (!mm.tiles_available) noteMap("No base map: map tiles are not configured. Routes are drawn from saved coordinates.");
    $("m-traffic").disabled = !(mm.tiles_available && mm.traffic_url);
  }
  setTimeout(() => S.map.invalidateSize(), 50);
  S.mode === "current" ? loadCurrent() : loadDay(S.day);
}
function noteMap(text) { const n = $("map-note"); n.textContent = text || ""; n.classList.toggle("hidden", !text); }

function setMode(mode) {
  S.mode = mode;
  $("m-current").classList.toggle("on", mode === "current");
  $("m-history").classList.toggle("on", mode === "history");
  $("m-history-controls").classList.toggle("hidden", mode !== "history");
  $("m-current-controls").classList.toggle("hidden", mode !== "current");
  if (mode === "history") {
    stopTraffic(); $("m-traffic").checked = false;
    loadDay(S.day || localDate());
  } else loadCurrent();
}

async function loadCurrent() {
  const r = await api(`/api/latest?direction_id=${S.direction}`);
  S.cycleId = r.detail ? r.detail.cycle.id : null;
  renderCycle(r.detail, { latest: r });
}

async function loadDay(day, keepTime) {
  S.day = day || localDate();
  $("m-date").value = S.day;
  const d = await api(`/api/day?direction_id=${S.direction}&date=${S.day}`);
  S.daySlots = d.slots;
  const sel = clear($("m-time"));
  d.slots.forEach(s => {
    const label = `${fmtTime(s.slot_utc)}${s.cycle_status === "complete" ? "" : " · " + statusLabel(s.cycle_status)}`;
    sel.appendChild(el("option", { value: s.cycle_id || "", disabled: !s.cycle_id }, label));
  });
  let target = d.slots.filter(s => s.cycle_id && s.cycle_status !== "future");
  let pick = null;
  if (S.cycleId) pick = target.find(s => s.cycle_id === S.cycleId);
  if (!pick) pick = target.filter(s => ["complete", "partial"].includes(s.cycle_status)).pop() || target.pop();
  drawTimeline();
  if (pick) loadCycle(pick.cycle_id); else renderCycle(null, { empty: `No observations recorded on ${S.day}.` });
}

function statusLabel(s) {
  return { complete: "complete", partial: "partial", failed: "failed", skipped: "skipped", missed: "missed", interrupted: "interrupted",
           no_record: "no record", future: "future", running: "running" }[s] || s;
}

function drawTimeline() {
  const wrap = $("m-timeline");
  const items = S.daySlots.map(s => {
    const ok = ["complete"].includes(s.cycle_status), part = s.cycle_status === "partial";
    const color = s.cycle_status === "future" ? "var(--surface-2)" : s.recommendation_changed ? "var(--accent)" : ok ? "#9aa0a6" : part ? "var(--warn)" : "var(--bad)";
    return { color, tooltip: [`${fmtTime(s.slot_utc)} · ${statusLabel(s.cycle_status)}`, s.cycle_reason || "", s.recommendation_changed ? "Recommendation changed" : ""].filter(Boolean) };
  });
  const sel = S.daySlots.findIndex(s => s.cycle_id === S.cycleId);
  bandChart(wrap, items, { onPick: i => S.daySlots[i].cycle_id && loadCycle(S.daySlots[i].cycle_id), selected: sel });
}

async function loadCycle(id) {
  if (!id) return;
  S.cycleId = id;
  $("m-time").value = id;
  const d = await api(`/api/cycles/${id}?direction_id=${S.direction}`);
  renderCycle(d, { history: true });
  if (S.daySlots) drawTimeline();
}

async function stepSlot(dir, changesOnly) {
  // Navigate within the day, crossing into adjacent days when needed.
  let slots = S.daySlots || [];
  let i = slots.findIndex(s => s.cycle_id === S.cycleId);
  for (let guard = 0; guard < 3; guard++) {
    for (let j = i + dir; j >= 0 && j < slots.length; j += dir) {
      const s = slots[j];
      if (!s.cycle_id) continue;
      if (changesOnly && !(dir > 0 ? s.recommendation_changed : (slots[j + 1] && slots[j + 1].recommendation_changed))) continue;
      if (changesOnly && dir < 0) { // land on the slot *after* the change boundary when going back
        const k = j + 1; if (slots[k] && slots[k].cycle_id) return loadCycle(slots[k].cycle_id);
      }
      return loadCycle(s.cycle_id);
    }
    const nd = addDays(S.day, dir);
    if (nd > localDate()) return toast("No later observations.");
    await loadDay(nd);
    slots = S.daySlots; i = dir > 0 ? -1 : slots.length;
  }
  toast(changesOnly ? "No recommendation change found nearby." : "No observation found.");
}

async function renderCycle(d, ctx) {
  S.mapLayers.clearLayers();
  const stamp = clear($("m-stamp")), flags = clear($("m-flags")), list = clear($("m-routes"));
  if (!d) {
    stamp.textContent = ctx.empty || "No observations recorded yet for this direction.";
    if (ctx.latest) flags.appendChild(el("span.badge.bad", "No data"));
    list.appendChild(el("div.card.small.muted", "When the scheduler has run, the latest stored routes appear here. Opening this page never triggers routing requests."));
    return;
  }
  const isHist = !!ctx.history;
  stamp.appendChild(document.createTextNode(isHist ? "Historical observation · " : "Latest observation · "));
  stamp.appendChild(el("span", fmtTime(d.intended_utc, true)));
  if (ctx.latest) stamp.appendChild(el("span.muted", ` (${fmtAge(ctx.latest.age_s)})`));
  if (RT.meta.mode === "demo") flags.appendChild(el("span.badge.demo", "Synthetic demo data"));
  if (d.cycle.trigger === "manual") flags.appendChild(el("span.badge.neutral", "Manual refresh"));
  if (ctx.latest && ctx.latest.stale) flags.appendChild(el("span.badge.bad", `Stale: older than ${RT.meta.stale_after_minutes} min`));
  if (d.cycle.status !== "complete") flags.appendChild(el("span.badge.warn", `Cycle ${statusLabel(d.cycle.status)}`));
  if (!d.comparable) flags.appendChild(el("span.badge.warn", `Requests ${Math.round(d.request_spread_s)} s apart (tolerance ${d.comparison_tolerance_s} s): not compared`));
  else if (d.routes.length > 1) flags.appendChild(el("span.badge.neutral", `Requests within ${Math.round(d.request_spread_s)} s`));
  if (isHist) flags.appendChild(el("span.badge.neutral", "No live traffic shown for past times"));

  const ok = d.routes.filter(r => r.status === "ok" && r.role !== "alternative" && r.travel_time_s !== null);
  const fastest = ok.length > 1 && d.comparable ? Math.min(...ok.map(r => r.travel_time_s)) : null;
  const bounds = [];
  // Draw: alternatives first (bottom), then corridors, then recommendation on top.
  const order = [...d.routes].sort((a, b) => (a.role === "alternative" ? 0 : a.role === "corridor" ? 1 : 2) - (b.role === "alternative" ? 0 : b.role === "corridor" ? 1 : 2));
  for (const r of order) {
    if (r.status !== "ok" || !r.variant_id) continue;
    const coords = await variantCoords(r.variant_id);
    if (!coords) continue;
    const dim = S.highlight && S.highlight !== (r.key || "alt" + r.id);
    const line = routeLine(coords, r.role === "alternative" ? "#868e96" : r.color,
      { weight: r.role === "recommended" ? 7 : r.role === "alternative" ? 3 : 5, dash: r.role === "alternative" ? "6 6" : null, opacity: dim ? 0.25 : 1 });
    line.bindTooltip(`${r.name}: ${fmtMin(r.travel_time_s)}, delay ${fmtDelay(r.traffic_delay_s)}`, { sticky: true });
    line.addTo(S.mapLayers);
    if (r.role === "recommended" && r.traffic_sections.length && !dim) sectionLines(coords, r.traffic_sections).addTo(S.mapLayers);
    bounds.push(...coords);
    const ch = d.changes[r.key];
    if (ch && ch.geometry_changed && ch.previous_variant_id && S.highlight === r.key) {
      const prev = await variantCoords(ch.previous_variant_id);
      if (prev) routeLine(prev, "#495057", { weight: 3, dash: "2 6" }).addTo(S.mapLayers);
    }
  }
  const dir = RT.meta.directions.find(x => x.id === S.direction);
  if (dir) {
    L.circleMarker(dir.from, { radius: 7, color: "#fff", weight: 2, fillColor: "#0b0b0b", fillOpacity: 1 }).bindTooltip(dir.from_label).addTo(S.mapLayers);
    L.circleMarker(dir.to, { radius: 7, color: "#fff", weight: 2, fillColor: "#e34948", fillOpacity: 1 }).bindTooltip(dir.to_label).addTo(S.mapLayers);
    bounds.push(dir.from, dir.to);
  }
  if (bounds.length && !S.keepView) S.map.fitBounds(L.latLngBounds(bounds), { padding: [30, 30] });
  S.keepView = false;
  noteMap(isHist && d.routes.some(r => r.traffic_sections && r.traffic_sections.length)
    ? "Red segments: traffic sections TomTom reported for the recommendation at this observation time."
    : (!RT.meta.map.tiles_available ? "No base map configured. Routes are drawn from saved coordinates." : ""));

  for (const r of d.routes.filter(r => r.role !== "alternative")) list.appendChild(routeCard(r, d, fastest));
  const alts = d.routes.filter(r => r.role === "alternative");
  if (alts.length) {
    const det = el("details.card.small", el("summary", `${alts.length} extra alternative(s) returned with the unrestricted request (grey dashed on the map)`));
    for (const a of alts) det.appendChild(el("div", { style: { marginTop: "6px" } },
      el("b", a.status === "ok" ? fmtMin(a.travel_time_s) : "no data"), ` · delay ${fmtDelay(a.traffic_delay_s)} · ${fmtKm(a.distance_m)} · `,
      a.resembles ? `resembles ${a.resembles.name}` : "no matching corridor"));
    det.appendChild(el("p.muted", "Kept for reference. They are not substitutes for the monitored corridors."));
    list.appendChild(det);
  }
  for (const m of d.missing) {
    list.appendChild(el("div.route.failed", el("div.swatch", { style: { background: m.color } }),
      el("div", el("div.name", m.name),
        el("div.small", { style: { color: "var(--bad)" } }, "Missing: no observation stored for this route in this cycle."))));
  }
}

function routeCard(r, d, fastest) {
  const key = r.key || "alt" + r.id;
  const card = el("div.route" + (r.status !== "ok" ? ".failed" : "") + (S.highlight && S.highlight !== key ? ".dim" : ""),
    { onclick: () => { S.highlight = S.highlight === key ? null : key; S.keepView = true; renderCycle(d, S.mode === "current" ? { latest: { age_s: null } } : { history: true }); } });
  card.appendChild(el("div.swatch", { style: { background: r.role === "alternative" ? "#868e96" : r.color } }));
  const body = el("div");
  const kind = r.role === "recommended" ? "Unrestricted fastest" : r.role === "alternative" ? "Extra alternative from the unrestricted request" :
    (r.corridor_method === "reconstruct" ? "Corridor · exact geometry" : "Corridor · via points (small variations allowed)");
  body.appendChild(el("div.row", el("span.name", r.name), el("span.spacer"), el("span.small.muted", kind)));
  if (r.status !== "ok") {
    body.appendChild(el("div.small", { style: { color: "var(--bad)" } }, "No data: " + (r.failure_reason || "request failed")));
    body.appendChild(el("div.small.muted", "Failed and missing observations are kept and never shown as zero."));
  } else {
    body.appendChild(el("div.row", el("span.big", fmtMin(r.travel_time_s)), el("span.muted", "estimated"),
      fastest !== null && r.role !== "alternative" && r.travel_time_s !== fastest ? el("span.badge.neutral", fmtSigned(r.travel_time_s - fastest) + " vs fastest") : null,
      fastest !== null && r.role !== "alternative" && r.travel_time_s === fastest ? el("span.badge.ok", "Fastest in this cycle") : null));
    const mt = el("div.metrics");
    const add = (l, v, t) => mt.appendChild(el("div", { title: t || "" }, el("span", l), v));
    add("Traffic delay", fmtDelay(r.traffic_delay_s), "TomTom's trafficDelayInSeconds field");
    add("Above free flow", fmtSigned(r.excess_over_free_flow_s), "Estimated time minus no-traffic time");
    add("Distance", fmtKm(r.distance_m));
    add("Free flow", fmtMin(r.free_flow_s), "noTrafficTravelTimeInSeconds");
    add("Historic traffic", fmtMin(r.historic_s), "historicTrafficTravelTimeInSeconds");
    add("Live incidents", fmtMin(r.live_s), "liveTrafficIncidentsTravelTimeInSeconds");
    add("Traffic length", r.traffic_length_m === null ? "—" : fmtKm(r.traffic_length_m));
    add("Depart / arrive", `${fmtOffsetTime(r.departure_time)} → ${fmtOffsetTime(r.arrival_time)}`, "TomTom departure and arrival times");
    body.appendChild(mt);
    const tags = el("div.tags");
    if (r.resembles) tags.appendChild(el("span.badge.neutral", `Resembles ${r.resembles.name} (${fmtPct(r.resembles.score)})`));
    else if (r.role === "recommended") tags.appendChild(el("span.badge.neutral", "Does not match a confirmed corridor"));
    if (r.deviation_flag) tags.appendChild(el("span.badge.bad", `Deviates from corridor: ${fmtPct(r.deviation_share)} off, max ${r.deviation_max_m} m`));
    const ch = d.changes[r.key];
    if (ch && ch.route_changed) tags.appendChild(el("span.badge.warn", "Recommendation changed since previous cycle"));
    else if (ch && ch.geometry_changed) tags.appendChild(el("span.badge.neutral", "Geometry changed since previous cycle (click to compare)"));
    if (tags.childNodes.length) body.appendChild(tags);
  }
  card.appendChild(body);
  return card;
}

async function collectNow() {
  if (!confirm(`This makes up to ${RT.meta.requests_per_cycle} routing requests now, counted against the budget. Continue?`)) return;
  const btn = $("m-collect"); btn.disabled = true;
  try {
    const r = await api("/api/collect", { method: "POST", body: {} });
    toast(`Manual cycle ${r.status}.`);
    RT.meta = await api("/api/meta"); renderBanners();
    loadCurrent();
  } catch (e) { toast(e.message, 6000); }
  finally { btn.disabled = false; }
}

/* Live traffic overlay: off by default, current view only, >= 5 min refresh, paused when hidden or offline. */
function startTraffic() {
  const mm = RT.meta.map;
  if (!mm.traffic_url || S.mode !== "current" || document.hidden || !navigator.onLine) return;
  const bucket = () => Math.floor(Date.now() / (mm.traffic_refresh_minutes * 60000));
  const url = () => tileUrl(mm.traffic_url) + (mm.traffic_url.includes("?") || RT.embedToken ? "&" : "?") + "v=" + bucket();
  if (!S.trafficLayer) S.trafficLayer = L.tileLayer(url(), { maxZoom: 19, opacity: 0.85, attribution: "Traffic © TomTom" });
  S.trafficLayer.setUrl(url());
  S.trafficLayer.addTo(S.map);
  clearInterval(S.trafficTimer);
  S.trafficTimer = setInterval(() => {
    if (document.hidden || !navigator.onLine || S.view !== "map" || S.mode !== "current") return pauseTraffic();
    S.trafficLayer.setUrl(url());
  }, mm.traffic_refresh_minutes * 60000);
  noteMap(`Live traffic now (refreshes every ${mm.traffic_refresh_minutes} min while visible). It shows current conditions only.`);
}
function pauseTraffic() { clearInterval(S.trafficTimer); S.trafficTimer = null; }
function stopTraffic() {
  pauseTraffic();
  if (S.trafficLayer && S.map) S.map.removeLayer(S.trafficLayer);
  if ($("m-traffic")) $("m-traffic").checked = false;
}

/* ---------------- analysis ---------------- */
function wireAnalysis() {
  const wd = $("a-weekdays");
  WEEKDAYS.forEach((n, i) => wd.appendChild(el("label", el("input", { type: "checkbox", value: i, checked: true }), n)));
  const today = localDate();
  $("a-from").value = addDays(today, -27); $("a-to").value = today; $("a-day").value = today;
  document.querySelectorAll("[data-preset]").forEach(b => b.addEventListener("click", () => {
    $("a-from").value = addDays(localDate(), -(+b.dataset.preset - 1)); $("a-to").value = localDate(); loadAnalysis();
  }));
  $("a-apply").addEventListener("click", loadAnalysis);
  $("a-direction").addEventListener("change", e => { setDirection(e.target.value); loadAnalysis(); });
  $("a-day").addEventListener("change", loadDayCharts);
  for (const id of ["h-key", "h-metric", "h-bucket"]) $(id).addEventListener("change", loadHeatmap);
}

function analysisQuery() {
  const days = [...document.querySelectorAll("#a-weekdays input:checked")].map(i => i.value);
  const p = new URLSearchParams({ direction_id: S.direction, from: $("a-from").value, to: $("a-to").value });
  if (days.length < 7) p.set("weekdays", days.join(","));
  if ($("a-tfrom").value) p.set("time_from", $("a-tfrom").value);
  if ($("a-tto").value) p.set("time_to", $("a-tto").value);
  return p;
}

async function loadAnalysis() {
  const st = await api("/api/stats?" + analysisQuery());
  const cs = st.cycle_status;
  const sum = clear($("a-summary"));
  sum.appendChild(document.createTextNode(`${st.expected_slots} expected slots · ${cs.complete || 0} complete · ${cs.partial || 0} partial · ` +
    `${(cs.failed || 0) + (cs.skipped || 0) + (cs.missed || 0) + (cs.interrupted || 0) + (cs.no_record || 0)} without data · ` +
    `${st.comparable_cycles} cycles comparable · recommendation changed ${st.recommendation_changes} times in ${st.recommendation_transitions} transitions`));
  if (RT.meta.mode === "demo") sum.appendChild(el("span.badge.demo", { style: { marginLeft: "6px" } }, "Synthetic"));
  const t = clear($("a-table"));
  t.appendChild(el("thead", el("tr", ["Route", "Coverage", "Obs.", "Failed", "Missing", "Median", "Mean", "90th pct", "Median delay", "90th pct delay", "Median above free flow", "Fastest*"]
    .map((h, i) => el("th" + (i ? ".num" : ""), h)))));
  const tb = el("tbody");
  for (const r of st.routes) {
    tb.appendChild(el("tr",
      el("td", el("span", { style: { display: "inline-block", width: "12px", height: "3px", background: r.color, marginRight: "6px", verticalAlign: "middle" } }), r.name),
      el("td.num", fmtPct(r.coverage)), el("td.num", r.ok), el("td.num", r.failed), el("td.num", r.not_collected),
      el("td.num", fmtMin(r.travel_time_s.median)), el("td.num", fmtMin(r.travel_time_s.mean)), el("td.num", fmtMin(r.travel_time_s.p90)),
      el("td.num", fmtDelay(r.traffic_delay_s.median)), el("td.num", fmtDelay(r.traffic_delay_s.p90)), el("td.num", fmtSigned(r.excess_over_free_flow_s.median)),
      el("td.num", { title: `${r.fastest_count} of ${r.fastest_basis} compared cycles; ${r.no_data_in_compared_cycles} compared cycles had no data for this route` },
        r.fastest_share === null ? "—" : `${fmtPct(r.fastest_share)} (${r.fastest_count})`)));
  }
  t.appendChild(tb);
  $("a-method").textContent = st.method;
  const hk = $("h-key"), prev = hk.value;
  clear(hk);
  for (const r of st.routes) hk.appendChild(el("option", { value: r.key }, r.name));
  hk.value = prev && [...hk.options].some(o => o.value === prev) ? prev : "recommended";
  loadDayCharts(); loadHeatmap();
}

async function loadDayCharts() {
  const d = await api(`/api/day?direction_id=${S.direction}&date=${$("a-day").value}`);
  const slots = d.slots.filter(s => s.cycle_status !== "future");
  const xs = slots.map(s => Date.parse(s.slot_utc));
  const mk = metric => d.routes.map(r => ({ name: r.name, color: r.color, values: slots.map(s => { const o = s.routes[r.key]; return o && o.status === "ok" ? o[metric] : null; }) }));
  const leg = clear($("a-legend-day"));
  d.routes.forEach(r => leg.appendChild(el("span", el("i", { style: { background: r.color } }), r.name)));
  leg.appendChild(el("span.muted", "Gaps = no successful observation"));
  const xf = (t, long) => fmtTime(new Date(t).toISOString(), long);
  const pickCycle = i => { if (slots[i].cycle_id) { S.cycleId = slots[i].cycle_id; S.day = d.date; location.hash = "#map"; setTimeout(() => setMode("history"), 0); } };
  lineChart($("a-chart-travel"), { xs, series: mk("travel_time_s"), yFormat: v => fmtMin(v), xFormat: xf, onPick: pickCycle, unit: 60 });
  lineChart($("a-chart-delay"), { xs, series: mk("traffic_delay_s"), yFormat: v => fmtDelay(Math.round(v)), xFormat: xf, height: 160, yZero: true, onPick: pickCycle, unit: 60 });
  // Recommendation timeline: colour by the corridor the recommendation resembles.
  const colorOf = new Map(d.routes.map(r => [r.key, r.color]));
  const items = slots.map(s => {
    const rec = s.routes.recommended;
    if (!rec || rec.status !== "ok") return { color: "var(--bad)", hatch: true, tooltip: [fmtTime(s.slot_utc), "No recommendation stored: " + statusLabel(s.cycle_status)] };
    const rc = s.recommendation && s.recommendation.resembles_corridor_id;
    const col = rc ? colorOf.get("corridor:" + rc) || "#868e96" : "#2a78d6";
    const name = rc ? (d.routes.find(r => r.key === "corridor:" + rc) || {}).name : "Unmatched route (not a confirmed corridor)";
    return { color: col, tooltip: [fmtTime(s.slot_utc), name, `${fmtMin(rec.travel_time_s)} estimated`, s.recommendation_changed ? "Changed here" : ""].filter(Boolean) };
  });
  bandChart($("a-rec-timeline"), items, { onPick: pickCycle });
  const rl = clear($("a-rec-legend"));
  rl.appendChild(el("span", el("i.box", { style: { background: "#2a78d6" } }), "Unmatched route"));
  d.routes.filter(r => r.kind === "corridor").forEach(r => rl.appendChild(el("span", el("i.box", { style: { background: r.color } }), "Resembles " + r.name)));
  rl.appendChild(el("span", el("i.box", { style: { background: "var(--bad)", opacity: .35 } }), "No data"));
}

async function loadHeatmap() {
  const p = analysisQuery();
  const q = new URLSearchParams({ direction_id: S.direction, key: $("h-key").value || "recommended", from: p.get("from"), to: p.get("to"),
    metric: $("h-metric").value, bucket: $("h-bucket").value });
  const h = await api("/api/heatmap?" + q);
  heatmapChart($("a-heatmap"), h, h.metric === "traffic_delay_s" ? v => fmtDelay(Math.round(v)) : v => fmtMin(v));
}

/* ---------------- plan ---------------- */
function wirePlan() {
  $("p-direction").addEventListener("change", e => setDirection(e.target.value));
  $("p-go").addEventListener("click", runPlan);
  $("p-live").addEventListener("click", liveCheck);
}
function initPlan() {
  if (!$("p-date").value) $("p-date").value = addDays(localDate(), 1);
  $("p-live").textContent = `Check live now (uses up to ${RT.meta.requests_per_cycle} requests for both directions)`;
  $("p-live-card").classList.toggle("hidden", S.role !== "admin");
}

async function runPlan() {
  const q = new URLSearchParams({ direction_id: S.direction, date: $("p-date").value, arrive: $("p-arrive").value, pct: $("p-pct").value,
    weeks: $("p-weeks").value, same_weekday: $("p-match").value === "1" });
  const out = clear($("p-result"));
  let r;
  try { r = await api("/api/plan?" + q); } catch (e) { out.appendChild(el("div.card", e.message)); return; }
  const card = el("div.card");
  out.appendChild(card);
  if (RT.meta.mode === "demo") card.appendChild(el("p", el("span.badge.demo", "Based on synthetic demo data")));
  if (r.state === "insufficient_data") {
    card.appendChild(el("h2", "Not enough observations yet"));
    card.appendChild(el("p", `No departure slot has at least ${r.min_samples} successful observations on comparable days in the last ${r.history.weeks} weeks. ` +
      "Keep collecting; recommendations become useful as weeks of history accumulate."));
  } else if (r.state === "no_feasible_departure") {
    card.appendChild(el("h2", "No departure in the window meets the target"));
    card.appendChild(el("p", "None of the observed departure slots in the three hours before your target arrives on time at this reliability level. Try an earlier search window, a lower reliability level, or check data coverage."));
  } else if (r.state === "invalid") {
    card.appendChild(el("p", r.message)); return;
  } else {
    const rec = r.recommendation;
    card.appendChild(el("h2", `Leave by ${fmtTime(rec.departure_local)}, ${rec.key === "recommended" ? "following TomTom's recommendation" : "via " + rec.name}`));
    card.appendChild(el("p", `To arrive by ${fmtTime(r.target_arrival_local)} on ${r.percentile}% of comparable observed days. ` +
      `Median arrival ${fmtTime(rec.median_arrival_local)}, ${r.percentile}th-percentile arrival ${fmtTime(rec.quantile_arrival_local)} ` +
      `(margin ${Math.round(rec.margin_s / 60)} min). Based on ${rec.n} observations at this slot.`));
  }
  card.appendChild(el("p.small.muted", `History: ${r.history.distinct_dates} distinct dates (${r.history.matching}), ${fmtTime(r.history.from, true)} – ${fmtTime(r.history.to, true)}. ${r.method}`));
  for (const n of r.notes) card.appendChild(el("p.banner.warn", n));
  const tbl = el("table");
  tbl.appendChild(el("thead", el("tr", el("th", "Departure"), ...r.routes.map(x => el("th.num", x.name)))));
  const tb = el("tbody");
  const n = r.routes[0] ? r.routes[0].slots.length : 0;
  for (let i = n - 1; i >= 0; i--) {
    const s0 = r.routes[0].slots[i];
    const tr = el("tr", el("td", fmtTime(s0.departure_local)));
    for (const x of r.routes) {
      const s = x.slots[i];
      const isRec = r.recommendation && r.recommendation.key === x.key && r.recommendation.minute === s.minute;
      tr.appendChild(el("td.num", { title: `${s.n} observations, ${s.failed} failed/missing at this slot` },
        s.quantile_s === undefined ? `insufficient (${s.n})` :
          `${fmtTime(s.quantile_arrival_local)}${s.meets_target ? " ✓" : ""} · n=${s.n}`));
      if (isRec) tr.classList.add("highlight");
    }
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  const c2 = el("div.card", el("h3", `${r.percentile}th-percentile arrival per departure slot`), el("p.small.muted", "✓ = arrives by the target at the chosen reliability. Earlier rows show whether leaving 15 or 30 minutes earlier usually helps."), el("div.table-wrap", tbl));
  out.appendChild(c2);
}

async function liveCheck() {
  if (!confirm(`This makes up to ${RT.meta.requests_per_cycle} routing requests now. Continue?`)) return;
  const box = clear($("p-live-result"));
  try {
    const r = await api("/api/collect", { method: "POST", body: { direction_ids: [S.direction] } });
    const d = await api(`/api/cycles/${r.cycle_id}?direction_id=${S.direction}`);
    box.appendChild(el("p.small", el("b", "Live estimate now"), ` (${fmtTime(d.intended_utc, true)}), separate from the historical plan:`));
    for (const x of d.routes.filter(x => x.role !== "alternative"))
      box.appendChild(el("div.small", `${x.name}: ${x.status === "ok" ? fmtMin(x.travel_time_s) + ", delay " + fmtDelay(x.traffic_delay_s) : "no data (" + x.failure_reason + ")"}`));
  } catch (e) { box.appendChild(el("p.small", { style: { color: "var(--bad)" } }, e.message)); }
}

/* ---------------- setup ---------------- */
async function loadSetup() {
  const data = await api("/api/setup");
  S.setup = data;
  if (!S.setupMap) {
    S.setupMap = makeMap($("setup-map"));
    S.setupLayers = L.featureGroup().addTo(S.setupMap);
    S.setupMap.on("click", onSetupMapClick);
  }
  setTimeout(() => S.setupMap.invalidateSize(), 50);
  renderJourney(); renderCorridors(); renderVariants();
  drawSetup();
}
const isAdmin = () => S.role === "admin";

function renderJourney() {
  const j = S.setup.journeys[0];
  const box = clear($("s-journey"));
  box.appendChild(el("h2", j ? "Journey" : "Create your journey"));
  if (!j && !isAdmin()) {
    box.appendChild(el("p.small.muted", "An administrator must configure a journey before routes can be collected."));
    return;
  }
  const values = j || { name: "Home ↔ Work", origin_label: "Home", dest_label: "Work", origin_lat: "", origin_lon: "", dest_lat: "", dest_lon: "" };
  const f = (id, label, val, type = "text") => el("div.field", el("label", { for: id }, label), el("input", { id, value: val, type, step: "any", disabled: !isAdmin() }));
  box.appendChild(f("j-name", "Name", values.name));
  box.appendChild(el("div.row", f("j-ol", "Origin label", values.origin_label), f("j-dl", "Destination label", values.dest_label)));
  box.appendChild(el("div.row", f("j-olat", "Origin latitude", values.origin_lat, "number"), f("j-olon", "Origin longitude", values.origin_lon, "number")));
  box.appendChild(el("div.row", f("j-dlat", "Destination latitude", values.dest_lat, "number"), f("j-dlon", "Destination longitude", values.dest_lon, "number")));
  box.appendChild(el("p.small.muted", j ? "Coordinate changes affect future observations only. Each observation keeps the request parameters it was made with." : "Your locations are saved only in this installation's private database. Creating a journey enables collection in both directions and makes no immediate routing request."));
  const dirs = el("div.row");
  for (const d of S.setup.directions.filter(x => j && x.journey_id === j.id))
    dirs.appendChild(el("label.row", { style: { gap: "6px" } }, el("input", { type: "checkbox", checked: !!d.enabled, disabled: !isAdmin(),
      onchange: async e => { await api(`/api/directions/${d.id}`, { method: "PUT", body: { enabled: e.target.checked } }); toast("Saved"); refreshMeta(); } }), "Collect " + d.name));
  box.appendChild(dirs);
  if (isAdmin()) box.appendChild(el("button.primary", { style: { marginTop: "8px" }, onclick: async () => {
    try {
      for (const id of ["j-olat", "j-olon", "j-dlat", "j-dlon"]) {
        if (!$(id).value.trim()) throw new Error("Enter all four coordinates.");
      }
      await api(j ? `/api/journeys/${j.id}` : "/api/journeys", { method: j ? "PUT" : "POST", body: { name: $("j-name").value, origin_label: $("j-ol").value, dest_label: $("j-dl").value,
        origin_lat: $("j-olat").value, origin_lon: $("j-olon").value, dest_lat: $("j-dlat").value, dest_lon: $("j-dlon").value, ...(!j ? { enabled: true } : {}) } });
      toast("Journey saved"); await refreshMeta(); await loadSetup();
    } catch (e) { toast(e.message); }
  } }, j ? "Save journey" : "Create journey"));
}

async function refreshMeta() {
  RT.meta = await api("/api/meta");
  if (!RT.meta.directions.some(d => d.id === S.direction)) S.direction = RT.meta.directions[0]?.id ?? null;
  for (const sel of ["m-direction", "a-direction", "p-direction"]) fillDirections($(sel));
  $("m-collect").textContent = `Collect now (${RT.meta.requests_per_cycle} requests)`;
  renderBanners();
}

function renderCorridors() {
  const box = clear($("s-corridors"));
  box.appendChild(el("h2", "Corridors"));
  box.appendChild(el("p.small.muted", "A corridor is a named route you confirm. 'Via points' corridors let TomTom vary the route slightly between your ordered points; " +
    "'exact geometry' corridors rebuild a saved route with TomTom supporting points. Drafts are never collected. Preview uses one routing request."));
  const sel = el("select", { onchange: e => { setDirection(e.target.value); renderCorridors(); renderVariants(); drawSetup(); } });
  for (const d of S.setup.directions) sel.appendChild(el("option", { value: d.id }, d.name));
  sel.value = S.direction;
  box.appendChild(sel);
  const d = S.setup.directions.find(x => x.id === S.direction);
  if (!d) return;
  for (const c of d.corridors) {
    const st = c.status === "confirmed" ? el("span.badge.ok", "✓ Confirmed") : c.status === "draft" ? el("span.badge.warn", c.source === "suggested" ? "Draft · suggested, unconfirmed" : "Draft") : el("span.badge.neutral", "Retired");
    const row = el("div.corridor",
      el("div.row", el("span", { style: { width: "14px", height: "4px", background: c.color, borderRadius: "2px" } }), el("b", c.name), el("span.spacer"), st),
      el("div.small.muted", `${c.method === "reconstruct" ? `Exact geometry (${c.supporting_points_count} supporting points)` : `Via points: ${c.waypoints.map(p => p.map(v => (+v).toFixed(4)).join(", ")).join(" → ")}`}` +
        ` · ${c.observations} observations · ${c.deviations} deviations · threshold ${c.deviation_threshold_m} m`),
      c.note ? el("div.small", c.note) : null);
    const act = el("div.actions");
    act.appendChild(el("button.small", { onclick: () => { S.focusCorridor = c.id; drawSetup(); } }, "Show on map"));
    if (isAdmin() && c.status === "draft") {
      act.appendChild(el("button.small", { onclick: () => preview(c) }, c.method === "waypoints" ? "Preview (1 request)" : "Preview again (1 request)"));
      act.appendChild(el("button.small.primary", { disabled: !c.preview_variant_id, title: c.preview_variant_id ? "" : "Preview first",
        onclick: async () => { if (!confirm(`Confirm '${c.name}' with the previewed geometry as its reference?`)) return; await api(`/api/corridors/${c.id}/confirm`, { method: "POST", body: {} }); toast("Confirmed"); await refreshMeta(); loadSetup(); } }, "Confirm"));
      if (c.method === "waypoints") act.appendChild(el("button.small", { onclick: () => startDraft(c) }, "Edit via points"));
    }
    if (isAdmin() && c.status !== "retired") act.appendChild(el("button.small", { onclick: async () => { const n = prompt("Name", c.name); if (n) { await api(`/api/corridors/${c.id}`, { method: "PUT", body: { name: n } }); loadSetup(); } } }, "Rename"));
    if (isAdmin() && c.status === "confirmed") act.appendChild(el("button.small.danger", { onclick: async () => { if (confirm("Retire this corridor? Its history is kept, collection stops.")) { await api(`/api/corridors/${c.id}/retire`, { method: "POST", body: {} }); await refreshMeta(); loadSetup(); } } }, "Retire"));
    if (isAdmin() && c.observations === 0) act.appendChild(el("button.small.danger", { onclick: async () => { if (confirm("Delete this draft?")) { await api(`/api/corridors/${c.id}`, { method: "DELETE" }); await refreshMeta(); loadSetup(); } } }, "Delete"));
    row.appendChild(act);
    box.appendChild(row);
  }
  if (isAdmin()) {
    box.appendChild(el("div.row", { style: { marginTop: "10px" } },
      el("button", { onclick: () => startDraft(null) }, "New corridor from via points")));
    if (S.draft) {
      box.appendChild(el("div.banner.info", { style: { marginTop: "8px" } },
        `Click the map to add via points in driving order (${S.draft.points.length} so far). Drag markers to adjust. Place points on the road you mean, away from junctions.`));
      box.appendChild(el("div.row", { style: { marginTop: "6px" } },
        el("input", { id: "draft-name", placeholder: "Corridor name", value: S.draft.name || "" }),
        el("button.primary", { onclick: saveDraft, disabled: !S.draft.points.length }, "Save draft"),
        el("button", { onclick: () => { S.draft.points.pop(); renderCorridors(); drawSetup(); } }, "Undo point"),
        el("button", { onclick: () => { S.draft = null; renderCorridors(); drawSetup(); } }, "Cancel")));
    }
  }
}

async function renderVariants() {
  const box = clear($("s-variants"));
  box.appendChild(el("h2", "Routes TomTom returned"));
  if (!S.setup.directions.length) {
    box.appendChild(el("p.small.muted", "Create a journey first. Routes will appear after collection starts."));
    return;
  }
  box.appendChild(el("p.small.muted", "Distinct geometries from unrestricted requests (recommendations and extra alternatives). You can turn one into an exact-geometry corridor draft without a new request."));
  const v = await api(`/api/variants?direction_id=${S.direction}&key=unrestricted`);
  if (!v.variants.length) { box.appendChild(el("p.small", "None yet.")); return; }
  const t = el("table", el("thead", el("tr", el("th", "#"), el("th", "Roads"), el("th.num", "Length"), el("th.num", "Seen"), el("th", "Last seen"), el("th", ""))));
  const tb = el("tbody");
  for (const x of v.variants.slice(0, 15)) {
    tb.appendChild(el("tr",
      el("td", x.id), el("td", { title: x.roads.join(", ") }, x.roads.slice(0, 4).join(", ") || "—"), el("td.num", fmtKm(x.length_m)), el("td.num", x.n), el("td", fmtTime(x.last_utc, true)),
      el("td", el("button.small", { onclick: () => { S.focusVariant = x.id; drawSetup(); } }, "Show"),
        isAdmin() ? el("button.small", { onclick: async () => { const n = prompt("Name for the new corridor draft", (x.roads.slice(0, 3).join(" / ") || "Route " + x.id)); if (!n) return; await api("/api/corridors/from-variant", { method: "POST", body: { variant_id: x.id, name: n } }); toast("Draft created"); await refreshMeta(); loadSetup(); } }, "Use as corridor") : null)));
  }
  t.appendChild(tb);
  box.appendChild(el("div.table-wrap", t));
}

async function drawSetup() {
  if (!S.setupLayers) return;
  S.setupLayers.clearLayers();
  const d = S.setup.directions.find(x => x.id === S.direction);
  if (!d) return;
  const b = [d.from, d.to];
  L.circleMarker(d.from, { radius: 8, color: "#fff", weight: 2, fillColor: "#0b0b0b", fillOpacity: 1 }).bindTooltip(d.from_label + " (start)").addTo(S.setupLayers);
  L.circleMarker(d.to, { radius: 8, color: "#fff", weight: 2, fillColor: "#e34948", fillOpacity: 1 }).bindTooltip(d.to_label + " (end)").addTo(S.setupLayers);
  let note = "";
  for (const c of d.corridors) {
    if (c.status === "retired") continue;
    const focus = !S.focusCorridor || S.focusCorridor === c.id;
    const vid = c.reference_variant_id || c.preview_variant_id;
    if (vid) {
      const coords = await variantCoords(vid);
      routeLine(coords, c.color, { weight: focus ? 5 : 3, opacity: focus ? 1 : .35, dash: c.status === "draft" ? "8 6" : null })
        .bindTooltip(`${c.name} (${c.status === "draft" ? "preview of draft" : "confirmed reference"})`, { sticky: true }).addTo(S.setupLayers);
      if (focus) b.push(...coords);
    }
    c.waypoints.forEach((p, i) => L.marker(p, { title: `${c.name}: via point ${i + 1}`, opacity: focus ? 1 : .5 }).bindTooltip(`${c.name}: via ${i + 1}`).addTo(S.setupLayers));
    if (focus && S.focusCorridor) note = c.status === "draft" ? (vid ? "Dashed: previewed draft geometry. Confirm only if this is the route you mean." : "Not previewed yet: only the via points are shown.") : "Solid: the confirmed reference geometry.";
  }
  if (S.focusVariant) {
    const coords = await variantCoords(S.focusVariant);
    routeLine(coords, "#2a78d6", { weight: 6 }).bindTooltip(`Route variant ${S.focusVariant}`).addTo(S.setupLayers);
    b.push(...coords); note = `Route variant ${S.focusVariant} as returned by TomTom.`;
    S.focusVariant = null;
  }
  if (S.draft) {
    S.draft.points.forEach((p, i) => {
      const m = L.marker(p, { draggable: true, title: `New via point ${i + 1}` }).addTo(S.setupLayers);
      m.on("dragend", e => { const ll = e.target.getLatLng(); S.draft.points[i] = [ll.lat, ll.lng]; });
    });
    note = "Drafting: click to add via points.";
  }
  $("setup-note").textContent = note || "Pick 'Show on map' to inspect a corridor.";
  S.setupMap.fitBounds(L.latLngBounds(b), { padding: [30, 30] });
  S.focusCorridor = null;
}

function startDraft(c) {
  S.draft = c ? { id: c.id, name: c.name, points: c.waypoints.map(p => [...p]) } : { id: null, name: "", points: [] };
  renderCorridors(); drawSetup();
}
function onSetupMapClick(e) {
  if (!S.draft) return;
  S.draft.points.push([+e.latlng.lat.toFixed(6), +e.latlng.lng.toFixed(6)]);
  S.draft.name = $("draft-name") ? $("draft-name").value : S.draft.name;
  renderCorridors(); drawSetup();
}
async function saveDraft() {
  const name = $("draft-name").value.trim();
  if (!name) return toast("Give the corridor a name");
  try {
    if (S.draft.id) await api(`/api/corridors/${S.draft.id}`, { method: "PUT", body: { name, waypoints: S.draft.points } });
    else {
      const used = new Set(S.setup.directions.find(x => x.id === S.direction).corridors.map(c => c.color));
      await api("/api/corridors", { method: "POST", body: { direction_id: S.direction, name, waypoints: S.draft.points, color: S.setup.colors.find(c => !used.has(c)) } });
    }
    S.draft = null; toast("Draft saved. Preview it to see the route."); await refreshMeta(); loadSetup();
  } catch (e) { toast(e.message); }
}
async function preview(c) {
  if (!confirm("Preview makes one routing request, counted against the budget. Continue?")) return;
  try {
    const r = await api(`/api/corridors/${c.id}/preview`, { method: "POST", body: {} });
    toast(`Preview: ${fmtMin(r.travel_time_s)}, ${fmtKm(r.distance_m)} via ${r.roads.slice(0, 4).join(", ")}`, 6000);
    await loadSetup(); S.focusCorridor = c.id; drawSetup();
  } catch (e) { toast("Preview failed: " + e.message, 6000); }
}

/* ---------------- budget ---------------- */
async function loadBudget() {
  const b = await api("/api/budget");
  const box = clear($("b-content"));
  const r = b.routing, f = b.forecast, p = b.period;
  const c1 = el("div.card");
  c1.appendChild(el("h2", `Routing requests · ${p.key}`));
  c1.appendChild(el("div.kpis",
    kpi(r.used, `used (opening ${r.opening_usage} + ledger ${r.ledger_counted})`),
    kpi(r.ceiling, "application ceiling"),
    kpi(r.remaining_to_ceiling, "remaining to ceiling"),
    kpi(f.remaining_scheduled_requests, `scheduled until reset (${f.requests_per_cycle}/cycle)`),
    kpi(f.projected_period_total, "projected period total")));
  const pct = r.ceiling ? Math.min(100, r.used / r.ceiling * 100) : 100;
  const bar = el("div.bar", { style: { marginTop: "12px" } }, el("div", { style: { width: pct + "%", background: r.warning ? "var(--warn)" : "var(--accent)" } }),
    el("i", { style: { left: r.warning_percent + "%" }, title: "warning threshold" }));
  c1.appendChild(bar);
  c1.appendChild(el("p.small", b.collection_allowed ? el("span.badge.ok", "✓ Collection allowed") : el("span.badge.bad", "Paused: " + b.blocked_message),
    f.projected_to_exceed_ceiling ? el("span.badge.warn", { style: { marginLeft: "6px" } }, `At this rate collection pauses around ${fmtTime(f.projected_pause_at_utc, true)}`) : null));
  c1.appendChild(el("p.small.muted", `Allowance ${r.allowance} − safety reserve ${r.safety_reserve} − other applications ${r.other_apps_allowance} = ceiling ${r.ceiling}` +
    (r.hard_ceiling_override ? ` (overridden: ${r.hard_ceiling_override})` : "") + `. A full period at the current plan needs about ${f.nominal_full_period_requests} requests.`));
  c1.appendChild(el("p.small.muted", `Period ${fmtTime(p.start_utc, true)} – ${fmtTime(p.end_utc, true)} (${p.period_type}, reset timezone ${p.reset_timezone}, ` +
    (p.reset_confirmed ? "reset time confirmed" : `reset time NOT confirmed: collection resumes ${RT.meta && "after a grace period"}, at ${fmtTime(p.resume_at_utc, true)}`) + ")."));
  c1.appendChild(el("p.small", b.scope_note));
  box.appendChild(c1);

  const c2 = el("div.card", el("h2", "Map tiles"));
  for (const k of ["map", "traffic"]) {
    const t = b.tiles[k];
    c2.appendChild(el("div.small", `${k === "map" ? "Map display" : "Traffic flow"} tiles this period: ${t.observable ? t.used : "not observable (direct browser mode)"} of ceiling ${t.ceiling} (allowance ${t.allowance})`));
  }
  box.appendChild(c2);

  if (isAdmin()) {
    const c3 = el("div.card", el("h2", "Budget settings"));
    const s = (id, label, val) => el("div.field", el("label", { for: id }, label), el("input", { id, type: "number", min: 0, value: val }));
    c3.appendChild(el("div.row.end", s("bs-allow", "Monthly routing allowance", r.allowance), s("bs-res", "Safety reserve", r.safety_reserve),
      s("bs-other", "Reserved for other apps", r.other_apps_allowance), s("bs-open", `Opening usage for ${p.key}`, r.opening_usage),
      s("bs-warn", "Warning at %", r.warning_percent), s("bs-hard", "Hard ceiling override (0 = computed)", r.hard_ceiling_override)));
    c3.appendChild(el("label.row", { style: { gap: "6px", marginTop: "8px" } }, el("input", { type: "checkbox", id: "bs-conf", checked: p.reset_confirmed }),
      "I have confirmed the provider's reset time (removes the grace period)"));
    c3.appendChild(el("div.row", { style: { marginTop: "8px" } },
      el("button.primary", { onclick: async () => {
        try {
          await api("/api/budget", { method: "PUT", body: { routing_monthly_allowance: +$("bs-allow").value, routing_safety_reserve: +$("bs-res").value,
            routing_other_apps_allowance: +$("bs-other").value, opening_usage: +$("bs-open").value, routing_warning_percent: +$("bs-warn").value,
            routing_hard_ceiling: +$("bs-hard").value, allowance_reset_confirmed: $("bs-conf").checked } });
          toast("Saved"); RT.meta = await api("/api/meta"); renderBanners(); loadBudget();
        } catch (e) { toast(e.message); }
      } }, "Save"),
      el("button", { onclick: async () => { await api("/api/budget/resume", { method: "POST", body: {} }); toast("Pauses cleared"); RT.meta = await api("/api/meta"); renderBanners(); loadBudget(); } }, "Resume after provider pause"),
      el("button", { onclick: async () => { await api("/api/collection", { method: "PUT", body: { enabled: !RT.meta.collection_enabled } }); RT.meta = await api("/api/meta"); renderBanners(); loadBudget(); } },
        RT.meta.collection_enabled ? "Switch scheduled collection off" : "Switch scheduled collection on"),
      el("button", { onclick: async () => { const x = await api("/api/backup", { method: "POST", body: {} }); toast(`Backup ${x.file} written`); } }, "Back up database now")));
    box.appendChild(c3);
  }

  const c4 = el("div.card", el("h2", "Exports"),
    el("div.row", el("a", { href: `${BASE}/api/export/observations.csv` }, "All observations (CSV)"), el("a", { href: `${BASE}/api/export/geometries.geojson` }, "Saved route geometries (GeoJSON)")));
  box.appendChild(c4);

  const t = el("table", el("thead", el("tr", ["#", "Created", "Purpose", "Outcome", "Counted", "HTTP", "Endpoint", "Note"].map(h => el("th", h)))));
  const tb = el("tbody");
  for (const x of b.ledger) tb.appendChild(el("tr", el("td", x.id), el("td", fmtTime(x.created_utc, true)), el("td", x.purpose), el("td", x.outcome),
    el("td", x.counted ? "yes" : "no"), el("td", x.http_status ?? "—"), el("td", x.endpoint || ""), el("td", { title: x.note || "" }, (x.note || "").slice(0, 60))));
  t.appendChild(tb);
  box.appendChild(el("details.card", el("summary", el("b", "Request ledger"), " (latest 100 entries)"), el("div.table-wrap", { style: { marginTop: "8px" } }, t)));

  const ev = await api("/api/events?limit=15");
  const et = el("table", el("tbody", ev.events.map(x => el("tr", el("td", fmtTime(x.ts_utc, true)), el("td", x.level), el("td", x.kind), el("td", { style: { whiteSpace: "normal" } }, x.message)))));
  box.appendChild(el("div.card", el("h2", "Events"), el("div.table-wrap", et)));
}
function kpi(v, l) { return el("div.kpi", el("div.v", v === null || v === undefined ? "—" : v.toLocaleString("en-GB")), el("div.l", l)); }

boot().catch(e => { console.error(e); toast("Failed to load: " + e.message, 8000); });
