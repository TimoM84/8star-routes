import json
import logging
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database, backup, restore, utcnow
from app.main import create_app
from app.tomtom import RouteRequest, TomTomClient
from conftest import ScriptedClient
from app.seed import seed_demo

KEY = "SECRET-KEY-should-never-leak-123"
H = {"X-Requested-With": "8star-routes"}


@pytest.fixture
def app_env(tmp_path):
    cfg = Config(data_dir=tmp_path, app_password="admin-password-1", session_secret="s", tomtom_api_key=KEY,
                 api_read_token="read-token", embed_token="embed-token", retry_delay_seconds=0)
    client = ScriptedClient(utcnow)
    app = create_app(cfg, client=client, start_scheduler=False)
    with TestClient(app) as tc:
        seed_demo(app.state.s.db)
        yield tc, client, cfg


def login(tc):
    r = tc.post("/api/login", json={"password": "admin-password-1"})
    assert r.status_code == 200


def test_auth_required(app_env):
    tc, _, _ = app_env
    assert tc.get("/api/meta").status_code == 401
    assert tc.get("/api/v1/latest").status_code == 401
    assert tc.get("/healthz").status_code == 200
    assert tc.post("/api/login", json={"password": "wrong"}).status_code == 401


def test_read_token_is_read_only(app_env):
    tc, _, _ = app_env
    auth = {"Authorization": "Bearer read-token"}
    assert tc.get("/api/v1/latest", headers=auth).status_code == 200
    assert tc.get("/api/latest?direction_id=1", headers={"X-Embed-Token": "embed-token"}).status_code == 200
    assert tc.post("/api/collect", json={}, headers={**auth, **H}).status_code == 403
    assert tc.put("/api/budget", json={"routing_safety_reserve": 0}, headers={**auth, **H}).status_code == 403


def test_csrf_header_required_for_changes(app_env):
    tc, _, _ = app_env
    login(tc)
    assert tc.put("/api/collection", json={"enabled": False}).status_code == 403
    assert tc.put("/api/collection", json={"enabled": False}, headers=H).status_code == 200


def test_manual_collect_then_history_without_new_requests(app_env):
    tc, client, _ = app_env
    login(tc)
    r = tc.post("/api/collect", json={"direction_ids": [1]}, headers=H)
    assert r.status_code == 200, r.text
    n = len(client.calls)
    assert n == 1  # only unrestricted (corridors are unconfirmed drafts)
    cid = r.json()["cycle_id"]
    # Every read endpoint the map, analysis and dashboard use: none may call TomTom.
    today = date.today().isoformat()
    for url in ["/api/latest?direction_id=1", f"/api/cycles/{cid}?direction_id=1", f"/api/day?direction_id=1&date={today}",
                "/api/stats?direction_id=1", "/api/heatmap?direction_id=1", f"/api/plan?direction_id=1&date={today}&arrive=09:00",
                "/api/meta", "/api/budget", "/api/v1/latest", "/api/v1/latest/outbound", "/api/setup", "/api/variants?direction_id=1",
                "/api/export/observations.csv", "/api/export/geometries.geojson", "/", "/dashboard"]:
        resp = tc.get(url)
        assert resp.status_code == 200, (url, resp.text)
    assert len(client.calls) == n
    v = tc.get(f"/api/cycles/{cid}?direction_id=1").json()
    vid = v["routes"][0]["variant_id"]
    assert len(tc.get(f"/api/variants/{vid}").json()["coords"]) > 10
    assert len(client.calls) == n
    # Rate limit on manual refreshes
    assert tc.post("/api/collect", json={}, headers=H).status_code == 429


def test_v1_latest_shape(app_env):
    tc, _, _ = app_env
    login(tc)
    tc.post("/api/collect", json={}, headers=H)
    d = tc.get("/api/v1/latest/outbound").json()
    assert d["data_source"] == "live"
    assert d["recommended_travel_time_min"] == 25.0
    assert d["recommended_traffic_delay_min"] == 0.0  # zero, not null
    assert d["stale"] is False


