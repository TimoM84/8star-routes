"""One collection cycle: plan the requests, call TomTom through the budget
guard, and store every outcome (successes, failures and requests that were
never sent) as observations."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from . import families
from . import geometry as geo
from .budget import Budget, BudgetBlocked
from .config import Config
from .db import Database, iso, utcnow
from .tomtom import ParsedRoute, RawResult, RouteRequest, parse_routes

log = logging.getLogger("routes.collector")
RESEMBLE_THRESHOLD = 0.85


class RoutingClient(Protocol):
    configured: bool

    def calculate_route(self, req: RouteRequest) -> RawResult: ...


@dataclass
class Target:
    direction_id: int
    request: RouteRequest
    corridor: dict | None  # corridor row as dict, or None for unrestricted


def direction_rows(db: Database, enabled_only: bool = True) -> list[dict]:
    rows = db.all(
        "SELECT d.id, d.code, d.enabled, j.id journey_id, j.name journey_name, j.origin_label, j.origin_lat, j.origin_lon, "
        "j.dest_label, j.dest_lat, j.dest_lon FROM directions d JOIN journeys j ON j.id = d.journey_id "
        + ("WHERE d.enabled = 1 " if enabled_only else "") + "ORDER BY j.id, d.code = 'return', d.id")
    out = []
    for r in rows:
        d = dict(r)
        if d["code"] == "outbound":
            d["from"], d["to"] = (d["origin_lat"], d["origin_lon"]), (d["dest_lat"], d["dest_lon"])
            d["from_label"], d["to_label"] = d["origin_label"], d["dest_label"]
        else:
            d["from"], d["to"] = (d["dest_lat"], d["dest_lon"]), (d["origin_lat"], d["origin_lon"])
            d["from_label"], d["to_label"] = d["dest_label"], d["origin_label"]
        d["name"] = f"{d['from_label']} → {d['to_label']}"
        out.append(d)
    return out


def corridor_rows(db: Database, direction_id: int, statuses=("confirmed",), include_hidden: bool = False) -> list[dict]:
    q = ",".join("?" * len(statuses))
    hidden = "" if include_hidden else " AND hidden_utc IS NULL"
    return [dict(r) for r in db.all(
        f"SELECT * FROM corridors WHERE direction_id = ? AND status IN ({q}){hidden} ORDER BY sort, id", (direction_id, *statuses))]


def build_request(direction: dict, corridor: dict | None, cfg: Config) -> RouteRequest:
    if corridor is None:
        return RouteRequest(direction["id"], "unrestricted", direction["from"], direction["to"],
                            max_alternatives=cfg.unrestricted_max_alternatives)
    if corridor["method"] == "reconstruct":
        sp = json.loads(corridor["supporting_points_json"] or "[]")
        return RouteRequest(direction["id"], "corridor", direction["from"], direction["to"], corridor_id=corridor["id"],
                            supporting_points=sp)
    return RouteRequest(direction["id"], "corridor", direction["from"], direction["to"], corridor_id=corridor["id"],
                        waypoints=json.loads(corridor["waypoints_json"] or "[]"))


def plan_targets(db: Database, cfg: Config, direction_ids: list[int] | None = None) -> list[Target]:
    targets = []
    for d in direction_rows(db):
        if direction_ids and d["id"] not in direction_ids:
            continue
        targets.append(Target(d["id"], build_request(d, None, cfg), None))
        for c in corridor_rows(db, d["id"]):
            targets.append(Target(d["id"], build_request(d, c, cfg), c))
    return targets


def requests_per_cycle(db: Database, cfg: Config) -> int:
    return len(plan_targets(db, cfg))


def variant_geometry(db: Database, variant_id: int | None) -> list[list[float]] | None:
    if not variant_id:
        return None
    blob = db.scalar("SELECT geometry_z FROM route_variants WHERE id = ?", (variant_id,))
    return geo.decompress(blob) if blob else None


def upsert_variant(db: Database, direction_id: int, route: ParsedRoute, seen: datetime) -> int | None:
    if len(route.coords) < 2:
        return None
    fp = geo.fingerprint(route.coords)
    db.execute(
        "INSERT INTO route_variants(direction_id, fingerprint, geometry_z, point_count, length_m, roads_json, first_seen_utc, last_seen_utc, seen_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(direction_id, fingerprint) DO NOTHING",
        (direction_id, fp, geo.compress(route.coords), len(route.coords), route.distance_m, json.dumps(route.roads), iso(seen), iso(seen)))
    db.execute("UPDATE route_variants SET last_seen_utc = MAX(last_seen_utc, ?), seen_count = seen_count + 1 WHERE direction_id = ? AND fingerprint = ?",
               (iso(seen), direction_id, fp))
    vid = db.scalar("SELECT id FROM route_variants WHERE direction_id = ? AND fingerprint = ?", (direction_id, fp))
    families.assign_family(db, vid)
    return vid


class Collector:
    def __init__(self, db: Database, cfg: Config, budget: Budget, client: RoutingClient,
                 clock: Callable[[], datetime] = utcnow, sleep: Callable[[float], bool] | None = None):
        self.db = db
        self.cfg = cfg
        self.budget = budget
        self.client = client
        self.clock = clock
        # sleep(seconds) -> False when interrupted by shutdown
        self.sleep = sleep or (lambda s: (time.sleep(s), True)[1])
        self._ref_cache: dict[int, tuple[int | None, list[list[float]] | None]] = {}
        # Variants repeat constantly, so geometry comparisons are memoised per (variant, corridor reference).
        self._sim_cache: dict[tuple, object] = {}

    # --- outbound call with guard and retry policy --------------------------------------
    def call(self, req: RouteRequest, purpose: str, cycle_id: int | None) -> tuple[RawResult | None, int | None, str | None]:
        """Returns (result, ledger_id, not_sent_reason)."""
        attempt = 0
        endpoint = f"{req.method()} routing/1/calculateRoute ({'unrestricted' if req.corridor_id is None else 'corridor ' + str(req.corridor_id)})"
        if not self.client.configured:
            return None, None, "not requested: TOMTOM_API_KEY is not set"
        while True:
            try:
                lid = self.budget.reserve(purpose if attempt == 0 else "retry", endpoint, cycle_id)
            except BudgetBlocked as b:
                return None, None, f"not requested: {b.reason}"
            res = self.client.calculate_route(req)
            outcome = "ok" if res.ok else ("timeout" if res.error_kind == "timeout" else
                                           "network_error" if res.error_kind == "network" else
                                           "invalid_response" if res.error_kind == "invalid_response" else "http_error")
            self.budget.complete(lid, outcome, res.http_status, res.tracking_id, res.request_utc, res.response_utc,
                                 None if res.ok else f"{res.error_kind}: {res.error_message}")
            if res.ok:
                return res, lid, None
            if res.error_kind == "auth":
                self.budget.note_auth_error()
                return res, lid, None
            if res.error_kind == "quota":
                self.budget.note_quota_error()
                return res, lid, None
            if not res.retriable or attempt >= self.cfg.max_retries or not self.budget.retry_allowed():
                if res.error_kind == "rate_limited" and res.retry_after_s:
                    self.budget.note_retry_after(res.retry_after_s)
                return res, lid, None
            if res.error_kind == "rate_limited":
                wait = res.retry_after_s if res.retry_after_s is not None else self.cfg.retry_delay_seconds * 4
                if wait > self.cfg.max_retry_after_seconds:
                    self.budget.note_retry_after(wait)
                    return res, lid, None
            else:
                wait = self.cfg.retry_delay_seconds
            if not self.sleep(wait):
                return res, lid, None
            attempt += 1

    # --- storage helpers ---------------------------------------------------------------
    def reset_caches(self) -> None:
        self._ref_cache.clear()
        self._sim_cache.clear()

    def _reference_with_id(self, corridor_id: int) -> tuple[int | None, list[list[float]] | None]:
        if corridor_id not in self._ref_cache:
            vid = self.db.scalar("SELECT reference_variant_id FROM corridors WHERE id = ?", (corridor_id,))
            self._ref_cache[corridor_id] = (vid, variant_geometry(self.db, vid))
        return self._ref_cache[corridor_id]

    def _insert_obs(self, **f) -> int:
        cols = ",".join(f)
        cur = self.db.execute(f"INSERT INTO observations({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
        return cur.lastrowid

    def _store(self, cycle_id: int, intended: datetime, t: Target, res: RawResult | None, lid: int | None, not_sent: str | None) -> bool:
        params = json.dumps(t.request.sanitized())
        base = dict(cycle_id=cycle_id, direction_id=t.direction_id, target=t.request.target, corridor_id=t.request.corridor_id,
                    ledger_id=lid, intended_utc=iso(intended), request_params_json=params)
        role_first = "recommended" if t.corridor is None else "corridor"
        if res is None or not res.ok:
            reason = not_sent or f"{res.error_kind}: {res.error_message}"
            self._insert_obs(**base, role=role_first, status="failed", failure_reason=reason,
                             http_status=None if res is None else res.http_status,
                             request_utc=None if res is None else iso(res.request_utc),
                             response_utc=None if res is None else iso(res.response_utc),
                             tracking_id=None if res is None else res.tracking_id)
            return False
        routes = parse_routes(res.data)
        if t.corridor is not None:
            routes = routes[:1]  # constrained request: the first route is the corridor route
        confirmed = corridor_rows(self.db, t.direction_id)
        raw = geo.compress(res.data) if self.cfg.store_raw_responses else None
        for r in routes:
            vid = upsert_variant(self.db, t.direction_id, r, res.response_utc or intended)
            fields = dict(base, role=role_first if r.index == 0 else "alternative", returned_index=r.index, variant_id=vid,
                          status="ok", http_status=res.http_status, request_utc=iso(res.request_utc), response_utc=iso(res.response_utc),
                          distance_m=r.distance_m, travel_time_s=r.travel_time_s, traffic_delay_s=r.traffic_delay_s,
                          free_flow_s=r.free_flow_s, historic_s=r.historic_s, live_s=r.live_s, traffic_length_m=r.traffic_length_m,
                          departure_time=r.departure_time, arrival_time=r.arrival_time,
                          traffic_sections_json=json.dumps(r.traffic_sections), tracking_id=res.tracking_id,
                          raw_response_z=raw if r.index == 0 else None)
            if t.corridor is not None:
                ref_id, ref = self._reference_with_id(t.corridor["id"])
                if ref:
                    key = ("dev", vid, ref_id, t.corridor["deviation_threshold_m"])
                    dev = self._sim_cache.get(key)
                    if dev is None:
                        dev = self._sim_cache[key] = geo.deviation(r.coords, ref, t.corridor["deviation_threshold_m"])
                    fields.update(deviation_flag=int(dev.flagged), deviation_share=round(dev.share_outside, 4),
                                  deviation_max_m=int(dev.max_m) if dev.max_m != float("inf") else None)
                    # One event per new deviating *route family*, not per observation or per slightly different geometry.
                    fam = self.db.scalar("SELECT family_id FROM route_variants WHERE id = ?", (vid,))
                    if dev.flagged and not self.db.scalar(
                            "SELECT 1 FROM observations o JOIN route_variants v ON v.id = o.variant_id "
                            "WHERE o.corridor_id = ? AND o.deviation_flag = 1 AND v.family_id = ? LIMIT 1", (t.corridor["id"], fam)):
                        self.db.event("warning", "corridor_deviation",
                                      f"Corridor '{t.corridor['name']}' took a different route ({families.family_label(self.db, fam)}): "
                                      f"{dev.share_outside:.0%} of it lies more than {t.corridor['deviation_threshold_m']} m from the "
                                      f"confirmed geometry, max {dev.max_m:.0f} m. Inspect it in Setup.")
            else:
                best, score = None, 0.0
                for c in confirmed:
                    ref_id, ref = self._reference_with_id(c["id"])
                    if not ref:
                        continue
                    key = ("sim", vid, ref_id)
                    s = self._sim_cache.get(key)
                    if s is None:
                        s = self._sim_cache[key] = geo.similarity(r.coords, ref)
                    if s > score:
                        best, score = c["id"], s
                fields.update(resembles_corridor_id=best if score >= RESEMBLE_THRESHOLD else None,
                              resembles_score=round(score, 4) if best else None)
            self._insert_obs(**fields)
        return True

    # --- cycles ------------------------------------------------------------------------
    def run_cycle(self, cycle_id: int, intended: datetime, direction_ids: list[int] | None = None,
                  purpose: str = "schedule", should_stop: Callable[[], bool] = lambda: False) -> str:
        targets = plan_targets(self.db, self.cfg, direction_ids)
        self._ref_cache.clear()  # corridor references may have changed since the last cycle
        if len(self._sim_cache) > 20000:
            self._sim_cache.clear()
        self.db.execute("UPDATE collection_cycles SET planned_requests = ?, started_utc = ? WHERE id = ?",
                        (len(targets), iso(self.clock()), cycle_id))
        ok_count = attempted = 0
        for t in targets:
            if should_stop():
                self.db.execute("UPDATE collection_cycles SET status='interrupted', reason='shutdown during cycle', finished_utc=?, "
                                "attempted_requests=? WHERE id=?", (iso(self.clock()), attempted, cycle_id))
                return "interrupted"
            res, lid, not_sent = self.call(t.request, purpose, cycle_id)
            if lid is not None:
                attempted += 1
            with self.db.tx():
                ok_count += self._store(cycle_id, intended, t, res, lid, not_sent)
        status = "complete" if ok_count == len(targets) and targets else ("partial" if ok_count else "failed")
        reason = None if status == "complete" else f"{len(targets) - ok_count} of {len(targets)} targets without data"
        self.db.execute("UPDATE collection_cycles SET status=?, reason=?, finished_utc=?, attempted_requests=? WHERE id=?",
                        (status, reason, iso(self.clock()), attempted, cycle_id))
        return status

    def preview_corridor(self, corridor_id: int, purpose: str = "preview") -> dict:
        """One budgeted request to show what a corridor resolves to. Does not create an observation."""
        c = dict(self.db.one("SELECT * FROM corridors WHERE id = ?", (corridor_id,)))
        d = next(x for x in direction_rows(self.db, enabled_only=False) if x["id"] == c["direction_id"])
        req = build_request(d, c, self.cfg)
        res, lid, not_sent = self.call(req, purpose, None)
        if res is None:
            return {"ok": False, "reason": not_sent}
        if not res.ok:
            return {"ok": False, "reason": f"{res.error_kind}: {res.error_message}", "http_status": res.http_status}
        r = parse_routes(res.data)[0]
        with self.db.tx():
            vid = upsert_variant(self.db, d["id"], r, res.response_utc or self.clock())
            self.db.execute("UPDATE corridors SET preview_variant_id = ?, preview_utc = ?, updated_utc = ? WHERE id = ?",
                            (vid, iso(res.response_utc), iso(self.clock()), corridor_id))
        return {"ok": True, "variant_id": vid, "distance_m": r.distance_m, "travel_time_s": r.travel_time_s,
                "traffic_delay_s": r.traffic_delay_s, "roads": r.roads, "geometry": r.coords}
