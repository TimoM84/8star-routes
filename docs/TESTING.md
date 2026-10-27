# What was tested, and what still needs real-world observation

## Automated tests (`pytest`, 73 tests, no network)

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
| Route identity across TomTom alternatives (`test_route_identity.py`) | route families group near-identical geometries and keep different ones apart; an alternative that is the same route as a corridor observed in the same cycle (or a visible corridor's reference) is shown once and not counted again; deviating corridor routes are a breakdown (`subset_of`) of the corridor, never an extra series in comparisons or the planner; short harmless differences (< 500 m outside the tolerance) are not flagged; retired and hidden corridors are not "expected" after retirement |
| Planner answers | when to leave, fastest and most reliable corridor, 15/30 minutes earlier, on-time share, buffer, sample count and confidence; low-sample slots never recommended |
| Budget hard ceiling | `ROUTING_HARD_CEILING` above allowance − other apps is clamped |
| Retention purge | ledger rows survive (cycle link set to NULL); family representative geometries are kept |
| Upgrade from the previous schema | database built with migrations 001–002 and realistic records → start the new version: every existing row unchanged, pre-migration backup identical, families assigned, 0 routing calls; a failing migration is rolled back completely and leaves the database as it was |
| Private first-run setup | Empty live database makes no routing calls; user-created coordinates persist across restart; demo/test locations are fictional |
| Demo separation | separate database file, `data_source: demo` everywhere |

## Manual and visual checks

- Ran the app in demo mode and checked every view with Playwright/Chromium at 1440×900 (desktop),
  1280×800 (landscape tablet) and 390×844 (phone): map current and history, analysis, planner, setup, budget, and
  the Home Assistant dashboard. Earlier screenshots were removed because they contained private location data. No JavaScript errors were reported in that earlier check.
- Built the Docker image and ran it with `compose.yaml` (read-only root filesystem, dropped capabilities):
  health check healthy, `scripts/backup.py` inside the container, graceful stop in under a second with the
  scheduler stopping cleanly, restart with the data intact.

- **Upgrade with production-like data** (October 2026): a copy of the previous version's schema (`b91b480`) filled
  with 57,074 observations, 34,712 ledger entries, 300 geometries, retired and hidden corridors. Checked: every old
  row and column unchanged, `PRAGMA integrity_check` ok, no foreign-key violations, pre-migration backup byte-for-byte
  equal in content to the original, an interrupted family assignment resumes, the previous code still runs a cycle
  and statistics on the upgraded database, `scripts/restore.py` reproduces the original. Start-up took about 4 s.
- **Real `docker compose` rebuild**: the old image ran with the data volume, then the new image was built and the
  container recreated (as Dockhand does): migration and backup log lines present, container healthy, row counts
  unchanged. Both rollbacks in [UPGRADING.md](UPGRADING.md) (code only, and restore of the pre-migration backup via
  compose and via plain `docker run`) were executed. `scripts/check_upgrade.py` was run against the volume mounted
  read-only.
- Visual check of the changed views (map with "Show all collected routes", analysis, planner, setup corridor editor
  and deviation list, budget) with Playwright/Chromium at desktop and phone width on demo data; no JavaScript errors.

## Not verified yet: needs the real API and real time

- **No live TomTom request was made.** The development environment's network policy blocked `api.tomtom.com`. The
  parser, error classification and request parameters follow the documented API but have only been exercised with
  synthetic fixtures. Run `scripts/live_verify.py --max-requests 2` first and check the recorded responses.
- **Map tiles** have not been loaded from TomTom; screenshots show routes without a base map.
- **Billing units and reset time**: confirm on the TomTom dashboard after the first day (about 576 requests a day
  with two confirmed corridors per direction; 192 a day until corridors are confirmed).
- **Live corridors** must be configured in Setup, previewed and confirmed by the administrator.
- **Deviation thresholds** (corridor tolerance, default 400 m; flagged when more than 5 % and at least 500 m of the
  route is outside it, or the length differs by more than 15 %; route families at 150 m / 97 % / 5 % length) and the 85 % "resembles" similarity are first
  guesses. Tune them once real geometry variations are visible.
- **Statistics and planning** become meaningful after several weeks of observations per weekday. With fewer than
  6 observations per slot the planner reports insufficient data.
- **Not deployed**: nothing has been installed on the Ubuntu/Dockhand server, and no continuous collection is running.