def test_corridor_lifecycle(app_env):
    tc, client, _ = app_env
    login(tc)
    r = tc.post("/api/corridors", json={"direction_id": 1, "name": "My corridor", "waypoints": [[52.02, 4.3]]}, headers=H)
    cid = r.json()["id"]
    assert tc.post(f"/api/corridors/{cid}/confirm", json={}, headers=H).status_code == 409  # preview first
    pr = tc.post(f"/api/corridors/{cid}/preview", json={}, headers=H)
    assert pr.status_code == 200 and pr.json()["ok"]
    assert client.calls[-1].waypoints == [[52.02, 4.3]]
    assert tc.post(f"/api/corridors/{cid}/confirm", json={}, headers=H).status_code == 200
    # Confirmed corridors cannot silently change route.
    assert tc.put(f"/api/corridors/{cid}", json={"waypoints": [[52.0, 4.2]]}, headers=H).status_code == 409
    assert tc.put(f"/api/corridors/{cid}", json={"name": "Renamed"}, headers=H).status_code == 200
    # Now collected
    meta = tc.get("/api/meta").json()
    assert meta["requests_per_cycle"] == 3
    assert tc.post(f"/api/corridors/{cid}/retire", json={}, headers=H).status_code == 200
    assert tc.get("/api/meta").json()["requests_per_cycle"] == 2


def test_corridor_from_variant_needs_no_request(app_env):
    tc, client, _ = app_env
    login(tc)
    tc.post("/api/collect", json={"direction_ids": [1]}, headers=H)
    n = len(client.calls)
    vid = tc.get("/api/variants?direction_id=1&key=unrestricted").json()["variants"][0]["id"]
    cid = tc.post("/api/corridors/from-variant", json={"variant_id": vid, "name": "Exact A4"}, headers=H).json()["id"]
    assert len(client.calls) == n
    assert tc.post(f"/api/corridors/{cid}/confirm", json={}, headers=H).status_code == 200
    s = tc.get("/api/setup").json()
    c = next(c for d in s["directions"] for c in d["corridors"] if c["id"] == cid)
    assert c["method"] == "reconstruct" and c["supporting_points_count"] > 2


def test_tile_proxy_is_not_an_open_proxy(app_env):
    tc, _, _ = app_env
    login(tc)
    assert tc.get("/tiles/other/1/0/0.png").status_code == 404
    assert tc.get("/tiles/map/3/9/0.png").status_code == 400
    assert tc.get("/tiles/map/1/0/0.png?url=https://evil.example").status_code in (502, 200, 503)
    # unauthenticated is refused
    tc.post("/api/logout", json={}, headers=H)
    tc.cookies.clear()
    assert tc.get("/tiles/map/1/0/0.png").status_code == 401


def test_tile_proxy_counts_and_hides_key(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"PNG", headers={"Cache-Control": "max-age=600", "Content-Type": "image/png", "ETag": "x"})
    cfg = Config(data_dir=tmp_path, app_password="admin-password-1", session_secret="s", tomtom_api_key=KEY)
    app = create_app(cfg, client=ScriptedClient(utcnow), start_scheduler=False)
    app.state.s.tiles.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(app) as tc:
        login(tc)
        r = tc.get("/tiles/map/2/1/1.png")
        assert r.status_code == 200 and r.headers["cache-control"] == "max-age=600" and r.headers["etag"] == "x"
        assert KEY not in r.text and KEY not in json.dumps(dict(r.headers))
        assert "api.tomtom.com/map/1/tile/basic/main/2/1/1.png" in seen[0]
        assert tc.get("/api/budget").json()["tiles"]["map"]["used"] == 1


