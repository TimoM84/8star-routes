"""Route families, incidental routes ("Show all collected routes") and corridor lifecycle in the statistics."""

import json
import shutil
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import analysis, families
from app import db as dbmod
from app import geometry as geo
from app.budget import Budget
from app.config import Config
from app.db import Database, iso, migrate, utcnow
from app.demo import shape_coords
from app.main import create_app
from app.scheduler import purge_before
from app.seed import seed_demo
from conftest import ScriptedClient, confirm_corridors, outbound_only, tt_route
from test_analysis import collect_days

DAY = date(2026, 9, 22)


def jitter(coords, d=0.0002):
    return [[a + d, b - d] for a, b in coords]


def test_families_group_small_variations_and_separate_real_alternatives(env):
    a12 = shape_coords("a12", True)
    assert families.same_family(a12, jitter(a12))  # ~25 m offset: same road
    assert not families.same_family(a12, shape_coords("a12", True, detour=True))  # a real detour
    assert not families.same_family(a12, shape_coords("n470", True))


def test_alternatives_get_their_own_identity_and_duplicates_are_not_counted_twice(env):
    """Two genuinely different alternatives that both resemble corridor 2 stay separate; one that *is* corridor 2 is a duplicate."""
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    a12 = shape_coords("a12", True)
    detour = shape_coords("a12", True, detour=True)

    def script(req, n):
        if req.target == "unrestricted":
            routes = [tt_route(shape_coords("a4", True), 1500, 0, 1440),
                      tt_route(jitter(a12), 1550, 20, 1440),  # same road as corridor 2 (tiny geometry difference)
                      tt_route(detour, 1900, 20, 1440)]  # different road that still resembles corridor 2
            # TomTom's ordering of alternatives changes from cycle to cycle; identity must not.
            return env.client.ok([routes[0], *(routes[1:] if n % 2 else routes[:0:-1])])
        if req.corridor_id == 2:
            return env.client.ok([tt_route(a12, 1600, 30)])
        return env.client.ok([tt_route(shape_coords("n470", True), 1400, 0)])
    collect_days(env, 1, script)
    plain = analysis.stats(env.db, env.cfg, 1, DAY, DAY)
    full = analysis.stats(env.db, env.cfg, 1, DAY, DAY, extras=True)
    alts = [r for r in full["routes"] if r["key"].startswith("alt:")]
    # Only the detour has its own series; the jittered a12 copy is a duplicate of monitored corridor 2.
    assert len(alts) == 1, [r["key"] for r in alts]
    det_fam = env.db.scalar("SELECT v.family_id FROM observations o JOIN route_variants v ON v.id = o.variant_id "
                            "WHERE o.role='alternative' AND o.travel_time_s = 1900 LIMIT 1")
    assert alts[0]["key"] == f"alt:{det_fam}" and alts[0]["ok"] == 96 and alts[0]["travel_time_s"]["median"] == 1900
    assert "resembles" in alts[0]["name"] and alts[0]["monitored"] is False and alts[0]["fastest_share"] is None
    # One family for the detour regardless of its position (index 1 or 2) in TomTom's answer.
    assert env.db.scalar("SELECT COUNT(DISTINCT o.returned_index) FROM observations o JOIN route_variants v ON v.id = o.variant_id "
                         "WHERE v.family_id = ?", (det_fam,)) == 2
    # Monitored rows and comparisons are identical with or without incidental routes.
    for p in plain["routes"]:
        f = next(x for x in full["routes"] if x["key"] == p["key"])
        assert (p["ok"], p["fastest_count"], p["travel_time_s"], p["coverage"]) == (f["ok"], f["fastest_count"], f["travel_time_s"], f["coverage"])
    for k in ("comparable_cycles", "corridor_comparisons", "recommendation_changes"):
        assert plain[k] == full[k]
    # The map still lists every stored route individually, explaining the duplicate.
    cyc = env.db.scalar("SELECT MAX(id) FROM collection_cycles WHERE status='complete'")
    det = analysis.cycle_detail(env.db, env.cfg, cyc, 1)
    alt_routes = [r for r in det["routes"] if r["role"] == "alternative"]
    assert len(alt_routes) == 2
    assert {(r["duplicate_of"] or {}).get("key") for r in alt_routes} == {"corridor:2", None}
    assert {r["series_key"] for r in alt_routes} == {None, f"alt:{det_fam}"}


