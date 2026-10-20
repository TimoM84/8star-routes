# Implementation plan and key assumptions

This is the short design note written before the first implementation. It records the choices that are not obvious from the code, and the assumptions that still need real-world confirmation.

## Stack

- **Python 3.12, FastAPI, Uvicorn**: one process, one container.
- **SQLite** (stdlib `sqlite3`, WAL mode) in a Docker volume. No ORM. Numbered SQL migrations.
- **Vanilla JavaScript + Leaflet 1.9.4** (vendored, no CDN) and small hand-written SVG charts. No build step.
- **httpx** for outbound TomTom calls.

The sibling 8star apps use TypeScript. The spec named Python/FastAPI as the default, and nothing here benefits from a frontend build pipeline, so this app follows the spec.

## Main design decisions

1. **Schedule is defined in UTC.** Collection slots are UTC instants on multiples of the interval (default 15 min). Amsterdam offsets are whole hours, so quarter-hour slots in UTC are also quarter-hour slots in local time. A DST spring-forward day then has 92 slots and a fall-back day has 100. Nothing is skipped or doubled. Weekday and time-of-day analysis converts each slot to `Europe/Amsterdam`.
2. **Exactly-once slots.** Each scheduled slot is a row with a `UNIQUE` slot timestamp. Whoever inserts it first owns it. A database lease also allows only one scheduler loop to be active across workers or containers.
3. **No catch-up.** Only the current slot can run, and only within a configurable lateness tolerance (default 120 s). Slots that passed while the app was down are written as `missed` rows that make no requests. They show up as explicit gaps.
4. **Budget guard in front of every outbound routing call.** A ledger row is inserted inside an `IMMEDIATE` transaction *before* the request is sent, and it counts even when the request then fails. A scheduled cycle starts only when the whole cycle fits within the ceiling, so budget protection never leaves half cycles.
5. **Stable route identity.**
   - *Unrestricted recommendation* = the route TomTom returns first for an unconstrained request. It is stored as the role `recommended`, never as `routes[0]`. Its geometry is matched against the confirmed corridors ("resembles corridor X", with a score).
   - *Configured corridor* = a named route defined by the user: ordered via points (`waypoints`) or an exact saved geometry rebuilt with TomTom `supportingPoints` (`reconstruct`).
   - Geometry is stored once per distinct *variant* (SHA-256 fingerprint of the exact coordinates, zlib-compressed). Every observation references its variant.
6. **Corridors are never invented and presented as confirmed.** Live mode starts empty; demo mode contains fictional draft corridors only. They are not collected until the user inspects them on the map and confirms them.
7. **Zero vs missing.** Every metric column is nullable. Failed requests produce an observation row with `status='failed'` and NULL metrics. Missing slots produce no observation but a `missed`/`skipped` cycle row.
8. **Map tiles through a fixed-template server proxy** (`/tiles/map/z/x/y.png`, `/tiles/traffic/z/x/y.png`). The key stays server-side, tile requests are counted, upstream cache headers are passed through, and nothing is cached on disk. There is no arbitrary URL proxy. A direct browser mode with a separate restricted key is optional.
9. **Demo mode is a separate database.** `APP_MODE=demo` uses `demo.sqlite`, a synthetic fixture-based TomTom client and generated history. Live mode never contains demo rows. Every API response carries `data_source`.

## Assumptions to confirm in real use

The TomTom web sites (`developer.tomtom.com`, `docs.tomtom.com`, `api.tomtom.com`) were blocked by the development environment's network policy, so these could not be read or called directly. They are configurable instead of hardcoded:

| Topic | Assumption | Setting |
| --- | --- | --- |
| Billing unit | One `calculateRoute` call = one Routing request, including `maxAlternatives` and waypoints | `ROUTING_MONTHLY_ALLOWANCE`, ledger counts attempts |
| Allowance period | Calendar month (TomTom moved to monthly free tiers on 1 July 2026) | `ALLOWANCE_PERIOD=monthly` (or `daily`) |
| Reset time | 00:00 UTC on the 1st, treated as *unconfirmed* with a 1 h grace before resuming | `ALLOWANCE_RESET_TIMEZONE`, `ALLOWANCE_RESET_GRACE_MINUTES`, `ALLOWANCE_RESET_CONFIRMED` |
| Tile billing | One raster tile = one request | separate map/traffic allowances |
| Storage terms | Not verified; long-term storage of route results may be restricted by the TomTom licence | retention settings, see README "TomTom terms" |
| Response fields | `summary.lengthInMeters`, `travelTimeInSeconds`, `trafficDelayInSeconds`, `trafficLengthInMeters`, `departureTime`, `arrivalTime`, and with `computeTravelTimeFor=all`: `noTrafficTravelTimeInSeconds`, `historicTrafficTravelTimeInSeconds`, `liveTrafficIncidentsTravelTimeInSeconds` | parser tolerates missing fields (stored as NULL) |
| Tracking id | Request and response header `Tracking-ID` | stored when present |

## Build order

1. Vertical slice: editable journey, one collection cycle, persistent observations and geometry, map from stored data.
2. Corridors, route identity, deviation detection, scheduler.
3. Budget guard, ledger, forecast, retry policy.
4. History navigation, statistics, arrival planner.
5. Home Assistant examples, Docker, docs.
6. Tests and visual checks.