def test_csv_and_geojson_exports(app_env):
    tc, _, _ = app_env
    login(tc)
    tc.post("/api/collect", json={}, headers=H)
    csv_text = tc.get("/api/export/observations.csv").text
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("observation_id,data_source")
    assert len(lines) == 1 + 4  # 2 directions x (recommendation + 1 alternative)
    gj = tc.get("/api/export/geometries.geojson").json()
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) >= 2
    lon, lat = gj["features"][0]["geometry"]["coordinates"][0]
    assert 5 <= lon < 6 and 51 < lat < 53  # Fictional geometry; GeoJSON order is lon, lat.


def test_secrets_not_in_logs_or_errors(caplog):
    caplog.set_level(logging.DEBUG)

    def boom(request):
        raise httpx.ConnectError(f"failed to connect {request.url}")

    c = TomTomClient(KEY, transport=httpx.MockTransport(boom))
    req = RouteRequest(1, "unrestricted", (52.0, 4.1), (52.1, 4.5), max_alternatives=2)
    res = c.calculate_route(req)
    assert not res.ok and res.error_kind == "network"
    assert KEY not in res.error_message
    assert KEY not in caplog.text
    assert "key" not in json.dumps(req.sanitized())

    def forbidden(request):
        return httpx.Response(403, json={"detailedError": {"code": "Forbidden", "message": f"Account Over Queries Per Month Limit for key {KEY}"}})
    res = TomTomClient(KEY, transport=httpx.MockTransport(forbidden)).calculate_route(req)
    assert res.error_kind == "quota" and KEY not in res.error_message

    def qps(request):
        return httpx.Response(403, text="<h1>Developer Over Qps</h1>")
    assert TomTomClient(KEY, transport=httpx.MockTransport(qps)).calculate_route(req).error_kind == "rate_limited"

    def bad_key(request):
        return httpx.Response(403, text="<h1>Developer Inactive</h1>")
    assert TomTomClient(KEY, transport=httpx.MockTransport(bad_key)).calculate_route(req).error_kind == "auth"


def test_client_parses_success_and_retry_after():
    def ok(request):
        assert request.url.params["key"] == KEY
        assert request.url.params["computeTravelTimeFor"] == "all"
        assert request.headers["Tracking-ID"]
        return httpx.Response(200, json={"routes": [{"summary": {"travelTimeInSeconds": 1}, "legs": []}]}, headers={"Tracking-ID": "abc"})
    req = RouteRequest(1, "corridor", (52.0, 4.1), (52.1, 4.5), corridor_id=1, supporting_points=[[52.05, 4.3], [52.06, 4.31]])
    res = TomTomClient(KEY, transport=httpx.MockTransport(ok)).calculate_route(req)
    assert res.ok and res.tracking_id == "abc"
    assert req.method() == "POST" and "maxAlternatives" not in req.params()

    def limited(request):
        return httpx.Response(429, headers={"Retry-After": "12"})
    res = TomTomClient(KEY, transport=httpx.MockTransport(limited)).calculate_route(req)
    assert res.error_kind == "rate_limited" and res.retry_after_s == 12 and res.retriable


def test_persistence_backup_and_restore(tmp_path):
    cfg = Config(data_dir=tmp_path, app_password="admin-password-1", session_secret="s", tomtom_api_key=KEY)
    app = create_app(cfg, client=ScriptedClient(utcnow), start_scheduler=False)
    with TestClient(app) as tc:
        seed_demo(app.state.s.db)
        login(tc)
        tc.post("/api/collect", json={}, headers=H)
        b = tc.post("/api/backup", json={}, headers=H).json()  # online backup while running
        tc.put("/api/journeys/1", json={"name": "Changed after backup"}, headers=H)
    # App stopped: restore the backup over the live database.
    restore(cfg.backup_dir / b["file"], cfg.db_path)
    with TestClient(create_app(cfg, client=ScriptedClient(utcnow), start_scheduler=False)) as tc:
        login(tc)
        s = tc.get("/api/setup").json()
        assert s["journeys"][0]["name"] == "Demo origin ↔ Demo destination"
        assert tc.get("/api/latest?direction_id=1").json()["detail"]["routes"]
        assert tc.get("/api/budget").json()["routing"]["ledger_counted"] == 2


