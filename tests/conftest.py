import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import geometry as geo  # noqa: E402
from app.budget import Budget  # noqa: E402
from app.collector import Collector, upsert_variant  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import Database, iso, migrate  # noqa: E402
from app.demo import shape_coords  # noqa: E402
from app.seed import seed_demo  # noqa: E402
from app.tomtom import ParsedRoute, RawResult  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def tt_route(coords, travel, delay=0, free=None, historic=None, live=None, length=None, sections=None):
    s = {"lengthInMeters": length if length is not None else int(geo.length_m(coords)), "travelTimeInSeconds": travel,
         "trafficDelayInSeconds": delay, "trafficLengthInMeters": 0,
         "departureTime": "2026-10-20T08:00:00+02:00", "arrivalTime": "2026-10-20T08:30:00+02:00"}
    if free is not None:
        s["noTrafficTravelTimeInSeconds"] = free
    if historic is not None:
        s["historicTrafficTravelTimeInSeconds"] = historic
    if live is not None:
        s["liveTrafficIncidentsTravelTimeInSeconds"] = live
    return {"summary": s, "legs": [{"points": [{"latitude": a, "longitude": b} for a, b in coords]}], "sections": sections or []}


class ScriptedClient:
    """Stands in for TomTom. `script(req, n)` returns a RawResult; every call is recorded."""

    configured = True

    def __init__(self, clock, script=None):
        self.clock = clock
        self.calls = []
        self.script = script or (lambda req, n: self.ok(self.default_routes(req)))

    def ok(self, routes):
        now = self.clock()
        return RawResult(True, 200, now, now + timedelta(milliseconds=200), "trk-ok", {"routes": routes})

    def error(self, status, kind, retry_after=None, msg="error"):
        now = self.clock()
        return RawResult(False, status, now, now, "trk-err", None, kind, msg, retry_after)

    def default_routes(self, req):
        if req.target == "unrestricted":
            return [tt_route(shape_coords("a4", True), 1500, 0, 1440), tt_route(shape_coords("n470", True), 1700, 80, 1620)]
        key = "n470" if req.corridor_id % 2 == 1 else "a12"
        return [tt_route(shape_coords(key, True), 1800, 60, 1700)]

    def calculate_route(self, req):
        self.calls.append(req)
        return self.script(req, len(self.calls))


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 20, 6, 0, 3, tzinfo=timezone.utc))


@pytest.fixture
def env(tmp_path, clock):
    cfg = Config(data_dir=tmp_path, app_password="test-password-123", session_secret="x", retry_delay_seconds=0)
    db = Database(cfg.db_path)
    migrate(db, cfg.backup_dir)
    db.set_setting("first_period", "2026-10")
    seed_demo(db)
    budget = Budget(db, cfg, clock)
    client = ScriptedClient(clock)
    collector = Collector(db, cfg, budget, client, clock=clock, sleep=lambda s: True)

    class E:
        pass

    e = E()
    e.cfg, e.db, e.budget, e.client, e.collector, e.clock = cfg, db, budget, client, collector, clock
    return e


def confirm_corridors(db, clock, only_outbound=True):
    """Confirm both seeded corridors of the outbound direction with demo geometries as references."""
    rows = db.all("SELECT c.id, c.direction_id, d.code FROM corridors c JOIN directions d ON d.id = c.direction_id ORDER BY c.id")
    out = []
    for r in rows:
        if only_outbound and r["code"] != "outbound":
            continue
        key = "n470" if r["id"] % 2 == 1 else "a12"
        coords = shape_coords(key, r["code"] == "outbound")
        pr = ParsedRoute(0, coords, None, None, None, None, None, None, None, None, None, [], [])
        vid = upsert_variant(db, r["direction_id"], pr, clock())
        db.execute("UPDATE corridors SET status='confirmed', reference_variant_id=?, preview_variant_id=?, confirmed_utc=? WHERE id=?",
                   (vid, vid, iso(clock() - timedelta(days=60)), r["id"]))
        out.append(r["id"])
    return out


def outbound_only(db):
    db.execute("UPDATE directions SET enabled = CASE WHEN code='outbound' THEN 1 ELSE 0 END")


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())
