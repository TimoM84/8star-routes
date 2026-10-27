"""Read-side queries: latest state, cycle detail, statistics and planning.

Nothing in this module makes routing requests. Statistics use only
scheduled cycles. Missing and failed observations are counted explicitly
and never dropped silently.

Series
------
*Monitored* series are requested in every scheduled cycle:

- ``recommended``: the route TomTom returns first for the unrestricted request.
- ``corridor:<id>``: each confirmed (or retired, not hidden) corridor.

*Incidental* series only exist when "Show all collected routes" is on. They
re-use data that is stored anyway and never cost a request:

- ``alt:<family>``: an extra alternative TomTom returned with the unrestricted
  request, one series per route family (stable identity independent of the
  position in TomTom's answer). An alternative that is the same route family as
  a corridor observed in the same cycle, or as a corridor's confirmed
  geometry, is a duplicate of that monitored corridor and gets no series of
  its own, so the same road is never counted twice.
- ``dev:<corridor>:<family>``: corridor observations that deviated from the
  confirmed geometry, per deviating route family. They are a *breakdown* of
  the corridor's own series (a subset of its samples), not extra samples.

Incidental series never take part in "fastest" comparisons, comparable-cycle
counts or the planner's recommendation, and always report their coverage.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import geometry as geo
from . import timeutil
from .collector import corridor_rows, direction_rows, variant_geometry
from .config import Config
from .db import Database, iso, parse_iso, utcnow

RECOMMENDED_COLOR = "#2a78d6"
ALTERNATIVE_COLOR = "#868e96"
ALT_COLORS = ["#868e96", "#7048e8", "#c2255c", "#0c8599", "#5c940d", "#e67700", "#495057", "#ae3ec9"]
_same_route_cache: dict[tuple[int, int], bool] = {}


def percentile(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (same as numpy's default)."""
    if not values:
        return None
    v = sorted(values)
    if len(v) == 1:
        return float(v[0])
    k = (len(v) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return float(v[f] + (v[c] - v[f]) * (k - f))


def confidence(n: int) -> str:
    """Plain-language sample-size label used throughout the interface."""
    if n < 10:
        return "very low"
    if n < 30:
        return "low"
    if n < 100:
        return "moderate"
    return "good"


def _corridor_map(db: Database, direction_id: int) -> dict[int, dict]:
    return {c["id"]: c for c in corridor_rows(db, direction_id, ("draft", "confirmed", "retired"), include_hidden=True)}


class _Ctx:
    """Per-direction lookup data shared by the series functions."""

    def __init__(self, db: Database, direction_id: int):
        self.db = db
        self.direction_id = direction_id
        self.corridors = _corridor_map(db, direction_id)
        self.visible = corridor_rows(db, direction_id, ("confirmed", "retired"))
        self.monitored_keys = {"recommended"} | {f"corridor:{c['id']}" for c in self.visible}
        self.ref_family: dict[int, str] = {}
        for c in self.visible:
            if c["reference_variant_id"]:
                fam = db.scalar("SELECT family_id FROM route_variants WHERE id = ?", (c["reference_variant_id"],))
                if fam:
                    self.ref_family.setdefault(fam, f"corridor:{c['id']}")
        self._fam: dict[int, dict] = {}

    def family(self, fid: int | None) -> dict:
        if not fid:
            return {"id": None, "roads": [], "length_m": None, "representative_variant_id": None}
        if fid not in self._fam:
            r = self.db.one("SELECT id, roads_json, length_m, representative_variant_id FROM route_families WHERE id = ?", (fid,))
            self._fam[fid] = {"id": fid, "roads": json.loads(r["roads_json"]) if r else [], "length_m": r["length_m"] if r else None,
                              "representative_variant_id": r["representative_variant_id"] if r else None}
        return self._fam[fid]

    def family_text(self, fid: int | None) -> str:
        f = self.family(fid)
        roads = [x for x in f["roads"] if x][:3]
        return f"route {fid}" + (f" via {', '.join(roads)}" if roads else "")

    def corridor_name(self, key: str) -> str:
        c = self.corridors.get(int(key.split(":")[1]))
        return c["name"] if c else "Deleted corridor"


def route_key(obs) -> str | None:
    """Monitored series key of an observation (None for alternatives)."""
    if obs["role"] == "recommended":
        return "recommended"
    if obs["role"] == "corridor":
        return f"corridor:{obs['corridor_id']}"
    return None


def _fam(o) -> int | None:
    return o["family_id"] if "family_id" in o.keys() else None


def _series(rows, ctx: _Ctx, extras: bool = False) -> tuple[dict, dict, dict]:
    """Observations of one cycle by series key.

    Returns (monitored, incidental, annotations). `annotations` maps observation id to
    {"series_key", "duplicate_of"} for every observation, so the map can explain each line.
    """
    mon: dict = {}
    ext: dict = {}
    notes: dict = {}
    cor_fams: dict[int, str] = {}
    for o in rows:
        k = route_key(o)
        if k == "recommended" or (k and k in ctx.monitored_keys):
            mon[k] = o
            notes[o["id"]] = {"series_key": k, "duplicate_of": None}
            if k != "recommended" and o["status"] == "ok" and _fam(o):
                cor_fams.setdefault(_fam(o), k)
    for o in rows:
        fam = _fam(o)
        if o["role"] == "corridor" and o["deviation_flag"] and o["status"] == "ok" and fam:
            k = f"corridor:{o['corridor_id']}"
            if k in ctx.monitored_keys:
                dk = f"dev:{o['corridor_id']}:{fam}"
                notes[o["id"]] = {"series_key": k, "breakdown_key": dk, "duplicate_of": None}
                if extras:
                    ext[dk] = o
        elif o["role"] == "alternative" and o["status"] == "ok" and fam:
            dup = cor_fams.get(fam) or ctx.ref_family.get(fam)
            if dup:
                notes[o["id"]] = {"series_key": None, "duplicate_of": dup}
                continue
            ak = f"alt:{fam}"
            notes[o["id"]] = {"series_key": ak, "duplicate_of": None}
            if extras:
                cur = ext.get(ak)  # two alternatives of one family in one answer: keep the fastest
                if cur is None or (o["travel_time_s"] or 1e12) < (cur["travel_time_s"] or 1e12):
                    ext[ak] = o
    return mon, ext, notes


def alt_color(family_id: int | None) -> str:
    """Colour of an alternative route family; the same everywhere it is shown."""
    return ALT_COLORS[(family_id or 0) % len(ALT_COLORS)]


def extra_label(key: str, ctx: _Ctx, resembles: int | None = None) -> dict:
    parts = key.split(":")
    if parts[0] == "dev":
        cid, fam = int(parts[1]), int(parts[2])
        c = ctx.corridors.get(cid)
        return {"key": key, "name": f"{c['name'] if c else 'Deleted corridor'} · deviating variant ({ctx.family_text(fam)})",
                "color": c["color"] if c else ALTERNATIVE_COLOR, "kind": "deviation", "dashed": True, "monitored": False,
                "family_id": fam, "subset_of": f"corridor:{cid}"}
    fam = int(parts[1])
    rc = ctx.corridors.get(resembles) if resembles else None
    name = f"Alternative {ctx.family_text(fam)}"
    if rc:
        name += f", resembles {rc['name']}" + (" (hidden corridor)" if rc.get("hidden_utc") else "")
    return {"key": key, "name": name + " (not monitored)", "color": alt_color(fam), "kind": "alternative",
            "dashed": True, "monitored": False, "family_id": fam, "subset_of": None}


def _monitored_labels(ctx: _Ctx) -> list[dict]:
    out = [{"key": "recommended", "name": "TomTom recommendation", "color": RECOMMENDED_COLOR, "kind": "unrestricted", "monitored": True}]
    for c in ctx.visible:
        out.append({"key": f"corridor:{c['id']}", "name": c["name"], "color": c["color"], "kind": "corridor", "monitored": True,
                    "method": c["method"], "status": c["status"], "deviation_threshold_m": c["deviation_threshold_m"]})
    return out


def route_labels(db: Database, direction_id: int, extra_keys=(), ctx: _Ctx | None = None, resembles: dict | None = None,
                 counts: dict | None = None) -> list[dict]:
    ctx = ctx or _Ctx(db, direction_id)
    out = _monitored_labels(ctx)
    order = {l["key"]: i for i, l in enumerate(out)}
    counts = counts or {}
    devs = sorted((k for k in set(extra_keys) if k.startswith("dev:")), key=lambda k: (order.get("corridor:" + k.split(":")[1], 99), -counts.get(k, 0), k))
    alts = sorted((k for k in set(extra_keys) if k.startswith("alt:")), key=lambda k: (-counts.get(k, 0), k))
    for k in devs:
        out.append(extra_label(k, ctx))
    for k in alts:
        out.append(extra_label(k, ctx, (resembles or {}).get(k)))
    return out


def _extras_meta(by_cycle, ctx: _Ctx, extras: bool) -> tuple[set, dict, dict]:
    """Incidental keys present in the range, the corridor each alternative family most often resembles, and counts."""
    if not extras:
        return set(), {}, {}
    keys: set = set()
    res: dict = defaultdict(lambda: defaultdict(int))
    counts: dict = defaultdict(int)
    for rows in by_cycle.values():
        for k, o in _series(rows, ctx, True)[1].items():
            keys.add(k)
            counts[k] += 1
            if k.startswith("alt:") and o["resembles_corridor_id"]:
                res[k][o["resembles_corridor_id"]] += 1
    return keys, {k: max(v, key=v.get) for k, v in res.items()}, counts


def same_route(db: Database, v1: int | None, v2: int | None) -> bool:
    if v1 is None or v2 is None:
        return False
    if v1 == v2:
        return True
    k = (min(v1, v2), max(v1, v2))
    if k not in _same_route_cache:
        a, b = variant_geometry(db, v1), variant_geometry(db, v2)
        _same_route_cache[k] = bool(a and b and geo.similarity(a, b) >= 0.85)
    return _same_route_cache[k]


def _same_recommendation(db: Database, a, b) -> bool:
    """Did TomTom recommend the same road route in two cycles?

    Same route family, or both resembling the same corridor (small via-point variations), counts as the same.
    """
    fa, fb = _fam(a), _fam(b)
    if fa and fb and fa == fb:
        return True
    ra, rb = a["resembles_corridor_id"], b["resembles_corridor_id"]
    if ra or rb:
        return ra == rb
    if fa and fb:
        return False
    return same_route(db, a["variant_id"], b["variant_id"])


_OBS_SQL = "SELECT o.*, v.family_id family_id FROM observations o LEFT JOIN route_variants v ON v.id = o.variant_id "


def _obs_dict(o, ctx: _Ctx, note: dict | None) -> dict:
    d = {k: o[k] for k in o.keys() if k not in ("raw_response_z", "request_params_json", "traffic_sections_json")}
    d["traffic_sections"] = json.loads(o["traffic_sections_json"]) if o["traffic_sections_json"] else []
    d["request_params"] = json.loads(o["request_params_json"]) if o["request_params_json"] else None
    d["key"] = route_key(o)
    d["monitored"] = o["role"] in ("recommended", "corridor")
    d["excess_over_free_flow_s"] = (o["travel_time_s"] - o["free_flow_s"]) if o["travel_time_s"] is not None and o["free_flow_s"] is not None else None
    d["family"] = ctx.family_text(_fam(o)) if _fam(o) else None
    note = note or {}
    d["series_key"] = note.get("series_key") if o["role"] == "alternative" else d["key"]
    d["breakdown_key"] = note.get("breakdown_key")
    dup = note.get("duplicate_of")
    d["duplicate_of"] = {"key": dup, "name": ctx.corridor_name(dup)} if dup else None
    if o["role"] == "corridor":
        c = ctx.corridors.get(o["corridor_id"]) or {}
        d["name"], d["color"] = c.get("name", "Deleted corridor"), c.get("color", ALTERNATIVE_COLOR)
        d["corridor_method"] = c.get("method")
        d["deviation_threshold_m"] = c.get("deviation_threshold_m")
        d["reference_variant_id"] = c.get("reference_variant_id")
        d["corridor_hidden"] = bool(c.get("hidden_utc"))
    elif o["role"] == "recommended":
        d["name"], d["color"] = "TomTom recommendation", RECOMMENDED_COLOR
    else:
        d["name"], d["color"] = f"TomTom alternative ({d['family'] or 'unknown route'})", ALTERNATIVE_COLOR
        d["series_color"] = alt_color(_fam(o)) if d["series_key"] else ALTERNATIVE_COLOR
    rc = ctx.corridors.get(o["resembles_corridor_id"]) if o["resembles_corridor_id"] else None
    d["resembles"] = {"corridor_id": rc["id"], "name": rc["name"], "score": o["resembles_score"]} if rc else None
    return d


def cycle_detail(db: Database, cfg: Config, cycle_id: int, direction_id: int) -> dict | None:
    cyc = db.one("SELECT * FROM collection_cycles WHERE id = ?", (cycle_id,))
    if cyc is None:
        return None
    ctx = _Ctx(db, direction_id)
    obs = db.all(_OBS_SQL + "WHERE o.cycle_id = ? AND o.direction_id = ? ORDER BY o.role != 'recommended', o.role, o.corridor_id, o.returned_index",
                 (cycle_id, direction_id))
    _, _, notes = _series(obs, ctx, True)
    routes = [_obs_dict(o, ctx, notes.get(o["id"])) for o in obs]
    # Previous / next scheduled cycle (any status), for navigation.
    t = cyc["slot_utc"] or cyc["started_utc"]
    prev = db.one("SELECT id, slot_utc, status FROM collection_cycles WHERE trigger='schedule' AND slot_utc < ? ORDER BY slot_utc DESC LIMIT 1", (t,))
    nxt = db.one("SELECT id, slot_utc, status FROM collection_cycles WHERE trigger='schedule' AND slot_utc > ? ORDER BY slot_utc LIMIT 1", (t,))
    prev_ok = db.one(
        "SELECT c.id FROM collection_cycles c WHERE c.trigger='schedule' AND c.slot_utc < ? AND EXISTS "
        "(SELECT 1 FROM observations o WHERE o.cycle_id = c.id AND o.direction_id = ? AND o.status='ok') ORDER BY c.slot_utc DESC LIMIT 1",
        (t, direction_id))
    changes = {}
    if prev_ok:
        prev_obs = {route_key(o): o for o in db.all(_OBS_SQL + "WHERE o.cycle_id = ? AND o.direction_id = ? AND o.status='ok' AND o.role != 'alternative'",
                                                    (prev_ok["id"], direction_id))}
        for r in routes:
            p = prev_obs.get(r["key"])
            if r["key"] and p is not None and r["status"] == "ok":
                changes[r["key"]] = {
                    "geometry_changed": p["variant_id"] != r["variant_id"],
                    "route_family_changed": bool(_fam(p) and r["family_id"] and _fam(p) != r["family_id"]),
                    "previous_variant_id": p["variant_id"],
                    "route_changed": r["key"] == "recommended" and not _same_recommendation(db, p, r),
                }
    times = [parse_iso(r["response_utc"]) for r in routes if r["response_utc"]]
    spread = (max(times) - min(times)).total_seconds() if len(times) > 1 else 0
    expected = _expected_keys(ctx, cyc)
    present = {r["key"] for r in routes if r["key"]}
    return {
        "cycle": dict(cyc),
        "intended_utc": cyc["slot_utc"] or cyc["started_utc"],
        "routes": routes,
        "missing_keys": sorted(expected - present),
        "missing": [l for l in _monitored_labels(ctx) if l["key"] in expected - present],
        "changes": changes,
        "request_spread_s": spread,
        "comparable": spread <= cfg.comparison_tolerance_seconds,
        "comparison_tolerance_s": cfg.comparison_tolerance_seconds,
        "prev": dict(prev) if prev else None,
        "next": dict(nxt) if nxt else None,
    }


def _corridor_expected(c: dict, slot_iso: str) -> bool:
    """Was the corridor collected at this slot? Confirmed before it, and not yet retired."""
    if not c.get("confirmed_utc") or c["confirmed_utc"] > slot_iso:
        return False
    return not (c.get("retired_utc") and slot_iso >= c["retired_utc"])


def _expected_keys(ctx: _Ctx, cyc) -> set[str]:
    when = cyc["slot_utc"] or cyc["started_utc"] or ""
    return {"recommended"} | {f"corridor:{c['id']}" for c in ctx.visible if _corridor_expected(c, when)}


def latest(db: Database, cfg: Config, direction_id: int, now: datetime | None = None) -> dict:
    """Latest stored state for a direction. Never triggers routing requests."""
    now = now or utcnow()
    row = db.one(
        "SELECT c.id FROM collection_cycles c WHERE EXISTS (SELECT 1 FROM observations o WHERE o.cycle_id = c.id AND o.direction_id = ? "
        "AND o.status = 'ok') ORDER BY COALESCE(c.slot_utc, c.started_utc) DESC LIMIT 1", (direction_id,))
    last_sched = db.one("SELECT * FROM collection_cycles WHERE trigger='schedule' ORDER BY slot_utc DESC LIMIT 1")
    detail = cycle_detail(db, cfg, row["id"], direction_id) if row else None
    age_s = None
    if detail:
        t = max((parse_iso(r["response_utc"]) for r in detail["routes"] if r["response_utc"]), default=parse_iso(detail["intended_utc"]))
        age_s = (now - t).total_seconds()
    return {
        "direction_id": direction_id,
        "detail": detail,
        "age_s": age_s,
        "stale": age_s is None or age_s > cfg.stale_after_minutes * 60,
        "partial": bool(detail and (detail["missing_keys"] or any(r["status"] != "ok" for r in detail["routes"]))),
        "last_scheduled_cycle": dict(last_sched) if last_sched else None,
    }


def _scheduled_rows(db: Database, direction_id: int, start: datetime, end: datetime, extras: bool = False):
    cycles = db.all("SELECT id, slot_utc, status, reason FROM collection_cycles WHERE trigger='schedule' AND slot_utc >= ? AND slot_utc < ? "
                    "ORDER BY slot_utc", (iso(start), iso(end)))
    # Alternatives are always loaded: duplicates of monitored corridors are recognised per cycle.
    obs = db.all(
        "SELECT o.*, v.family_id family_id FROM observations o JOIN collection_cycles c ON c.id = o.cycle_id "
        "LEFT JOIN route_variants v ON v.id = o.variant_id WHERE c.trigger='schedule' AND o.direction_id = ? "
        "AND c.slot_utc >= ? AND c.slot_utc < ?" + ("" if extras else " AND o.role IN ('recommended', 'corridor')"),
        (direction_id, iso(start), iso(end)))
    by_cycle: dict[int, list] = defaultdict(list)
    for o in obs:
        by_cycle[o["cycle_id"]].append(o)
    return cycles, by_cycle


def _route_values(o) -> dict:
    return {"status": o["status"], "travel_time_s": o["travel_time_s"], "traffic_delay_s": o["traffic_delay_s"],
            "free_flow_s": o["free_flow_s"], "distance_m": o["distance_m"], "failure_reason": o["failure_reason"],
            "deviation_flag": o["deviation_flag"], "variant_id": o["variant_id"], "family_id": _fam(o)}


def day_series(db: Database, cfg: Config, direction_id: int, day: date, extras: bool = False) -> dict:
    """Every expected slot of a local day with each route's values (or why it is missing)."""
    tz = cfg.tz
    ctx = _Ctx(db, direction_id)
    start, end = timeutil.local_day_bounds(day, tz)
    cycles, by_cycle = _scheduled_rows(db, direction_id, start, end, extras)
    by_slot = {c["slot_utc"]: c for c in cycles}
    slots = []
    prev_rec = None
    now = utcnow()
    for s in timeutil.slots_between(start, end, cfg.collection_interval_minutes):
        k = iso(s)
        c = by_slot.get(k)
        entry = {"slot_utc": k, "local": s.astimezone(tz).isoformat(timespec="minutes"), "cycle_id": c["id"] if c else None,
                 "cycle_status": c["status"] if c else ("future" if s > now else "no_record"),
                 "cycle_reason": c["reason"] if c else None, "routes": {}, "recommendation": None, "recommendation_changed": False}
        if c:
            mon, ext, _ = _series(by_cycle.get(c["id"], []), ctx, extras)
            for rk, o in {**mon, **ext}.items():
                entry["routes"][rk] = _route_values(o)
            o = mon.get("recommended")
            if o is not None and o["status"] == "ok":
                entry["recommendation"] = {"resembles_corridor_id": o["resembles_corridor_id"], "variant_id": o["variant_id"], "family_id": _fam(o)}
                if prev_rec is not None and not _same_recommendation(db, prev_rec, o):
                    entry["recommendation_changed"] = True
                prev_rec = o
        slots.append(entry)
    keys, res, counts = _extras_meta(by_cycle, ctx, extras)
    return {"date": day.isoformat(), "timezone": cfg.timezone, "slots": slots,
            "routes": route_labels(db, direction_id, keys, ctx, res, counts), "slot_count": len(slots)}


def _filters_match(dt_utc: datetime, tz, weekdays: set[int] | None, t_from: int | None, t_to: int | None) -> bool:
    loc = dt_utc.astimezone(tz)
    if weekdays is not None and loc.weekday() not in weekdays:
        return False
    m = loc.hour * 60 + loc.minute
    if t_from is not None and m < t_from:
        return False
    if t_to is not None and m >= t_to:
        return False
    return True


def _summ(values: list[int]) -> dict:
    if not values:
        return {"median": None, "mean": None, "p90": None, "min": None, "max": None}
    return {"median": percentile(values, 50), "mean": statistics.fmean(values), "p90": percentile(values, 90),
            "min": min(values), "max": max(values)}


def stats(db: Database, cfg: Config, direction_id: int, start_day: date, end_day: date,
          weekdays: set[int] | None = None, t_from: int | None = None, t_to: int | None = None, extras: bool = False) -> dict:
    tz = cfg.tz
    ctx = _Ctx(db, direction_id)
    start, _ = timeutil.local_day_bounds(start_day, tz)
    _, end = timeutil.local_day_bounds(end_day, tz)
    end = min(end, utcnow())
    cycles, by_cycle = _scheduled_rows(db, direction_id, start, end, extras)
    recorded = {c["slot_utc"]: c for c in cycles}
    first = db.scalar("SELECT MIN(slot_utc) FROM collection_cycles WHERE trigger='schedule'")
    slots = [s for s in timeutil.slots_between(max(start, parse_iso(first)) if first else end, end, cfg.collection_interval_minutes)
             if _filters_match(s, tz, weekdays, t_from, t_to)]
    # Only slots in the filter take part, so incidental labels come from those cycles as well.
    in_filter = {recorded[iso(s)]["id"]: by_cycle.get(recorded[iso(s)]["id"], []) for s in slots if iso(s) in recorded}
    keys, res, counts = _extras_meta(in_filter, ctx, extras)
    labels = route_labels(db, direction_id, keys, ctx, res, counts)
    corr = {f"corridor:{c['id']}": c for c in ctx.visible}

    def new_acc():
        return {"expected": 0, "ok": 0, "failed": 0, "not_collected": 0, "travel": [], "delay": [], "excess": [],
                "wins": 0, "no_data_while_compared": 0, "by_weekday": defaultdict(list)}
    acc = {l["key"]: new_acc() for l in labels}
    cycle_status = defaultdict(int)
    comparable = excluded_spread = corridor_compared = rec_compared = undetermined = 0
    rec_changes = rec_n = 0
    prev_rec = None
    dates_with_data: set[str] = set()
    for s in slots:
        k = iso(s)
        wd = s.astimezone(tz).weekday()
        c = recorded.get(k)
        cycle_status[c["status"] if c else "no_record"] += 1
        mon, ext, _ = _series(by_cycle.get(c["id"], []), ctx, extras) if c else ({}, {}, {})
        obs = {**mon, **ext}
        expected_now = set()
        for key, a in acc.items():
            if key.startswith("corridor:") and not _corridor_expected(corr[key], k) and key not in obs:
                continue  # corridor not collected at this time (before confirmation or after retirement)
            a["expected"] += 1
            if not key.startswith(("alt:", "dev:")):
                expected_now.add(key)
            o = obs.get(key)
            if o is None:
                a["not_collected"] += 1
            elif o["status"] != "ok":
                a["failed"] += 1
            else:
                a["ok"] += 1
                dates_with_data.add(s.astimezone(tz).date().isoformat())
                if o["travel_time_s"] is not None:
                    a["travel"].append(o["travel_time_s"])
                    a["by_weekday"][wd].append(o["travel_time_s"])
                if o["traffic_delay_s"] is not None:
                    a["delay"].append(o["traffic_delay_s"])
                if o["travel_time_s"] is not None and o["free_flow_s"] is not None:
                    a["excess"].append(o["travel_time_s"] - o["free_flow_s"])
        # Comparisons use monitored series only, so switching incidental routes on never changes them.
        ok = {kk: o for kk, o in mon.items() if o["status"] == "ok" and o["travel_time_s"] is not None}
        if len(ok) >= 2:
            ts = [parse_iso(o["response_utc"]) for o in ok.values() if o["response_utc"]]
            if ts and (max(ts) - min(ts)).total_seconds() > cfg.comparison_tolerance_seconds:
                excluded_spread += 1
            else:
                comparable += 1
                # Corridors compete with each other. The recommendation is TomTom's pick and is usually
                # one of those roads, so it is scored separately: did it beat every corridor by > 60 s?
                cor = {kk: o for kk, o in ok.items() if kk.startswith("corridor:")}
                expected_cor = {kk for kk in expected_now if kk.startswith("corridor:")}
                if len(expected_cor) >= 2 and cor:
                    corridor_compared += 1
                    if set(cor) >= expected_cor:
                        best = min(o["travel_time_s"] for o in cor.values())
                        for kk, o in cor.items():
                            if o["travel_time_s"] == best:
                                acc[kk]["wins"] += 1
                    else:
                        # Undetermined: a missing corridor might have been fastest. Nobody gets the win.
                        undetermined += 1
                        for kk in expected_cor - set(cor):
                            acc[kk]["no_data_while_compared"] += 1
                rec_o = ok.get("recommended")
                if rec_o is not None and cor:
                    rec_compared += 1
                    if rec_o["travel_time_s"] < min(o["travel_time_s"] for o in cor.values()) - 60:
                        acc["recommended"]["wins"] += 1
        r = mon.get("recommended")
        if r is not None and r["status"] == "ok":
            if prev_rec is not None:
                rec_n += 1
                if not _same_recommendation(db, prev_rec, r):
                    rec_changes += 1
            prev_rec = r
    routes = []
    for l in labels:
        a = acc[l["key"]]
        base = {**l, "expected": a["expected"], "ok": a["ok"], "failed": a["failed"], "not_collected": a["not_collected"],
                "coverage": a["ok"] / a["expected"] if a["expected"] else None, "confidence": confidence(a["ok"]),
                "travel_time_s": _summ(a["travel"]), "traffic_delay_s": _summ(a["delay"]),
                "excess_over_free_flow_s": _summ(a["excess"]),
                "by_weekday": {str(w): {"n": len(v), "median": percentile(v, 50), "p90": percentile(v, 90)} for w, v in sorted(a["by_weekday"].items())}}
        if not l.get("monitored"):
            # Incidental: only present in some cycles, never competes for "fastest".
            subset = acc.get(l.get("subset_of") or "")
            routes.append({**base, "missing_rate": None, "fastest_count": None, "fastest_basis": None, "fastest_share": None,
                           "no_data_in_compared_cycles": None,
                           "share_of_parent": (a["ok"] / subset["ok"]) if subset and subset["ok"] else None})
            continue
        basis = rec_compared if l["key"] == "recommended" else corridor_compared
        routes.append({**base, "missing_rate": 1 - a["ok"] / a["expected"] if a["expected"] else None,
                       "fastest_count": a["wins"], "fastest_basis": basis,
                       "fastest_share": a["wins"] / basis if basis else None,
                       "no_data_in_compared_cycles": a["no_data_while_compared"]})
    first_obs = min((s for s in slots if iso(s) in recorded), default=None)
    return {
        "direction_id": direction_id, "from": start_day.isoformat(), "to": end_day.isoformat(),
        "filters": {"weekdays": sorted(weekdays) if weekdays is not None else None, "time_from": t_from, "time_to": t_to},
        "expected_slots": len(slots), "cycle_status": dict(cycle_status),
        "first_slot_utc": iso(first_obs) if first_obs else None, "dates_with_data": len(dates_with_data),
        "comparable_cycles": comparable, "excluded_for_timing_spread": excluded_spread,
        "corridor_comparisons": corridor_compared, "undetermined_comparisons": undetermined,
        "comparison_tolerance_s": cfg.comparison_tolerance_seconds,
        "recommendation_changes": rec_changes, "recommendation_transitions": rec_n,
        "routes": routes,
        "method": "Scheduled cycles only. Percentiles use linear interpolation. Coverage = successful observations / expected slots; "
                  "failed and missing slots count against coverage and are never treated as zero. 'Fastest' compares monitored routes "
                  "from the same cycle only, when their responses arrived within the comparison tolerance. For corridors it is 'fastest of all expected "
                  "corridors'; when a corridor has no data the cycle is undetermined and nobody wins it; for the recommendation it is 'faster than every "
                  "corridor by more than a minute' (TomTom found a better road). 'Traffic delay' is TomTom's trafficDelayInSeconds field; "
                  "'above free flow' is the estimated travel time minus TomTom's no-traffic time. A corridor is expected from its confirmation until "
                  "it is retired."
                  + (" Not-monitored rows reuse data that was stored anyway. Alternatives are the extra routes TomTom returned with the unrestricted "
                     "request, one row per distinct route (route family); an alternative that is the same route as a monitored corridor is not "
                     "counted again. Coverage shows how often TomTom offered it; a slot without it is 'not returned', not a failure. Deviating "
                     "variants break a corridor's own observations down by the different route TomTom took; they are part of the corridor's row, "
                     "not extra samples. Not-monitored rows never count for 'fastest' or the planner's recommendation." if extras else ""),
    }


def heatmap(db: Database, cfg: Config, direction_id: int, key: str, start_day: date, end_day: date,
            bucket_minutes: int = 30, metric: str = "travel_time_s") -> dict:
    tz = cfg.tz
    ctx = _Ctx(db, direction_id)
    start, _ = timeutil.local_day_bounds(start_day, tz)
    _, end = timeutil.local_day_bounds(end_day, tz)
    extras = key.startswith(("alt:", "dev:"))
    cycles, by_cycle = _scheduled_rows(db, direction_id, start, end, extras)
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    expected: dict[tuple[int, int], int] = defaultdict(int)
    for c in cycles:
        s = parse_iso(c["slot_utc"]).astimezone(tz)
        cell = (s.weekday(), (s.hour * 60 + s.minute) // bucket_minutes)
        expected[cell] += 1
        mon, ext, _ = _series(by_cycle.get(c["id"], []), ctx, extras)
        o = ext.get(key) if extras else mon.get(key)
        if o is not None and o["status"] == "ok" and o[metric] is not None:
            cells[cell].append(o[metric])
    out = []
    for (wd, b), n in sorted(expected.items()):
        v = cells.get((wd, b), [])
        out.append({"weekday": wd, "bucket": b, "minute": b * bucket_minutes, "n": len(v), "expected": n,
                    "median": percentile(v, 50), "p90": percentile(v, 90)})
    return {"key": key, "metric": metric, "bucket_minutes": bucket_minutes, "cells": out}


def _slot_row(dep_utc: datetime, m: int, v: list[int], failed: int, expected: int, pct: int, min_samples: int,
              target_utc: datetime, tz) -> dict:
    row = {"departure_local": dep_utc.astimezone(tz).isoformat(timespec="minutes"), "minute": m, "n": len(v),
           "failed": failed, "expected": expected, "coverage": len(v) / expected if expected else None}
    if v:
        on_time = sum(1 for x in v if dep_utc + timedelta(seconds=x) <= target_utc)
        row.update(on_time=on_time, on_time_share=on_time / len(v))
    if len(v) >= min_samples:
        q = percentile(v, pct)
        med = percentile(v, 50)
        row.update(median_s=med, quantile_s=q, buffer_s=q - med, spread_s=percentile(v, 90) - percentile(v, 10),
                   median_arrival_local=(dep_utc + timedelta(seconds=med)).astimezone(tz).isoformat(timespec="minutes"),
                   quantile_arrival_local=(dep_utc + timedelta(seconds=q)).astimezone(tz).isoformat(timespec="minutes"),
                   meets_target=dep_utc + timedelta(seconds=q) <= target_utc,
                   margin_s=(target_utc - dep_utc - timedelta(seconds=q)).total_seconds(),
                   confidence=confidence(len(v)))
    return row


def plan_arrival(db: Database, cfg: Config, direction_id: int, day: date, arrive_minute: int, pct: int = 90,
                 weeks: int = 12, min_samples: int = 6, window_minutes: int = 180, same_weekday: bool = True,
                 extras: bool = False) -> dict:
    """Latest departure per route whose `pct`-percentile historical arrival meets the target.

    Only monitored routes can be recommended. With `extras`, alternatives TomTom returned now and then are
    listed separately with their (usually sparse) sample counts; deviating corridor variants are not routes
    one can choose and are left out.
    """
    tz = cfg.tz
    ctx = _Ctx(db, direction_id)
    today = utcnow().astimezone(tz).date()
    notes = []
    if day < today:
        notes.append("The selected date is in the past; this is a historical what-if, not a forecast.")
    if (day - today).days > 60:
        notes.append("More than 60 days ahead: patterns may change (roadworks, holidays, seasons). Treat with extra caution.")
    target_local = timeutil.local_from_parts(day, arrive_minute, tz)
    if target_local is None:
        return {"state": "invalid", "message": "That arrival time does not exist on this date (daylight-saving change)."}
    target_utc = target_local.astimezone(timezone.utc)
    hist_end = min(utcnow(), datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc))
    hist_start = hist_end - timedelta(weeks=weeks)
    cycles, by_cycle = _scheduled_rows(db, direction_id, hist_start, hist_end, extras)
    weekday = day.weekday()
    samples: dict[tuple[str, int], list[int]] = defaultdict(list)
    failures: dict[tuple[str, int], int] = defaultdict(int)
    expected: dict[int, int] = defaultdict(int)
    dates_used: set[str] = set()
    used_cycles: dict = {}
    first_used = last_used = None
    for c in cycles:
        s = parse_iso(c["slot_utc"]).astimezone(tz)
        if same_weekday and s.weekday() != weekday:
            continue
        if not same_weekday and (s.weekday() < 5) != (weekday < 5):
            continue
        m = s.hour * 60 + s.minute
        expected[m] += 1
        used_cycles[c["id"]] = by_cycle.get(c["id"], [])
        mon, ext, _ = _series(by_cycle.get(c["id"], []), ctx, extras)
        for k, o in {**mon, **{k: v for k, v in ext.items() if k.startswith("alt:")}}.items():
            if o["status"] == "ok" and o["travel_time_s"] is not None:
                samples[(k, m)].append(o["travel_time_s"])
                dates_used.add(s.date().isoformat())
                first_used = min(first_used or c["slot_utc"], c["slot_utc"])
                last_used = max(last_used or c["slot_utc"], c["slot_utc"])
            else:
                failures[(k, m)] += 1
    keys, res, counts = _extras_meta(used_cycles, ctx, extras)
    labels = [l for l in route_labels(db, direction_id, {k for k in keys if k.startswith("alt:")}, ctx, res, counts)]
    step = cfg.collection_interval_minutes
    candidates = []
    for m in range(arrive_minute - window_minutes, arrive_minute + 1, step):
        if m < 0:
            continue
        dep_local = timeutil.local_from_parts(day, m, tz)
        if dep_local is None:
            continue
        candidates.append((m, dep_local.astimezone(timezone.utc)))
    per_route = []
    for l in labels:
        rows = [_slot_row(dep_utc, m, samples.get((l["key"], m), []), failures.get((l["key"], m), 0), expected.get(m, 0),
                          pct, min_samples, target_utc, tz) for m, dep_utc in candidates]
        feasible = [r for r in rows if r.get("meets_target")]
        best = max(feasible, key=lambda r: r["minute"]) if feasible else None
        per_route.append({**l, "slots": rows, "latest_departure": best, "eligible": bool(l.get("monitored")),
                          "sufficient": any(r["n"] >= min_samples for r in rows)})
    eligible = [r for r in per_route if r["eligible"]]
    viable = [r for r in eligible if r["latest_departure"]]
    comparison = None
    if not any(r["sufficient"] for r in eligible):
        state, rec = "insufficient_data", None
    elif not viable:
        state, rec = "no_feasible_departure", None
    else:
        state = "ok"
        best = max(viable, key=lambda r: (r["latest_departure"]["minute"], -r["latest_departure"]["median_s"]))
        ld = best["latest_departure"]
        by_min = {s["minute"]: s for s in best["slots"]}
        earlier = []
        for d in (15, 30):
            e = by_min.get(ld["minute"] - d)
            if e:
                earlier.append({"minutes_earlier": d, "departure_local": e["departure_local"], "n": e["n"],
                                "on_time_share": e.get("on_time_share"), "on_time": e.get("on_time"),
                                "median_arrival_local": e.get("median_arrival_local"), "quantile_arrival_local": e.get("quantile_arrival_local"),
                                "margin_s": e.get("margin_s")})
        rec = {"key": best["key"], "name": best["name"], "kind": best["kind"], **ld, "earlier": earlier}
        if ld["n"] < 10:
            notes.append(f"Only {ld['n']} observations at this departure time: the {pct}th percentile is close to the worst value seen. "
                         "Treat the advice as a rough guide until more weeks are collected.")
        # Compare monitored routes at the recommended departure time.
        at = []
        for r in eligible:
            s = next((x for x in r["slots"] if x["minute"] == ld["minute"]), None)
            if s and "median_s" in s:
                at.append({"key": r["key"], "name": r["name"], "median_s": s["median_s"], "quantile_s": s["quantile_s"],
                           "spread_s": s["spread_s"], "on_time_share": s.get("on_time_share"), "n": s["n"]})
        if at:
            comparison = {"departure_local": ld["departure_local"], "routes": at,
                          "fastest": min(at, key=lambda x: x["median_s"])["key"],
                          "most_reliable": min(at, key=lambda x: (x["spread_s"], x["quantile_s"]))["key"]}
    incidental = [r for r in per_route if not r["eligible"]]
    if incidental:
        notes.append("Alternatives TomTom returned only now and then are listed for reference with their own sample counts. "
                     "They are not monitored every cycle and are never recommended.")
    return {
        "state": state, "recommendation": rec, "routes": per_route, "notes": notes, "comparison": comparison,
        "target_arrival_local": target_local.isoformat(timespec="minutes"),
        "percentile": pct, "min_samples": min_samples,
        "history": {"from": iso(hist_start), "to": iso(hist_end), "weeks": weeks, "distinct_dates": len(dates_used),
                    "first_observation_utc": first_used, "last_observation_utc": last_used,
                    "matching": "same weekday" if same_weekday else ("weekdays" if weekday < 5 else "weekend days")},
        "method": (f"For each departure slot in the {window_minutes} minutes before the target, we take TomTom's estimated travel "
                   f"times observed at that same local time on {'the same weekday' if same_weekday else 'comparable days'} over the last "
                   f"{weeks} weeks. A slot qualifies when its {pct}th-percentile estimate still arrives on time and it has at least "
                   f"{min_samples} observations. 'On time' is the share of those observed days on which that departure would have arrived "
                   f"by the target. Buffer = {pct}th percentile minus median travel time. 'Most reliable' = smallest spread between the "
                   "10th and 90th percentile. These are TomTom estimates, not measured journeys; holidays, incidents and roadworks are "
                   "not modelled, and missing or failed slots are not counted as samples."),
    }


def overview(db: Database, cfg: Config) -> list[dict]:
    out = []
    for d in direction_rows(db, enabled_only=False):
        out.append({"id": d["id"], "code": d["code"], "name": d["name"], "enabled": bool(d["enabled"]),
                    "journey_id": d["journey_id"], "journey_name": d["journey_name"],
                    "from": d["from"], "to": d["to"], "from_label": d["from_label"], "to_label": d["to_label"]})
    return out


def variant_geojson(db: Database, variant_id: int) -> dict | None:
    row = db.one("SELECT * FROM route_variants WHERE id = ?", (variant_id,))
    if not row:
        return None
    coords = geo.decompress(row["geometry_z"])
    return {"id": row["id"], "coords": coords, "length_m": row["length_m"], "roads": json.loads(row["roads_json"]),
            "family_id": row["family_id"], "first_seen_utc": row["first_seen_utc"], "last_seen_utc": row["last_seen_utc"],
            "seen_count": row["seen_count"]}


def families(db: Database, direction_id: int) -> list[dict]:
    """Distinct routes (families) seen for a direction, with where they were observed. No requests."""
    ctx = _Ctx(db, direction_id)
    rows = db.all(
        "SELECT f.id, f.representative_variant_id, f.length_m, f.roads_json, COUNT(o.id) n, "
        "SUM(o.role = 'recommended') n_recommended, SUM(o.role = 'alternative') n_alternative, SUM(o.role = 'corridor') n_corridor, "
        "SUM(o.deviation_flag) n_deviating, MIN(o.intended_utc) first_utc, MAX(o.intended_utc) last_utc, "
        "(SELECT COUNT(*) FROM route_variants v2 WHERE v2.family_id = f.id) variants "
        "FROM route_families f LEFT JOIN route_variants v ON v.family_id = f.id LEFT JOIN observations o ON o.variant_id = v.id "
        "WHERE f.direction_id = ? GROUP BY f.id ORDER BY n DESC, f.id", (direction_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["roads"] = json.loads(d.pop("roads_json") or "[]")
        dup = ctx.ref_family.get(r["id"])
        d["corridor"] = {"key": dup, "name": ctx.corridor_name(dup)} if dup else None
        out.append(d)
    return out


def corridor_deviations(db: Database, corridor_id: int) -> list[dict]:
    """Deviating route variants of one corridor, grouped by route family, for inspection."""
    rows = db.all(
        "SELECT v.family_id, COUNT(*) n, MIN(o.intended_utc) first_utc, MAX(o.intended_utc) last_utc, MAX(o.deviation_max_m) max_m, "
        "AVG(o.deviation_share) avg_share, MAX(o.variant_id) latest_variant_id "
        "FROM observations o JOIN route_variants v ON v.id = o.variant_id WHERE o.corridor_id = ? AND o.role = 'corridor' "
        "AND o.deviation_flag = 1 GROUP BY v.family_id ORDER BY n DESC", (corridor_id,))
    total = db.scalar("SELECT COUNT(*) FROM observations WHERE corridor_id = ? AND role = 'corridor' AND status = 'ok'", (corridor_id,)) or 0
    out = []
    for r in rows:
        f = db.one("SELECT representative_variant_id, length_m, roads_json FROM route_families WHERE id = ?", (r["family_id"],))
        tt = [x[0] for x in db.all("SELECT o.travel_time_s FROM observations o JOIN route_variants v ON v.id = o.variant_id "
                                   "WHERE o.corridor_id = ? AND o.deviation_flag = 1 AND v.family_id IS ? AND o.travel_time_s IS NOT NULL",
                                   (corridor_id, r["family_id"]))]
        out.append({**dict(r), "share_of_observations": r["n"] / total if total else None,
                    "representative_variant_id": f["representative_variant_id"] if f else r["latest_variant_id"],
                    "length_m": f["length_m"] if f else None, "roads": json.loads(f["roads_json"]) if f else [],
                    "median_travel_time_s": percentile(tt, 50)})
    return out
