# 8star-routes

Track TomTom route recommendations, travel times and traffic delays to discover patterns and plan smarter journeys.

8star-routes asks TomTom every 15 minutes, around the clock, which route it recommends between two places and
how long each of your own named routes ("corridors") would take. It keeps every answer, draws the routes on a map,
lets you step back through history, compares routes by weekday and time of day, and suggests when to leave to arrive
on time.

> **Estimates, not measurements.** Every number is TomTom's *estimate* at the moment of observation. Nobody drove
> these routes. The interface says so throughout.

## What it answers

- Which route does TomTom recommend at a given time, and when does it switch?
- How long would each alternative take at that moment, and how much traffic delay does TomTom report?
- Which route is usually fastest on Tuesday mornings? Would leaving 15 or 30 minutes earlier help?
- Which route is most reliable (90th percentile, coverage), not just fastest on average?
- When should I leave to arrive by 09:00?

## Features

- **Map**: current view (latest *stored* observation; opening the page never makes routing requests), recommendation
  in blue, corridors in their own colours, distance, estimated time, traffic delay, free-flow and historic times,
  data age, stale/partial/missing indicators. Optional live traffic overlay: off by default, current view only,
  refreshed every 5 minutes or slower, only while the page is visible and online.
- **History**: date and time picker, previous/next observation, jump to the previous/next recommendation change,
  a day timeline showing missing and failed slots, geometry-change comparison. Historical routes show the traffic
  sections TomTom reported *at that time*, never today's traffic.
- **Analysis**: per direction, corridor, weekday, departure-time window and date range: median, mean, 90th percentile,
  traffic delay median and 90th percentile, time above free flow, observation count, coverage and missing rate,
  how often each corridor is fastest, how often the recommendation changes. Day charts, recommendation timeline,
  weekday × time heatmap.
- **Plan a trip**: date and arrival time → recommended route and departure with a reliability margin, using
  slot-specific historical distributions (durations are not assumed constant). Shows sample size, date range and
  method, and an explicit "not enough data" state. An optional, separate, budgeted live check.
- **Setup**: editable journey, per-direction collection switch, corridor drafts from map clicks (via points) or from
  a route TomTom already returned (exact geometry), preview on the map (one request), confirm, retire.
- **Budget**: request ledger, routing/map/traffic counters, allowance, safety reserve, other-apps reserve, opening
  usage, warning threshold, hard ceiling, forecast, pause reason and automatic resumption after the reset.
- **Exports**: CSV of every observation (including failures) and GeoJSON of all saved route geometries.
- **Home Assistant**: iframe-friendly dashboard for a landscape tablet, read-only REST API and sensor examples.

## How collection works

Each cycle requests, for each direction:

1. the **unrestricted** fastest route (`maxAlternatives=2`; the extra alternatives are stored but never replace
   corridors), and
2. each **confirmed corridor**: either ordered via points (TomTom may vary the route slightly between them) or an
   exact saved geometry rebuilt with TomTom `supportingPoints`.

With two corridors per direction that is 6 requests per cycle, 576 per day, 17,856 in a 31-day month.
See [docs/TOMTOM.md](docs/TOMTOM.md) for the full budget calculation (including DST months) and the request parameters.

- Slots are UTC quarter hours (which are also local quarter hours), so DST changes neither skip nor double slots.
  A fall-back day simply has 100 slots and a spring-forward day 92.
- A slot runs at most once. The cycle row is claimed with a `UNIQUE` slot before any request, and a database lease
  lets only one scheduler be active, even with several workers.
- No catch-up. Only the current slot may run, and only within 120 s of its start. Slots missed during downtime are
  recorded as `missed` and shown as gaps. They are never backfilled with current traffic.
- Every outcome is stored: successes (including unchanged values), failures with their reason, and requests that
  were never sent (budget protection) as explicit failed observations. Missing values stay NULL; a real 0 stays 0.
- Request and response timestamps are stored separately. Routes from one cycle are compared only when their
  responses arrived within `COMPARISON_TOLERANCE_SECONDS` (120) of each other.
- Route identity never depends on response order. The recommendation is matched to corridors by geometry similarity,
  and geometry is stored once per distinct variant (SHA-256 of the exact coordinates, compressed) and referenced by
  every observation. Corridor routes that stray from their confirmed geometry are flagged for inspection.

### Private journey setup

