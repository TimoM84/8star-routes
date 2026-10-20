from datetime import date, datetime, timedelta, timezone

from app import analysis
from app.analysis import percentile
from app.demo import shape_coords
from app.scheduler import Scheduler
from conftest import confirm_corridors, outbound_only, tt_route


def collect_days(env, days, script=None, start=datetime(2026, 9, 21, 22, 0, 3, tzinfo=timezone.utc)):
    if script:
        env.client.script = script
    s = Scheduler(lambda: env.db, env.cfg, lambda db: env.collector, clock=env.clock)
    t = start
    while t < start + timedelta(days=days):
        env.clock.t = t
        s.step(env.db, env.collector, t)
        t += timedelta(minutes=15)


def test_percentile_linear():
    assert percentile([], 90) is None
    assert percentile([5], 90) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(1, 11)), 90) == 9.1


def test_comparisons_with_missing_routes_do_not_inflate_reliability(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)

    def script(req, n):
        if req.target == "unrestricted":
            return env.client.ok([tt_route(shape_coords("a4", True), 1500, 0, 1440)])
        if req.corridor_id == 1:
            # Corridor 1 is fastest, but fails half the time (exactly when it would be slow is unknowable).
            return env.client.error(500, "server") if n % 2 == 0 else env.client.ok([tt_route(shape_coords("n470", True), 1400, 0)])
        return env.client.ok([tt_route(shape_coords("a12", True), 1600, 30)])
    env.cfg.max_retries = 0
    collect_days(env, 1, script)
    st = analysis.stats(env.db, env.cfg, 1, date(2026, 9, 22), date(2026, 9, 22))
    r = {x["key"]: x for x in st["routes"]}
    c1, c2 = r["corridor:1"], r["corridor:2"]
    assert c1["expected"] == c2["expected"] == 96
    assert c1["failed"] > 0 and c1["coverage"] < 0.75
    assert c2["coverage"] == 1.0
    # Corridor 1 wins only the cycles where it has data, and the rest are reported as no-data, not as wins.
    # Corridor 1 only "wins" cycles where both corridors have data; the others are undetermined, not wins.
    assert c1["fastest_count"] == c1["ok"]
    assert st["undetermined_comparisons"] == c1["failed"]
    assert c1["fastest_share"] == c1["ok"] / 96 < 0.75
    assert c2["fastest_count"] == 0
    assert c1["no_data_in_compared_cycles"] == c1["failed"]
    assert c2["traffic_delay_s"]["median"] == 30


def test_missing_slots_counted(env):
    outbound_only(env.db)
    collect_days(env, 1)
    # An outage: delete 8 cycles, then let the scheduler fill them as 'missed'
    where = "slot_utc >= '2026-09-22T10:00' AND slot_utc < '2026-09-22T12:00'"
    env.db.execute(f"UPDATE request_ledger SET cycle_id = NULL WHERE cycle_id IN (SELECT id FROM collection_cycles WHERE {where})")
    env.db.execute(f"DELETE FROM collection_cycles WHERE {where}")
    st = analysis.stats(env.db, env.cfg, 1, date(2026, 9, 22), date(2026, 9, 22))
    rec = st["routes"][0]
    assert rec["expected"] == 96 and rec["ok"] == 88 and rec["not_collected"] == 8
    day = analysis.day_series(env.db, env.cfg, 1, date(2026, 9, 22))
    statuses = [s["cycle_status"] for s in day["slots"]]
    assert statuses.count("no_record") == 8
    assert all(s["routes"] == {} for s in day["slots"] if s["cycle_status"] == "no_record")


def test_recommendation_change_counting(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    flip = {"n": 0}

    def script(req, n):
        if req.target == "unrestricted":
            flip["n"] += 1
            k = "n470" if (flip["n"] // 4) % 2 else "a12"  # changes every 4 cycles
            return env.client.ok([tt_route(shape_coords(k, True), 1500)])
        return env.client.ok(env.client.default_routes(req))
    collect_days(env, 1, script)
    st = analysis.stats(env.db, env.cfg, 1, date(2026, 9, 22), date(2026, 9, 22))
    assert st["recommendation_changes"] == 23 or st["recommendation_changes"] == 24


def test_planner_insufficient_data(env):
    outbound_only(env.db)
    collect_days(env, 2)
    env.clock.t = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    p = analysis.plan_arrival(env.db, env.cfg, 1, date(2026, 9, 29), 9 * 60, min_samples=4)
    assert p["state"] == "insufficient_data"
    assert p["recommendation"] is None


def test_planner_uses_slot_specific_durations(env):
    """Travel time rises steeply after 07:30 local: the planner must not assume a constant duration."""
    outbound_only(env.db)
    from zoneinfo import ZoneInfo
    ams = ZoneInfo("Europe/Amsterdam")

    def script(req, n):
        loc = env.clock().astimezone(ams)
        m = loc.hour * 60 + loc.minute
        t = 1500 if m < 7 * 60 + 30 else 3000
        return env.client.ok([tt_route(shape_coords("a4", True), t)])
    collect_days(env, 35, script, start=datetime(2026, 8, 1, 22, 0, 3, tzinfo=timezone.utc))
    env.clock.t = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
    p = analysis.plan_arrival(env.db, env.cfg, 1, date(2026, 9, 8), 8 * 60 + 30, pct=90, weeks=8, min_samples=4)
    assert p["state"] == "ok"
    rec = p["recommendation"]
    # 07:45 + 50 min = 08:35 is too late; 07:30 + 50 min = 08:20 is the latest feasible.
    # A planner assuming the 25-minute off-peak duration would wrongly say 08:00.
    assert rec["departure_local"].endswith("07:30+02:00"), p
    slots = {s["departure_local"][-11:-6]: s for s in p["routes"][0]["slots"]}
    assert slots["07:15"]["quantile_s"] == 1500 and slots["07:45"]["quantile_s"] == 3000
    assert not slots["08:00"]["meets_target"]
    assert rec["n"] >= 4
    assert "same weekday" in p["history"]["matching"]


def test_latest_reads_stored_data_only(env):
    outbound_only(env.db)
    collect_days(env, 1)
    n = len(env.client.calls)
    lt = analysis.latest(env.db, env.cfg, 1, now=env.clock())
    assert lt["detail"]["routes"]
    assert len(env.client.calls) == n