def test_deviating_corridor_routes_are_a_breakdown_not_extra_samples(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)

    def script(req, n):
        if req.target == "unrestricted":
            return env.client.ok([tt_route(shape_coords("a4", True), 1500, 0, 1440)])
        if req.corridor_id == 2:
            return env.client.ok([tt_route(shape_coords("a12", True, detour=n % 4 == 1), 1600 + (300 if n % 4 == 1 else 0), 30)])
        return env.client.ok([tt_route(shape_coords("n470", True), 1400, 0)])
    collect_days(env, 1, script)
    full = analysis.stats(env.db, env.cfg, 1, DAY, DAY, extras=True)
    r = {x["key"]: x for x in full["routes"]}
    devs = [x for x in full["routes"] if x["kind"] == "deviation"]
    assert len(devs) == 1 and devs[0]["subset_of"] == "corridor:2"
    flagged = env.db.scalar("SELECT COUNT(*) FROM observations WHERE corridor_id = 2 AND deviation_flag = 1")
    assert devs[0]["ok"] == flagged and r["corridor:2"]["ok"] == 96  # the corridor row still holds all its samples
    assert abs(devs[0]["share_of_parent"] - flagged / 96) < 1e-9
    # One deviation event per deviating route, not per observation.
    assert env.db.scalar("SELECT COUNT(*) FROM events WHERE kind='corridor_deviation'") == 1
    # The planner never offers a deviation (it is not a route one can choose) and never recommends incidental routes.
    env.clock.t = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    p = analysis.plan_arrival(env.db, env.cfg, 1, date(2026, 9, 23), 9 * 60, min_samples=1, same_weekday=False, extras=True)
    assert not any(x["key"].startswith("dev:") for x in p["routes"])
    inspect = analysis.corridor_deviations(env.db, 2)
    assert len(inspect) == 1 and inspect[0]["n"] == flagged and inspect[0]["max_m"] > 1000
    assert inspect[0]["median_travel_time_s"] == 1900


def test_short_harmless_geometry_difference_is_not_a_deviation():
    ref = shape_coords("a12", True)
    # A short stretch (a ramp or parallel service road) lies ~800 m away: beyond the tolerance, but too short to matter.
    bumped = [list(p) for p in ref]
    for i in range(150, 152):
        bumped[i] = [bumped[i][0] + 0.0075, bumped[i][1] - 0.004]
    d = geo.deviation(bumped, ref, 400)
    assert d.share_outside > 0 and d.outside_m < geo.DEVIATION_MIN_OUTSIDE_M and not d.flagged
    assert geo.deviation(bumped, ref, 400, share_limit=0, min_outside_m=0).flagged  # the stretch is visible to the check
    assert geo.deviation(shape_coords("a12", True, detour=True), ref, 400).flagged