Live installations start with no journey, coordinates or suggested corridors.
After logging in, open **Setup**, enter your own Home and Work locations and select **Create journey**.
The locations are stored only in your persistent database, not in source code or environment examples.
Existing installations keep their configured journeys and observations when upgrading.

Once a journey is configured, the two unrestricted requests run each cycle. Add corridors using via points,
or choose **Use as corridor** on a returned route. Preview and confirm each corridor before it is collected.
Demo mode uses an explicitly fictional journey and fictional route geometries in its separate database.
Never commit real route recordings, database files, exports or screenshots containing private locations.

## Deploy with Docker Compose (Dockhand)

1. In **Dockhand**, create a new **Stack** from a **Git repository**:
   - Repository URL: `https://github.com/TimoM84/8star-routes.git`
   - Branch: `main`
   - Compose path: `compose.yaml`
2. Add the **environment variables** to the stack (see `.env.example`):
   - `TOMTOM_API_KEY`: required.
   - `APP_PASSWORD`: required, the administrator password (12+ characters recommended).
   - `SESSION_SECRET`: required, `openssl rand -hex 32`. Keeps logins valid across restarts.
   - `HOST_PORT`: default `8080`.
   - Optional: `API_READ_TOKEN`, `EMBED_TOKEN` for Home Assistant; `TRUSTED_PROXIES`, `PUBLIC_BASE_URL`, `BASE_PATH`,
     `FRAME_ANCESTORS` behind a reverse proxy; budget values if you started mid-month (`ROUTING_OPENING_USAGE`).
3. **Deploy**. Dockhand builds the image from the `Dockerfile`.
4. Open `http://<server>:<HOST_PORT>`, log in, go to **Setup**, create your journey, then preview and confirm corridors.
5. Check **Budget** after a day: the ledger should show about 576 requests per day with two corridors per direction.
   Compare with the TomTom dashboard.

From a shell instead:

```bash
git clone https://github.com/TimoM84/8star-routes.git && cd 8star-routes
cp .env.example .env    # fill in TOMTOM_API_KEY, APP_PASSWORD, SESSION_SECRET
docker compose up -d --build
```

The container runs as a non-root user with a read-only root filesystem, no capabilities and `no-new-privileges`.
All state lives in the `routes-data` volume (`/data`). On `SIGTERM` the scheduler stops between requests (an
unfinished cycle is marked `interrupted`) and the server shuts down gracefully. After a crash, open cycles and
pending ledger entries are closed on start (still counted).

### Try it without a key: demo mode