def test_restart_keeps_data_and_does_not_reseed(tmp_path):
    cfg = Config(data_dir=tmp_path, app_password="admin-password-1", session_secret="s", tomtom_api_key=KEY)
    for i in range(2):
        app = create_app(cfg, client=ScriptedClient(utcnow), start_scheduler=False)
        with TestClient(app) as tc:
            login(tc)
            if i == 0:
                seed_demo(app.state.s.db)
                tc.put("/api/journeys/1", json={"name": "Edited name"}, headers=H)
            else:
                s = tc.get("/api/setup").json()
                assert len(s["journeys"]) == 1 and s["journeys"][0]["name"] == "Edited name"
    db = Database(cfg.db_path)
    assert db.scalar("SELECT COUNT(*) FROM schema_migrations") == 1
    db.close()


def test_backup_is_consistent_while_writing(tmp_path):
    db = Database(tmp_path / "x.sqlite")
    db.execute("CREATE TABLE t(v)")
    for i in range(200):
        db.execute("INSERT INTO t VALUES (?)", (i,))
    p = backup(db, tmp_path / "b")
    check = Database(p)
    assert check.scalar("SELECT COUNT(*) FROM t") == 200


def test_demo_mode_is_labelled_and_separate(tmp_path):
    cfg = Config(app_mode="demo", data_dir=tmp_path, app_password="admin-password-1", session_secret="s")
    assert cfg.db_path.name == "demo.sqlite"
    app = create_app(cfg, start_scheduler=False, demo_days=1)
    with TestClient(app) as tc:
        login(tc)
        import time
        for _ in range(100):
            if not tc.get("/healthz").json()["demo_generating"]:
                break
            time.sleep(0.3)
        assert tc.get("/api/meta").json()["data_source"] == "demo"
        assert "demo" in tc.get("/api/export/observations.csv").headers["content-disposition"]
    assert not (tmp_path / "routes.sqlite").exists()


def test_live_first_run_has_no_seeded_locations_and_setup_persists(tmp_path):
    cfg = Config(data_dir=tmp_path, app_password="admin-password-1", session_secret="s", tomtom_api_key=KEY)
    client = ScriptedClient(utcnow)
    app = create_app(cfg, client=client, start_scheduler=False)
    with TestClient(app) as tc:
        login(tc)
        setup = tc.get("/api/setup").json()
        assert setup["journeys"] == [] and setup["directions"] == []
        assert tc.get("/api/meta").json()["requests_per_cycle"] == 0
        assert tc.post("/api/collect", json={}, headers=H).status_code == 400
        assert client.calls == []
        private_journey = {
            "name": "Private commute", "origin_label": "Home", "dest_label": "Work",
            "origin_lat": 51.1, "origin_lon": 5.1, "dest_lat": 51.2, "dest_lon": 5.2, "enabled": True,
        }
        assert tc.post("/api/journeys", json=private_journey, headers=H).status_code == 200
        setup = tc.get("/api/setup").json()
        assert len(setup["directions"]) == 2
        assert all(d["enabled"] and d["corridors"] == [] for d in setup["directions"])
        assert client.calls == []  # Saving a private location never sends a routing request.
    app = create_app(cfg, client=client, start_scheduler=False)
    with TestClient(app) as tc:
        login(tc)
        saved = tc.get("/api/setup").json()["journeys"]
        assert len(saved) == 1 and saved[0]["name"] == "Private commute"
        assert saved[0]["origin_lat"] == 51.1 and saved[0]["dest_lon"] == 5.2
        assert saved[0]["origin_label"] == "Home" and saved[0]["dest_label"] == "Work"