def test_retired_corridor_is_not_expected_after_retirement(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    collect_days(env, 1)
    env.db.execute("UPDATE corridors SET status='retired', retired_utc='2026-09-22T22:00:00.000Z' WHERE id = 1")
    collect_days(env, 1, start=datetime(2026, 9, 22, 22, 0, 3, tzinfo=timezone.utc))
    st = analysis.stats(env.db, env.cfg, 1, date(2026, 9, 22), date(2026, 9, 23))
    c1 = next(r for r in st["routes"] if r["key"] == "corridor:1")
    assert c1["expected"] == c1["ok"] == 96 and c1["coverage"] == 1.0 and c1["not_collected"] == 0
    # With one corridor left, no corridor-vs-corridor comparisons are made after retirement.
    day2 = analysis.stats(env.db, env.cfg, 1, date(2026, 9, 23), date(2026, 9, 23))
    assert day2["corridor_comparisons"] == 0
    det = analysis.cycle_detail(env.db, env.cfg, env.db.scalar("SELECT MAX(id) FROM collection_cycles"), 1)
    assert "corridor:1" not in det["missing_keys"]


def test_hidden_corridor_with_history_does_not_break_statistics(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    # Corridor 1 is the fastest corridor all day.
    env.client.script = lambda req, n: env.client.ok(
        [tt_route(shape_coords("n470", True), 1300)] if req.corridor_id == 1 else env.client.default_routes(req))
    collect_days(env, 1)
    env.db.execute("UPDATE corridors SET status='retired', retired_utc='2026-09-23T00:00:00.000Z', hidden_utc='2026-09-23T00:00:00.000Z' WHERE id = 1")
    for extras in (False, True):
        st = analysis.stats(env.db, env.cfg, 1, DAY, DAY, extras=extras)
        assert "corridor:1" not in {r["key"] for r in st["routes"]}
        ds = analysis.day_series(env.db, env.cfg, 1, DAY, extras=extras)
        assert all("corridor:1" not in s["routes"] for s in ds["slots"])


def test_planner_answers_buffer_on_time_and_earlier_departures(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    from zoneinfo import ZoneInfo
    ams = ZoneInfo("Europe/Amsterdam")

    def script(req, n):
        loc = env.clock().astimezone(ams)
        m = loc.hour * 60 + loc.minute
        base = 1500 if m < 7 * 60 + 30 else 2400
        if req.target == "unrestricted":
            return env.client.ok([tt_route(shape_coords("a4", True), base)])
        if req.corridor_id == 1:  # slower but very steady
            return env.client.ok([tt_route(shape_coords("n470", True), 1800)])
        return env.client.ok([tt_route(shape_coords("a12", True), base + (n % 3) * 120)])
    collect_days(env, 21, script, start=datetime(2026, 8, 31, 22, 0, 3, tzinfo=timezone.utc))
    env.clock.t = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    p = analysis.plan_arrival(env.db, env.cfg, 1, date(2026, 9, 22), 8 * 60 + 30, pct=90, weeks=4, min_samples=6, same_weekday=False)
    assert p["state"] == "ok", p["state"]
    rec = p["recommendation"]
    assert rec["n"] >= 6 and rec["on_time_share"] == 1.0 and rec["buffer_s"] >= 0
    assert [e["minutes_earlier"] for e in rec["earlier"]] == [15, 30]
    assert p["comparison"]["most_reliable"] == "corridor:1"  # zero spread
    assert p["history"]["first_observation_utc"] and p["history"]["distinct_dates"] >= 6
    for r in p["routes"]:
        for s in r["slots"]:
            assert s["expected"] >= s["n"]


def test_hard_ceiling_override_never_exceeds_allowance(env):
    b = env.budget
    b.update_settings({"routing_hard_ceiling": 50000, "routing_other_apps_allowance": 1000})
    assert b.ceiling() == 19000
    st = b.status(6, True)
    assert st["routing"]["hard_ceiling_clamped"] and st["routing"]["ceiling"] == 19000
    b.update_settings({"routing_hard_ceiling": 5000})
    assert b.ceiling() == 5000 and not b.status(6, True)["routing"]["hard_ceiling_clamped"]
    b.update_settings({"routing_hard_ceiling": 0})
    assert b.ceiling() == 20000 - 2000 - 1000
    assert "spare_after_schedule" in st["forecast"] and isinstance(st["daily"], list)


def test_retention_purge_keeps_ledger_and_corridor_geometry(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    collect_days(env, 2)
    counted = Budget(env.db, env.cfg).ledger_count("2026-09")
    n = purge_before(env.db, "2026-09-23T00:00:00.000Z")
    assert n > 0
    assert env.db.scalar("SELECT COUNT(*) FROM observations WHERE intended_utc < '2026-09-23'") == 0
    assert Budget(env.db, env.cfg).ledger_count("2026-09") == counted  # budget history unaffected
    assert env.db.scalar("PRAGMA foreign_key_check") is None
    for (vid,) in env.db.all("SELECT reference_variant_id FROM corridors WHERE reference_variant_id IS NOT NULL"):
        assert env.db.scalar("SELECT 1 FROM route_variants WHERE id = ?", (vid,))


def test_upgrade_from_previous_schema_keeps_history_and_assigns_families(tmp_path, monkeypatch):
    """A database created by the previous release (migrations 001-002) upgrades without losing rows or making requests."""
    old_dir = tmp_path / "old_migrations"
    old_dir.mkdir()
    for f in ("001_initial.sql", "002_corridor_hidden.sql"):
        shutil.copy(dbmod.MIGRATIONS_DIR / f, old_dir / f)
    cfg = Config(data_dir=tmp_path, app_password="admin-password-1", session_secret="s", tomtom_api_key="k")
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", old_dir)
    db = Database(cfg.db_path)
    migrate(db, cfg.backup_dir)
    seed_demo(db)
    now = iso(utcnow())
    shapes = [shape_coords("a4", True), jitter(shape_coords("a4", True)), shape_coords("n470", True)]
    vids = []
    for i, c in enumerate(shapes):
        vids.append(db.execute("INSERT INTO route_variants(direction_id, fingerprint, geometry_z, point_count, length_m, first_seen_utc, "
                               "last_seen_utc, seen_count) VALUES (1, ?, ?, ?, ?, ?, ?, 1)",
                               (geo.fingerprint(c), geo.compress(c), len(c), int(geo.length_m(c)), now, now)).lastrowid)
    cyc = db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('schedule', ?, 'complete')", (now,)).lastrowid
    for i, v in enumerate(vids):
        db.execute("INSERT INTO observations(cycle_id, direction_id, target, role, variant_id, status, intended_utc, travel_time_s) "
                   "VALUES (?, 1, 'unrestricted', ?, ?, 'ok', ?, 1500)", (cyc, "recommended" if i == 0 else "alternative", v, now))
    db.execute("UPDATE corridors SET status='retired' WHERE id = 1")
    before = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in ("observations", "route_variants", "collection_cycles", "corridors")}
    db.close()
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", dbmod.Path(dbmod.__file__).parent / "migrations")
    client = ScriptedClient(utcnow)
    with TestClient(create_app(cfg, client=client, start_scheduler=False)) as tc:
        tc.post("/api/login", json={"password": "admin-password-1"})
        fams = tc.get("/api/families?direction_id=1").json()["families"]
        assert len(fams) == 2 and sum(f["variants"] for f in fams) == 3
        assert tc.get("/api/stats?direction_id=1&extras=true").status_code == 200
    assert client.calls == []
    db = Database(cfg.db_path)
    assert {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in before} == before
    assert db.scalar("SELECT COUNT(*) FROM route_variants WHERE family_id IS NULL") == 0
    assert db.scalar("SELECT retired_utc FROM corridors WHERE id = 1")
    assert list(cfg.backup_dir.glob("pre-migration-*.sqlite"))
    assert families.backfill(db) == 0  # idempotent
    db.close()


def test_new_read_endpoints_make_no_requests(tmp_path):
    cfg = Config(data_dir=tmp_path, app_password="admin-password-1", session_secret="s", tomtom_api_key="k")
    client = ScriptedClient(utcnow)
    app = create_app(cfg, client=client, start_scheduler=False)
    with TestClient(app) as tc:
        seed_demo(app.state.s.db)
        tc.post("/api/login", json={"password": "admin-password-1"})
        H = {"X-Requested-With": "8star-routes"}
        tc.post("/api/collect", json={}, headers=H)
        n = len(client.calls)
        b = tc.get("/api/budget").json()
        assert {"schedule", "daily"} <= set(b) and b["schedule"]["next_slot_utc"]
        for path in ("/api/families?direction_id=1", "/api/corridors/1/deviations", "/api/stats?direction_id=1&extras=true",
                     "/api/day?direction_id=1&extras=true", f"/api/plan?direction_id=1&date={date.today()}&extras=true",
                     "/api/export/observations.csv"):
            assert tc.get(path).status_code == 200, path
        assert "route_family_id" in tc.get("/api/export/observations.csv").text.splitlines()[0]
        assert tc.put("/api/corridors/1", json={"color": "red"}, headers=H).status_code == 400
        assert tc.put("/api/corridors/1", json={"color": "#123abc"}, headers=H).status_code == 200
        assert len(client.calls) == n


def test_failed_migration_is_rolled_back_completely(tmp_path, monkeypatch):
    """If migration 003 fails half-way (or the container is killed), nothing of it is applied and no data changes."""
    old_dir = tmp_path / "m"
    old_dir.mkdir()
    for f in ("001_initial.sql", "002_corridor_hidden.sql"):
        shutil.copy(dbmod.MIGRATIONS_DIR / f, old_dir / f)
    real_dir = dbmod.MIGRATIONS_DIR
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", old_dir)
    db = Database(tmp_path / "routes.sqlite")
    migrate(db, tmp_path / "backups")
    seed_demo(db)
    (old_dir / "003_route_families.sql").write_text((real_dir / "003_route_families.sql").read_text() + "\nSELECT no_such_function();\n")
    import pytest
    with pytest.raises(Exception):
        migrate(db, tmp_path / "backups")
    assert db.scalar("SELECT COUNT(*) FROM schema_migrations") == 2
    assert "retired_utc" not in [r[1] for r in db.all("PRAGMA table_info(corridors)")]
    assert not db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name = 'route_families'")
    assert db.scalar("SELECT COUNT(*) FROM corridors") == 4
    assert list((tmp_path / "backups").glob("pre-migration-*.sqlite"))  # written before anything changed
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", real_dir)
    assert migrate(db, tmp_path / "backups") == ["003_route_families"]
    db.close()