`APP_MODE=demo` uses a separate `demo.sqlite`, generates 35 days of **synthetic** history (with a simulated 7-hour
outage) and keeps "collecting" from a fake TomTom stand-in. Nothing is sent to TomTom. Every page and API response
is labelled as demo data. Live and demo data never mix.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TOMTOM_API_KEY` | none | Server-side routing key (also used for proxied tiles). |
| `APP_PASSWORD` | none | Administrator password. Required with `AUTH_MODE=password`. |
| `SESSION_SECRET` | random per start | Signs the session cookie. |
| `AUTH_MODE` | `password` | `none` disables login. Only use it behind an authenticating proxy (Authelia, Authentik…). |
| `API_READ_TOKEN` | empty | Bearer token for the read-only API (`/api/v1/…` and other GET endpoints). |
| `EMBED_TOKEN` | empty | Read-only token for `/dashboard?token=…` (Home Assistant iframe across sites). |
| `PORT` / `HOST_PORT` | `8080` | Container port / published host port. |
| `BASE_PATH` | empty | Serve under a sub path, e.g. `/routes`. |
| `PUBLIC_BASE_URL` | empty | Public URL, used in documentation links. |
| `TRUSTED_PROXIES` | empty | Proxy IPs/CIDRs whose `X-Forwarded-*` headers are trusted. |
| `COOKIE_SECURE` | `auto` | `auto` sets `Secure` when the request is HTTPS. |
| `FRAME_ANCESTORS` | `'self'` | CSP `frame-ancestors`; add your Home Assistant origin. |
| `TIMEZONE` | `Europe/Amsterdam` | Display and weekday analysis. Storage is always UTC. |
| `COLLECTION_ENABLED` | `true` | Default state; can be switched in Budget. |
| `COLLECTION_INTERVAL_MINUTES` | `15` | 5, 10, 15, 20, 30 or 60. Mind the budget. |
| `COLLECTION_LATE_TOLERANCE_SECONDS` | `120` | A slot that cannot start within this time is skipped. |
| `COMPARISON_TOLERANCE_SECONDS` | `120` | Max response-time spread for comparing routes within a cycle. |
| `STALE_AFTER_MINUTES` | `40` | Latest data older than this is marked stale. |
| `UNRESTRICTED_MAX_ALTERNATIVES` | `2` | Extra alternatives in the unrestricted request. |
| `MAX_RETRIES` / `RETRY_DELAY_SECONDS` | `1` / `8` | Retries for timeouts, network and 5xx errors only. |
| `MAX_RETRY_AFTER_SECONDS` | `90` | Longer `Retry-After` values pause routing instead of waiting. |
| `ROUTING_MONTHLY_ALLOWANCE` | `20000` | Provider allowance per period. |
| `ROUTING_SAFETY_RESERVE` | `2000` | Kept unused. |
| `ROUTING_OTHER_APPS_ALLOWANCE` | `0` | Reserved for other apps on the same key. |
| `ROUTING_OPENING_USAGE` | `0` | Usage before this app started, for the first period. |
| `ROUTING_HARD_CEILING` | `0` | Override the computed ceiling (allowance − reserve − other apps). |
| `ROUTING_WARNING_PERCENT` | `80` | Warning threshold; above it, no automatic retries. |
| `ALLOWANCE_PERIOD` | `monthly` | `monthly` or `daily`. |
| `ALLOWANCE_RESET_TIMEZONE` | `UTC` | Timezone of the provider's reset (not the UI timezone). |
| `ALLOWANCE_RESET_CONFIRMED` | `false` | Without confirmation the guard waits an extra grace period after the reset. |
| `ALLOWANCE_RESET_GRACE_MINUTES` | `60` | That grace period. |
| `MAP_TILE_ALLOWANCE` / `TRAFFIC_TILE_ALLOWANCE` | `200000` | Tile allowances; `TILE_SAFETY_RESERVE` (10000) is kept unused. |
| `MAP_TILE_MODE` | `proxy` | `proxy` (key stays server-side, counted), `direct` (browser uses `TOMTOM_MAP_KEY`), `off`. |
| `MAP_TILE_URL` / `TRAFFIC_TILE_URL` | TomTom raster v1 / flow v4 | Must start with `https://api.tomtom.com/`. |
| `TRAFFIC_REFRESH_MINUTES` | `5` | Live overlay refresh, minimum 5. |
| `OBSERVATION_RETENTION_DAYS` | `0` | 0 keeps everything; see storage terms in docs/TOMTOM.md. |
| `STORE_RAW_RESPONSES` | `false` | Also keep the compressed raw response per request. |
| `DAILY_BACKUP` / `BACKUP_KEEP` | `true` / `14` | Daily consistent backup at 03:00 local, keep 14. |
| `APP_MODE` | `live` | `demo` for synthetic data in a separate database. |

Budget values can also be changed on the Budget page (stored in the database, they override the environment).

## Security

- Admin login with `APP_PASSWORD`, signed HttpOnly `SameSite=Lax` cookie, login rate limiting, CSRF header check on
  every change, strict Content Security Policy (no inline scripts), `frame-ancestors` restricted to `FRAME_ANCESTORS`.
- Read-only access with `API_READ_TOKEN` (Bearer) or `EMBED_TOKEN` (dashboard). These cannot change anything or
  trigger routing requests.
- The TomTom key never reaches the browser in the default proxy mode, is never stored with request parameters, and
  is stripped from error messages. httpx request logging is silenced because it would log the key.
- The tile proxy only knows two fixed upstream templates and accepts integer `z/x/y`. There is no arbitrary URL proxy.
- Behind a reverse proxy, set `TRUSTED_PROXIES` and serve over HTTPS. Do not expose the port to the internet directly.

## Backup and restore

All state is in the volume: `/data/routes.sqlite` (or `demo.sqlite`) plus `/data/backups/`.

- **Automatic**: a consistent backup every day around 03:00 local time (`daily-*.sqlite`, last 14 kept), and a
  backup before every database migration (`pre-migration-*.sqlite`).
- **Manual, while running**: Budget page → *Back up database now*, or
  `docker compose exec 8star-routes python scripts/backup.py`. Copy it out with
  `docker cp 8star-routes:/data/backups/<file> .`
