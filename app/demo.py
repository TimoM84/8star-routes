"""Demo mode: a synthetic TomTom stand-in and generated history.

Everything produced here is SYNTHETIC. Geometries are rough hand-drawn lines
between approximate points, and travel times come from a simple formula with
random noise. Demo data lives in a separate database file (demo.sqlite) and
is never mixed with real observations.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import geometry as geo
from .budget import Budget
from .collector import Collector, corridor_rows, direction_rows
from .config import Config
from .db import Database, iso
from .seed import DEMO_JOURNEY, seed_demo
from .tomtom import RawResult, RouteRequest

HOME = (DEMO_JOURNEY["origin_lat"], DEMO_JOURNEY["origin_lon"])
WORK = (DEMO_JOURNEY["dest_lat"], DEMO_JOURNEY["dest_lon"])

# Entirely fictional shapes, not routes to any user's home or workplace.
SHAPES = {
    "a4": {"via": [(52.31, 5.04), (52.33, 5.09), (52.34, 5.14), (52.35, 5.19), (52.35, 5.24), (52.34, 5.29)],
           "roads": ["Demo central road"], "free": 1440, "peak": 1.0},
    "n470": {"via": [(52.28, 5.06), (52.28, 5.13), (52.29, 5.18), (52.31, 5.22), (52.32, 5.28)],
             "roads": ["Demo south road"], "free": 1620, "peak": 0.7},
    "a12": {"via": [(52.33, 5.04), (52.36, 5.10), (52.37, 5.17), (52.37, 5.23), (52.35, 5.29)],
            "roads": ["Demo north road"], "free": 1740, "peak": 0.85},
}


def _densify(points: list[tuple[float, float]], step_m: float = 60) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in zip(points, points[1:]):
        n = max(1, int(geo.haversine(a, b) / step_m))
        for i in range(n):
            t = i / n
            out.append([round(a[0] + (b[0] - a[0]) * t, 5), round(a[1] + (b[1] - a[1]) * t, 5)])
    out.append([round(points[-1][0], 5), round(points[-1][1], 5)])
    return out


def shape_coords(key: str, outbound: bool, detour: bool = False) -> list[list[float]]:
    via = list(SHAPES[key]["via"])
    if detour:  # a local diversion in the middle of the route, used to demonstrate deviations
        mid = len(via) // 2
        a = via[mid]
        via.insert(mid + 1, (a[0] + 0.012, a[1] + 0.01))
    pts = [HOME, *via, WORK]
    if not outbound:
        pts = list(reversed(pts))
    return _densify(pts)


def _peak(minute: float, centre: float, width: float) -> float:
    return math.exp(-((minute - centre) / width) ** 2)


class FakeTomTom:
    """Answers RouteRequests with TomTom-shaped synthetic responses."""

    configured = True

    def __init__(self, clock: Callable[[], datetime], tz, seed: int = 7, failure_rate: float = 0.01):
        self.clock = clock
        self.tz = tz
        self.seed = seed
        self.failure_rate = failure_rate
        self.allow_detours = True

    def _shape_for(self, req: RouteRequest) -> str:
        if req.target == "unrestricted":
            return "a4"
        wps = req.waypoints or req.supporting_points or []
        # pick the shape whose line passes closest to the first via point
        if not wps:
            return "a4"
        p = wps[len(wps) // 2]
        return min(SHAPES, key=lambda k: min(geo.haversine(p, q) for q in SHAPES[k]["via"]))

    def _times(self, key: str, outbound: bool, now: datetime, rng: random.Random) -> tuple[int, int, int]:
        loc = now.astimezone(self.tz)
        minute = loc.hour * 60 + loc.minute
        weekday = loc.weekday() < 5
        s = SHAPES[key]
        free = s["free"] * (1 if outbound else 1.02)
        if weekday:
            centre = 8 * 60 + 5 if outbound else 17 * 60 + 15
            load = _peak(minute, centre, 55) * (0.9 if loc.weekday() != 1 else 1.15) * s["peak"]
            load += 0.25 * _peak(minute, 17 * 60 if outbound else 8 * 60, 60) * s["peak"]
        else:
            load = 0.18 * _peak(minute, 13 * 60, 150) * s["peak"]
        jam = 0.0
        if key == "a4" and weekday and rng.random() < 0.06:
            jam = rng.uniform(0.2, 0.6)  # incident on the A4/A12 now and then
        delay = max(0, int(free * (load * 0.9 + jam) + rng.gauss(0, 25)))
        historic = int(free + free * load * 0.8)
        return int(free), delay, historic

    def calculate_route(self, req: RouteRequest) -> RawResult:
        now = self.clock()
        outbound = self._outbound(req)
        rng = random.Random(f"{self.seed}:{int(now.timestamp()) // 60}:{req.direction_id}:{req.corridor_id}")
        if rng.random() < self.failure_rate:
            return RawResult(False, 500, now, now + timedelta(milliseconds=300), "demo-tracking", None, "server", "synthetic server error")
        keys = ["a4", "n470", "a12"] if req.target == "unrestricted" else [self._shape_for(req)]
        routes = []
        for k in keys:
            free, delay, historic = self._times(k, outbound, now, random.Random(f"{self.seed}:{int(now.timestamp()) // 60}:{k}:{outbound}"))
            detour = self.allow_detours and req.target == "corridor" and k == "a12" and rng.random() < 0.03
            coords = shape_coords(k, outbound, detour)
            travel = free + delay
            length = int(geo.length_m(coords))
            sections = []
            if delay > 120:
                a = len(coords) // 3
                sections.append({"startPointIndex": a, "endPointIndex": a + len(coords) // 6, "sectionType": "TRAFFIC",
                                 "simpleCategory": "JAM", "effectiveSpeedInKmh": 25, "delayInSeconds": delay, "magnitudeOfDelay": 2})
            dep = now.astimezone(self.tz)
            routes.append({
                "summary": {
                    "lengthInMeters": length, "travelTimeInSeconds": travel, "trafficDelayInSeconds": delay,
                    "trafficLengthInMeters": 2500 if delay > 120 else 0,
                    "departureTime": dep.isoformat(timespec="seconds"),
                    "arrivalTime": (dep + timedelta(seconds=travel)).isoformat(timespec="seconds"),
                    "noTrafficTravelTimeInSeconds": free, "historicTrafficTravelTimeInSeconds": historic,
                    "liveTrafficIncidentsTravelTimeInSeconds": travel,
                },
                "legs": [{"points": [{"latitude": a, "longitude": b} for a, b in coords]}],
                "sections": sections,
                "guidance": {"instructions": [{"roadNumbers": [r] if r[0] in "AN" and r[1:].isdigit() else [], "street": "" if r[0] in "AN" and r[1:].isdigit() else r}
                                              for r in (SHAPES[k]["roads"] if outbound else list(reversed(SHAPES[k]["roads"])))]},
            })
        routes.sort(key=lambda r: r["summary"]["travelTimeInSeconds"])
        data = {"formatVersion": "0.0.12", "_demo": "synthetic", "routes": routes}
        return RawResult(True, 200, now, now + timedelta(milliseconds=400), f"demo-{int(now.timestamp())}", data)

    def _outbound(self, req: RouteRequest) -> bool:
        return geo.haversine(req.origin, HOME) < geo.haversine(req.origin, WORK)


def confirm_demo_corridors(db: Database, collector: Collector) -> None:
    collector.client.allow_detours = False  # the confirmed reference must be the normal route
    for d in direction_rows(db, enabled_only=False):
        for c in corridor_rows(db, d["id"], ("draft",)):
            res = collector.preview_corridor(c["id"])
            if res.get("ok"):
                db.execute("UPDATE corridors SET status='confirmed', reference_variant_id=?, confirmed_utc=?, "
                           "name=replace(name, ' (suggested)', ''), source='suggested', "
                           "note='Demo corridor (synthetic geometry).' WHERE id=?",
                           (res["variant_id"], iso(collector.clock()), c["id"]))
    collector.client.allow_detours = True


def generate_history(db: Database, cfg: Config, days: int = 35, end: datetime | None = None, seed: int = 7) -> dict:
    """Simulate `days` of scheduled collection ending at `end`, including an outage."""
    from .scheduler import Scheduler

    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    state = {"now": start}
    clock = lambda: state["now"]  # noqa: E731
    seed_demo(db)
    budget = Budget(db, cfg, clock)
    client = FakeTomTom(clock, cfg.tz, seed=seed)
    collector = Collector(db, cfg, budget, client, clock=clock, sleep=lambda s: True)
    confirm_demo_corridors(db, collector)
    sched = Scheduler(lambda: db, cfg, lambda _db: collector, clock=clock)
    outage_start = start + timedelta(days=days * 0.6, hours=3)
    outage_end = outage_start + timedelta(hours=7)
    step = timedelta(minutes=cfg.collection_interval_minutes)
    from .timeutil import next_slot
    t = next_slot(start, cfg.collection_interval_minutes)
    ran = 0
    while t < end:
        if not (outage_start <= t < outage_end):
            state["now"] = t + timedelta(seconds=3)
            if sched.step(db, collector, state["now"]).startswith("ran"):
                ran += 1
        t += step
    db.execute("DELETE FROM scheduler_lease")
    db.event("info", "demo", f"Generated {days} days of SYNTHETIC demo history ({ran} cycles, one 7-hour simulated outage).")
    return {"cycles": ran, "start": iso(start), "end": iso(end)}
