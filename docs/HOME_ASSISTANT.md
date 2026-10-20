# Home Assistant

8star-routes runs on its own and stores its own history. Home Assistant only *displays* it, so nothing depends on
the Home Assistant Recorder, and you do not need to change its retention.

## Option A (recommended): same site behind your reverse proxy

Serve both under one registrable domain over HTTPS, for example `https://ha.example.com` and
`https://routes.example.com` (or `https://ha.example.com/routes` with `BASE_PATH=/routes`). Pages on the same site
can share the app's `SameSite=Lax` login cookie inside an iframe, so you log in once in the iframe and stay logged in.

8star-routes settings:

```env
PUBLIC_BASE_URL=https://routes.example.com
TRUSTED_PROXIES=172.18.0.0/16        # your proxy / Docker network
COOKIE_SECURE=auto
FRAME_ANCESTORS='self' https://ha.example.com
```

`FRAME_ANCESTORS` becomes the `frame-ancestors` Content Security Policy, so only your Home Assistant can embed the page.

Dashboard card (YAML mode or the manual card editor):

```yaml
type: iframe
url: https://routes.example.com/dashboard
aspect_ratio: 62%
```

For the full application use `url: https://routes.example.com/` instead, or add a sidebar panel:

```yaml
# configuration.yaml
panel_iframe:            # on recent versions: Settings > Dashboards > Add dashboard > Webpage
  routes:
    title: Routes
    icon: mdi:car-clock
    url: https://routes.example.com/
```

## Option B: read-only embed token (different sites, LAN IPs, the companion app)

When Home Assistant and 8star-routes are on different sites (two IP addresses count as two sites), browsers block
the login cookie inside the iframe. Use the read-only embed token instead. It is sent as a request header by the
page itself, not as a cookie:

```env
EMBED_TOKEN=<openssl rand -hex 24>
FRAME_ANCESTORS='self' http://homeassistant.local:8123 https://ha.example.com
```

```yaml
type: iframe
url: https://routes.example.com/dashboard?token=<EMBED_TOKEN>
aspect_ratio: 62%
```

The token only grants read access (dashboard, latest data, map tiles). Anyone who has the URL can see your routes,
including your home and work locations, so treat it like a password and rotate it if it leaks.
Do not mix schemes: an HTTPS Home Assistant cannot embed an `http://` page (mixed content).

## Tablet dashboard

`/dashboard` is a compact landscape layout: both directions with the current recommendation, traffic delay, the
configured corridors, today's travel-time curve and a map of the selected direction. It refreshes stored data every
60 seconds while visible and never triggers routing requests. In portrait or on phones it stacks vertically.

## REST sensors

Create a read token (`API_READ_TOKEN`) and use the read-only API:

- `GET /api/v1/latest`: all directions
- `GET /api/v1/latest/outbound` and `/api/v1/latest/return`: one direction (flat fields)

```yaml
# configuration.yaml
rest:
  - resource: https://routes.example.com/api/v1/latest/outbound
    headers:
      Authorization: !secret routes_read_token     # "Bearer <API_READ_TOKEN>"
    scan_interval: 300
    sensor:
      - name: Commute to work
        unique_id: routes_outbound_travel
        value_template: "{{ value_json.recommended_travel_time_min }}"
        unit_of_measurement: min
        device_class: duration
        json_attributes:
          - recommended_traffic_delay_min
          - recommended_resembles
          - fastest_route
          - fastest_travel_time_min
          - observed_utc
          - age_minutes
          - stale
          - partial
      - name: Commute to work traffic delay
        unique_id: routes_outbound_delay
        value_template: "{{ value_json.recommended_traffic_delay_min }}"
        unit_of_measurement: min
        device_class: duration
  - resource: https://routes.example.com/api/v1/latest/return
    headers:
      Authorization: !secret routes_read_token
    scan_interval: 300
    sensor:
      - name: Commute home
        unique_id: routes_return_travel
        value_template: "{{ value_json.recommended_travel_time_min }}"
        unit_of_measurement: min
        device_class: duration
        json_attributes: [recommended_traffic_delay_min, fastest_route, observed_utc, stale]
```

```yaml
# secrets.yaml
routes_read_token: "Bearer <API_READ_TOKEN>"
```

A sensor is `unknown` when the latest recommendation failed or nothing is stored yet. `stale: true` means the
observation is older than `STALE_AFTER_MINUTES` (40). Polling these endpoints only reads the database.

Example automation (leave-now reminder):

```yaml
automation:
  - alias: Tell me when the commute is slow
    triggers:
      - trigger: numeric_state
        entity_id: sensor.commute_to_work_traffic_delay
        above: 10
    conditions:
      - condition: time
        after: "06:30:00"
        before: "09:00:00"
        weekday: [mon, tue, wed, thu, fri]
      - condition: template
        value_template: "{{ not state_attr('sensor.commute_to_work', 'stale') }}"
    actions:
      - action: notify.notify
        data:
          message: >
            TomTom estimates {{ states('sensor.commute_to_work') }} min to work
            (+{{ states('sensor.commute_to_work_traffic_delay') }} min delay).
```