- **Restore**: stop the stack, then
  `docker compose run --rm --no-deps 8star-routes python scripts/restore.py /data/backups/<file>`, then start the
  stack. The restore script first saves the current database as `pre-restore-*.sqlite`. To restore a file from outside
  the volume, copy it in first, for example
  `docker run --rm -v 8star-routes_routes-data:/data -v "$PWD":/in alpine cp /in/<file> /data/backups/`
  (the volume name is prefixed with your stack name).
- **Whole volume**: `docker run --rm -v 8star-routes_routes-data:/data -v "$PWD":/out alpine tar czf /out/routes-data.tgz -C /data .`
- **Upgrades**: redeploy. Migrations run on start, each in a transaction, after a backup. If one fails, the app does
  not start and the data is unchanged.

## Storage expectations

With two corridors per direction there are about 960 observation rows a day (6 requests plus the extra unrestricted
alternatives). Measured in demo mode: about **0.9 MB per day, roughly 330 MB per year**, mostly observation rows and
indexes. Geometry is stored once per distinct route variant. Real TomTom polylines have hundreds to thousands of
points (tens of kB per route), so storing geometry per observation would add roughly 20–30 MB a day (about 8–10 GB a
year). With deduplication, a few hundred variants a year cost a few MB. `OBSERVATION_RETENTION_DAYS` limits growth
if needed.

## API

All JSON responses include `data_source` (`live` or `demo`). GET endpoints need a login, `API_READ_TOKEN` or
`EMBED_TOKEN`. None of them make routing requests.

| Endpoint | Content |
| --- | --- |
| `GET /api/v1/latest` | Latest stored state for all directions (compact, for Home Assistant). |
| `GET /api/v1/latest/{outbound\|return}` | One direction, flat fields. |
| `GET /api/latest?direction_id=` | Latest cycle in full detail. |
| `GET /api/cycles/{id}?direction_id=` | A historical cycle with previous/next and geometry changes. |
| `GET /api/day?direction_id=&date=` | Every slot of a local day, including missing ones. |
| `GET /api/stats?direction_id=&from=&to=&weekdays=0,1&time_from=07:00&time_to=09:00` | Comparison statistics. |
| `GET /api/heatmap?direction_id=&key=recommended&metric=travel_time_s&bucket=30` | Weekday × time medians. |
| `GET /api/plan?direction_id=&date=&arrive=09:00&pct=90&weeks=12` | Departure planning. |
| `GET /api/variants/{id}` | Saved geometry of a route variant. |
| `GET /api/budget` | Ledger, counters, forecast. |
| `GET /api/export/observations.csv` · `GET /api/export/geometries.geojson` | Exports. |
| `GET /healthz` | Health (no auth): database, scheduler, last slot. |

Administrator-only (POST/PUT/DELETE with header `X-Requested-With: 8star-routes`): `/api/collect`,
`/api/corridors…`, `/api/journeys…`, `/api/directions…`, `/api/budget`, `/api/budget/resume`, `/api/collection`, `/api/backup`.

Home Assistant iframe, dashboard and REST sensor examples: [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md).

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                           # 59 tests, no network
APP_MODE=demo APP_PASSWORD=dev-password-1 python -m app   # http://localhost:8080
```

`app/` holds the server (FastAPI, SQLite) and `app/static/` the interface (plain JavaScript, Leaflet 1.9.4 vendored,
no build step). Migrations are numbered SQL files in `app/migrations/`. Screenshots:
`npm i --no-save playwright && SHOT_URL=http://localhost:8080 SHOT_PASSWORD=… node scripts/screenshots.mjs`.

Live verification (at most 10 routing requests in total, all recorded in the ledger, responses saved as fixtures):
`python scripts/live_verify.py --max-requests 2`.

## What was tested and what still needs real-world observation

See [docs/TESTING.md](docs/TESTING.md). In short: scheduling, budget, identity, DST, persistence and the interface
are covered by automated tests and visual checks in demo mode. **No live TomTom request has been made yet**,
because the development environment's network blocked TomTom. The response parser follows the documented format
and has been tested only with synthetic fixtures.

## Licence

MIT, see [LICENSE](LICENSE). Leaflet is BSD-2-Clause (`app/static/vendor/LEAFLET-LICENSE`). Map and traffic content
© TomTom.
