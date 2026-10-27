# TomTom usage, budget and terms

## What the application calls

| Purpose | Endpoint | When |
| --- | --- | --- |
| Unrestricted recommendation (+ up to 2 extra alternatives in the same call) | `GET /routing/1/calculateRoute/{from}:{to}/json` | every scheduled cycle, per direction |
| Corridor, via points | `GET /routing/1/calculateRoute/{from}:{via…}:{to}/json` | every cycle, per confirmed corridor |
| Corridor, exact geometry | `POST /routing/1/calculateRoute/{from}:{to}/json` with `supportingPoints` | every cycle, per confirmed corridor |
| Corridor preview | same as the corridor request | only when you click **Preview** in Setup |
| Manual refresh / live check | same as a cycle | only when you click **Collect now** or **Check live now** |
| Map tiles | `map/1/tile/basic/main/{z}/{x}/{y}.png` | only while a map is on screen |
| Traffic flow tiles | `traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png` | only with the overlay switched on, current view, page visible |

Query parameters for routing: `traffic=true`, `departAt=now`, `routeType=fastest`, `travelMode=car`,
`computeTravelTimeFor=all` (adds no-traffic, historic and live-incident travel times), `routeRepresentation=polyline`,
`sectionType=traffic` (traffic sections with delay and category, stored per observation), `instructionsType=coded`
(road numbers and street names), and for the unrestricted request `maxAlternatives=2&alternativeType=anyRoute`.
Each request sends a random `Tracking-ID` header; the returned `Tracking-ID` is stored.

The key is added at send time only. It is never stored with request parameters, written to logs (httpx request
logging is silenced), or included in error messages (errors are sanitized).

## Routing budget calculation

Default plan: 2 directions × (1 unrestricted + 2 corridors) = **6 requests per cycle**, every 15 minutes.

| Period | Cycles | Requests |
| --- | --- | --- |
| Normal day | 96 | 576 |
| DST spring-forward day (last Sunday of March) | 92 | 552 |
| DST fall-back day (last Sunday of October) | 100 | 600 |
| 28-day month | 2,688 | 16,128 |
| 30-day month | 2,880 | 17,280 |
| 31-day month | 2,976 | 17,856 |
| March (31 days, one short day) | 2,972 | 17,832 |
| October (31 days, one long day) | 2,980 | **17,880** |

Default guard: allowance 20,000 − safety reserve 2,000 − other applications 0 = **application ceiling 18,000**.

That leaves 120–1,872 requests a month for retries, manual refreshes, corridor previews and the initial verification.
October is the tightest month (120 spare). If spare runs out, the guard skips the last cycles of the month
(shown as *skipped: budget_protection*) instead of exceeding the ceiling. The **Budget** page shows the forecast
and the projected pause time. Ways to create more headroom:

- `COLLECTION_INTERVAL_MINUTES=20` → 432 a day, 13,392 in a 31-day month.
- Fewer corridors: each corridor in both directions costs 192 a day (5,952 a month).
- A smaller safety reserve, if you are sure nothing else uses the key.

**The application never raises its own usage.** No setting is changed automatically; the collection interval and
corridors only change when you change them. `ROUTING_HARD_CEILING` can replace the computed ceiling, but it is
clamped to *allowance − other apps*, so even a typo cannot push the guard beyond the TomTom allowance (the Budget page
shows a warning when the value was clamped). The Budget page also shows requests per day (scheduled vs. extra), the
remaining spare after the rest of the month's schedule, and whether scheduled collection is running (thread,
lease, last heartbeat, next slot, last cycle).

Counting is conservative: an attempt is written to the ledger and counted *before* it is sent, and it stays counted
when it fails, times out, or the application stops during the request. Requests blocked by the guard are logged
but not counted (they never left the server).

## Things the ledger cannot know

- **Usage by other applications** on the same key. Enter an estimate as *Reserved for other apps* or
  *Opening usage*. TomTom's account dashboard is the authority.
- **Whether failed requests are billed.** Unknown, so they are counted.
- **The exact reset moment.** The allowance period defaults to the calendar month in UTC
  (`ALLOWANCE_PERIOD=monthly`, `ALLOWANCE_RESET_TIMEZONE=UTC`). Until you confirm it
  (`ALLOWANCE_RESET_CONFIRMED=true` or the checkbox on the Budget page), the guard waits an extra
  `ALLOWANCE_RESET_GRACE_MINUTES` (60) after the assumed reset before resuming a paused budget.
  If TomTom itself answers that the quota is exhausted, collection pauses until the next period.

## Verification status (October 2026)

**Verified** (TomTom's public price announcement, read via web search because the TomTom hosts were blocked):
monthly free tiers since 1 July 2026 of 20,000 Routing requests, 200,000 Map Display raster tiles and 200,000
Traffic raster tiles. **Not verified**: everything in the list below, the reset moment, behaviour when the limit is
exceeded (block or overage billing), and the storage terms.

The development environment could not reach `developer.tomtom.com`, `docs.tomtom.com` or `api.tomtom.com`
(outbound network policy). These points are therefore **assumptions to confirm**, and every one of them is configurable:

- TomTom introduced monthly free tiers on 1 July 2026 (per its price announcement, found via search). Your dashboard shows
  Routing 20,000 / Map Display raster tiles 200,000 / Traffic raster tiles 200,000 per month.
- One `calculateRoute` call is assumed to be one billable Routing request, regardless of `maxAlternatives`,
  waypoints or supporting points. Check the dashboard after the first day: it should be about 576 higher.
- One raster tile is assumed to be one tile request.
- Field names follow the TomTom Routing API documentation (as also reflected in TomTom's Web SDK reference).
  Missing fields are stored as NULL, never as 0, so a differently shaped response degrades gracefully.
- 403 responses are classified from their text: "Over Qps" → throttling (back off), "Over … limit/queries" → quota
  exhausted (pause until reset), anything else → authentication problem (pause until you resume; no automatic retry).

Run `scripts/live_verify.py` (at most 10 requests in total, recorded in the ledger) once the network allows it,
and compare the recorded fixtures with these assumptions.

## Storage and reuse terms

TomTom's developer terms could not be read from the development environment, and public summaries disagree on how
long routing results may be stored. This application stores, for personal analysis:

- numerical route summaries and timestamps per observation,
- deduplicated route geometries and traffic sections,
- no map tiles (tiles pass through the proxy to the browser cache, following TomTom's cache headers),
- no raw responses unless `STORE_RAW_RESPONSES=true`.

**Before relying on long-term retention, read the TomTom Developer Portal terms for your plan.** If they limit
storage, set `OBSERVATION_RETENTION_DAYS` (deletes observations, cycles and unreferenced geometries older than N days,
once a day). The default is 0 (keep everything) because the purpose is comparing months. Do not publish or
redistribute exported data or screenshots with TomTom map content beyond what the terms allow; keep the
"© TomTom" attribution visible on maps.

## Map key in the browser

In the default `MAP_TILE_MODE=proxy` the browser only talks to this application, so the TomTom key stays on the
server and tile requests are counted. `MAP_TILE_MODE=direct` sends tile requests straight from the browser to
`api.tomtom.com`; the key is then visible to anyone who can open the page. Use a **separate** key
(`TOMTOM_MAP_KEY`) restricted to the map display and traffic tile products and, if TomTom offers it for your account,
to your domain. In direct mode tile usage is not observable by the ledger. `MAP_TILE_MODE=off` draws routes
without a base map.
