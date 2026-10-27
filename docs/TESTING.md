# What was tested, and what still needs real-world observation

## Automated tests (`pytest`, 59 tests, no network)

| Requirement | Tests |
| --- | --- |
| Budget enforced before outbound requests | `test_budget.py::test_guard_blocks_before_any_outbound_request`, `test_concurrent_reservations_never_exceed_ceiling` (4 threads, exact ceiling), ceiling arithmetic, opening usage, hard ceiling |
| Conservative counting and retry policy | failed requests counted; no retry on auth/invalid; `Retry-After` honoured; long `Retry-After` → backoff pause; limited retries for 5xx; no retries above the warning threshold; quota/auth pauses and automatic resumption after the (grace-delayed) reset |
| No duplicate jobs from concurrent workers | `test_scheduler.py::test_no_duplicate_cycles_from_concurrent_workers` (4 workers, own connections, lease stolen mid-way: one cycle) and lease semantics |
| No catch-up storm after downtime | `test_no_catch_up_storm_after_downtime` (5 h outage → 0 extra requests, 19 `missed` slots), `test_late_wakeup_skips_slot_instead_of_running_it` |
| Daylight-saving behaviour | fall-back day has 100 distinct slots (02:00–02:45 twice, different offsets), spring-forward day has 92, scheduler run across the fall-back night (100 cycles, none doubled or skipped), non-existent local times in the planner |
| Zero vs missing | fixture parsing (0 delay stays 0, absent free-flow stays NULL), failed corridor → NULL metrics with reason, `recommended_traffic_delay_min == 0.0` in the API |
| Route identity with reordered alternatives | `test_route_identity_survives_alternative_reordering` (same geometry at index 0/1/2 → one variant; recommendation matched by geometry) |
| Corridor deviation detection | detour flagged with max distance; small jitter not flagged; one event per new deviating geometry |
| Failed corridor not replaced by the unrestricted route | `test_zero_values_stored_as_zero_and_failures_as_null` |
| Comparisons with missing routes | a corridor failing half the time gets wins only where both corridors have data; the rest are "undetermined", and coverage shows the gap |
| Historical map loading without routing calls | `test_api.py::test_manual_collect_then_history_without_new_requests` calls every read endpoint and page; the client call count does not change |
| Prediction with insufficient observations | `test_planner_insufficient_data`; `test_planner_uses_slot_specific_durations` (duration jumps at 07:30, planner picks 07:30, not the constant-duration answer) |
| Persistence and restoration | online backup while running, restore with the app stopped, restart keeps data and does not reseed, migrations idempotent, restart recovery of interrupted cycles and pending ledger entries |
| Security | auth required, read tokens read-only, CSRF header, tile proxy refuses other kinds, out-of-range tiles and unauthenticated use; key absent from logs, error messages, stored parameters and tile responses |
| Exports | CSV columns and rows, GeoJSON lon/lat order |
| Hiding and tolerance | retired corridors can be hidden (observations kept, removed from analysis), shown again, or purged explicitly; tolerance validated per corridor |
| "Show all collected routes" | extra alternatives and deviating corridor routes appear as their own series only when asked for; corridor statistics are identical with or without them; no requests |
| Private first-run setup | Empty live database makes no routing calls; user-created coordinates persist across restart; demo/test locations are fictional |
| Demo separation | separate database file, `data_source: demo` everywhere |

## Manual and visual checks

- Ran the app in demo mode and checked every view with Playwright/Chromium at 1440×900 (desktop),
  1280×800 (landscape tablet) and 390×844 (phone): map current and history, analysis, planner, setup, budget, and
  the Home Assistant dashboard. Earlier screenshots were removed because they contained private location data. No JavaScript errors were reported in that earlier check.
- Built the Docker image and ran it with `compose.yaml` (read-only root filesystem, dropped capabilities):
  health check healthy, `scripts/backup.py` inside the container, graceful stop in under a second with the
  scheduler stopping cleanly, restart with the data intact.

## Not verified yet: needs the real API and real time

- **No live TomTom request was made.** The development environment's network policy blocked `api.tomtom.com`. The
  parser, error classification and request parameters follow the documented API but have only been exercised with
  synthetic fixtures. Run `scripts/live_verify.py --max-requests 2` first and check the recorded responses.
- **Map tiles** have not been loaded from TomTom; screenshots show routes without a base map.
- **Billing units and reset time**: confirm on the TomTom dashboard after the first day (about 576 requests a day
  with two confirmed corridors per direction; 192 a day until corridors are confirmed).
- **Live corridors** must be configured in Setup, previewed and confirmed by the administrator.
- **Deviation thresholds** (400 m, 5 % of the route, ±15 % length) and the 85 % "resembles" similarity are first
  guesses. Tune them once real geometry variations are visible.
- **Statistics and planning** become meaningful after several weeks of observations per weekday. With fewer than
  4 observations per slot the planner reports insufficient data.
- **Not deployed**: nothing has been installed on the Ubuntu/Dockhand server, and no continuous collection is running.

Privacy update validation: 59 tests passed and JavaScript syntax checks passed. A new browser screenshot could not be produced in the update environment because the Chromium download failed.
