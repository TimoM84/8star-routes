"""Route identity, zero-vs-missing preservation, deviations and parsing."""
import json

from app import geometry as geo
from app.demo import shape_coords
from app.tomtom import parse_routes
from conftest import confirm_corridors, load_fixture, outbound_only, tt_route


def run(env, slot="2026-10-20T06:00:00.000Z"):
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('schedule', ?, 'running')", (slot,)).lastrowid
    env.collector.run_cycle(cid, env.clock())
    return cid


def test_parse_fixture_preserves_zero_and_missing():
    routes = parse_routes(load_fixture("synthetic_unrestricted.json"))
    a, b = routes
    assert a.traffic_delay_s == 0 and a.traffic_length_m == 0  # real zeros
    assert a.free_flow_s == 1488 and a.historic_s == 1601 and a.live_s == 1563
    assert b.free_flow_s is None and b.historic_s is None and b.live_s is None  # absent, not zero
    assert a.departure_time == "2026-10-20T08:00:00+02:00"
    assert a.roads[:3] == ["N211", "Wippolderlaan", "A4"]
    assert b.traffic_sections == [{"start": 2, "end": 3, "category": "JAM", "delay_s": 95, "speed_kmh": 31, "magnitude": 1}]
    assert a.coords[1] == [52.31281, 5.10523]  # original precision kept


def test_zero_values_stored_as_zero_and_failures_as_null(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    def script(req, n):
        if req.target == "unrestricted":
            return env.client.ok([tt_route(shape_coords("a4", True), 1440, delay=0, free=1440)])
        if req.corridor_id == 1:
            return env.client.ok([tt_route(shape_coords("n470", True), 1600, delay=0)])  # no free-flow field
        return env.client.error(400, "invalid", msg="NO_ROUTE_FOUND: cannot route via point")
    env.client.script = script
    cid = run(env)
    rows = {r["corridor_id"]: r for r in env.db.all("SELECT * FROM observations WHERE cycle_id = ?", (cid,))}
    rec, c1, c2 = rows[None], rows[1], rows[2]
    assert rec["traffic_delay_s"] == 0 and rec["free_flow_s"] == 1440
    assert c1["traffic_delay_s"] == 0 and c1["free_flow_s"] is None
    assert c2["status"] == "failed" and c2["travel_time_s"] is None and c2["traffic_delay_s"] is None
    assert "NO_ROUTE_FOUND" in c2["failure_reason"]
    # The failed corridor is not replaced by the unrestricted route.
    assert c2["variant_id"] is None
    assert env.db.scalar("SELECT status FROM collection_cycles WHERE id = ?", (cid,)) == "partial"


def test_route_identity_survives_alternative_reordering(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    a4, n470, a12 = (shape_coords(k, True) for k in ("a4", "n470", "a12"))
    orders = [[(a4, 1500), (n470, 1600), (a12, 1700)], [(n470, 1450), (a12, 1550), (a4, 1650)], [(a12, 1400), (a4, 1500), (n470, 1600)]]
    cycles = []
    for i, order in enumerate(orders):
        env.client.script = lambda req, n, order=order: env.client.ok([tt_route(c, t) for c, t in order]) if req.target == "unrestricted" \
            else env.client.ok(env.client.default_routes(req))
        cycles.append(run(env, f"2026-10-20T06:{i * 15:02d}:00.000Z"))
    # Same geometry => same variant id, whatever position TomTom returned it in.
    by_geom = {}
    for cid in cycles:
        for o in env.db.all("SELECT variant_id, returned_index, role, resembles_corridor_id FROM observations WHERE cycle_id = ? AND target='unrestricted'", (cid,)):
            by_geom.setdefault(o["variant_id"], set()).add(o["returned_index"])
    assert len(by_geom) == 3
    assert all(len(ix) == 3 for ix in by_geom.values())  # each geometry appeared at index 0, 1 and 2
    # The recommendation is matched to corridors by geometry, never by index.
    recs = [env.db.one("SELECT resembles_corridor_id FROM observations WHERE cycle_id = ? AND role='recommended'", (c,))[0] for c in cycles]
    assert recs == [None, 1, 2]


def test_geometry_dedup(env):
    outbound_only(env.db)
    for i in range(5):
        run(env, f"2026-10-20T0{6 + i}:00:00.000Z")
    assert env.db.scalar("SELECT COUNT(*) FROM route_variants WHERE direction_id = 1") == 2
    assert env.db.scalar("SELECT COUNT(*) FROM observations WHERE variant_id IS NOT NULL") == 10
    assert env.db.scalar("SELECT seen_count FROM route_variants ORDER BY id LIMIT 1") == 5


def test_corridor_deviation_detection(env):
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    normal, detour = shape_coords("a12", True), shape_coords("a12", True, detour=True)
    env.client.script = lambda req, n: env.client.ok([tt_route(detour if req.corridor_id == 2 else shape_coords("n470", True), 1800)]) \
        if req.target == "corridor" else env.client.ok([tt_route(shape_coords("a4", True), 1500)])
    cid = run(env)
    c2 = env.db.one("SELECT * FROM observations WHERE cycle_id = ? AND corridor_id = 2", (cid,))
    c1 = env.db.one("SELECT * FROM observations WHERE cycle_id = ? AND corridor_id = 1", (cid,))
    assert c2["deviation_flag"] == 1 and c2["deviation_max_m"] > 1000
    assert c1["deviation_flag"] == 0
    assert env.db.scalar("SELECT COUNT(*) FROM events WHERE kind='corridor_deviation'") == 1
    # Small routing variations within the corridor are not flagged.
    jitter = [[a + 0.0004, b - 0.0004] for a, b in normal]
    d = geo.deviation(jitter, normal, 400)
    assert not d.flagged


def test_similarity_and_supporting_points():
    a4, n470 = shape_coords("a4", True), shape_coords("n470", True)
    assert geo.similarity(a4, a4) == 1.0
    assert geo.similarity(a4, n470) < 0.5
    sp = geo.supporting_points(a4, max_points=20)
    assert len(sp) <= 20 and sp[0] == a4[0] and sp[-1] == a4[-1]


def test_request_parameters_exclude_credentials(env):
    outbound_only(env.db)
    run(env)
    for (p,) in env.db.all("SELECT request_params_json FROM observations"):
        d = json.loads(p)
        assert "key" not in d["params"]
        assert d["params"]["computeTravelTimeFor"] == "all"
