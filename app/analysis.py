"""Read-side queries: latest state, cycle detail, statistics and planning.

Nothing in this module makes routing requests. Statistics use only
scheduled cycles. Missing and failed observations are counted explicitly
and never dropped silently.
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


def _corridor_map(db: Database, direction_id: int) -> dict[int, dict]:
    return {c["id"]: c for c in corridor_rows(db, direction_id, ("draft", "confirmed", "retired"), include_hidden=True)}


def route_key(obs) -> str | None:
    if obs["role"] == "recommended":
        return "recommended"
    if obs["role"] == "corridor":
        return f"corridor:{obs['corridor_id']}"
    return None


def route_labels(db: Database, direction_id: int) -> list[dict]:
    out = [{"key": "recommended", "name": "TomTom recommendation", "color": RECOMMENDED_COLOR, "kind": "unrestricted"}]
    for c in corridor_rows(db, direction_id, ("confirmed", "retired")):
        out.append({"key": f"corridor:{c['id']}", "name": c["name"], "color": c["color"], "kind": "corridor",
                    "method": c["method"], "status": c["status"]})
    return out


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


def _obs_dict(o, corridors: dict[int, dict]) -> dict:
    d = {k: o[k] for k in o.keys() if k not in ("raw_response_z", "request_params_json", "traffic_sections_json")}
    d["traffic_sections"] = json.loads(o["traffic_sections_json"]) if o["traffic_sections_json"] else []
    d["request_params"] = json.loads(o["request_params_json"]) if o["request_params_json"] else None
    d["key"] = route_key(o)
    d["excess_over_free_flow_s"] = (o["travel_time_s"] - o["free_flow_s"]) if o["travel_time_s"] is not None and o["free_flow_s"] is not None else None
    if o["role"] == "corridor":
        c = corridors.get(o["corridor_id"]) or {}
        d["name"], d["color"] = c.get("name", "Deleted corridor"), c.get("color", "#868e96")
        d["corridor_method"] = c.get("method")
    elif o["role"] == "recommended":
        d["name"], d["color"] = "TomTom recommendation", RECOMMENDED_COLOR
    else:
        d["name"], d["color"] = f"TomTom alternative (returned #{o['returned_index']})", "#868e96"
    rc = corridors.get(o["resembles_corridor_id"]) if o["resembles_corridor_id"] else None
    d["resembles"] = {"corridor_id": rc["id"], "name": rc["name"], "score": o["resembles_score"]} if rc else None
    return d


def cycle_detail(db: Database, cfg: Config, cycle_id: int, direction_id: int) -> dict | None:
    cyc = db.one("SELECT * FROM collection_cycles WHERE id = ?", (cycle_id,))
    if cyc is None:
        return None
    corridors = _corridor_map(db, direction_id)
    obs = db.all("SELECT * FROM observations WHERE cycle_id = ? AND direction_id = ? ORDER BY role != 'recommended', role, corridor_id, returned_index",
                 (cycle_id, direction_id))
    routes = [_obs_dict(o, corridors) for o in obs]
    # Previous / next scheduled cycle for this direction (any status), for navigation.
    t = cyc["slot_utc"] or cyc["started_utc"]
    prev = db.one("SELECT id, slot_utc, status FROM collection_cycles WHERE trigger='schedule' AND slot_utc < ? ORDER BY slot_utc DESC LIMIT 1", (t,))
    nxt = db.one("SELECT id, slot_utc, status FROM collection_cycles WHERE trigger='schedule' AND slot_utc > ? ORDER BY slot_utc LIMIT 1", (t,))
    prev_ok = db.one(
        "SELECT c.id FROM collection_cycles c WHERE c.trigger='schedule' AND c.slot_utc < ? AND EXISTS "
        "(SELECT 1 FROM observations o WHERE o.cycle_id = c.id AND o.direction_id = ? AND o.status='ok') ORDER BY c.slot_utc DESC LIMIT 1",
        (t, direction_id))
    changes = {}
    if prev_ok:
        prev_obs = {route_key(o): o for o in db.all("SELECT * FROM observations WHERE cycle_id = ? AND direction_id = ? AND status='ok'",
                                                    (prev_ok["id"], direction_id))}
        for r in routes:
            p = prev_obs.get(r["key"])
            if r["key"] and p is not None and r["status"] == "ok":
                changes[r["key"]] = {
                    "geometry_changed": p["variant_id"] != r["variant_id"],
                    "previous_variant_id": p["variant_id"],
                    "route_changed": r["key"] == "recommended" and not _same_recommendation(db, p, r),
                }
    times = [parse_iso(r["response_utc"]) for r in routes if r["response_utc"]]
    spread = (max(times) - min(times)).total_seconds() if len(times) > 1 else 0
    expected = _expected_keys(db, direction_id, cyc)
    present = {r["key"] for r in routes if r["key"]}
    return {
        "cycle": dict(cyc),
        "intended_utc": cyc["slot_utc"] or cyc["started_utc"],
        "routes": routes,
        "missing_keys": sorted(expected - present),
        "missing": [l for l in route_labels(db, direction_id) if l["key"] in expected - present],
        "changes": changes,
        "request_spread_s": spread,
        "comparable": spread <= cfg.comparison_tolerance_seconds,
        "comparison_tolerance_s": cfg.comparison_tolerance_seconds,
        "prev": dict(prev) if prev else None,
        "next": dict(nxt) if nxt else None,
    }


def _same_recommendation(db: Database, a, b) -> bool:
    ra, rb = a["resembles_corridor_id"], b["resembles_corridor_id"]
    if ra or rb:
        return ra == rb
    return same_route(db, a["variant_id"], b["variant_id"])


def _expected_keys(db: Database, direction_id: int, cyc) -> set[str]:
    when = cyc["slot_utc"] or cyc["started_utc"]
    keys = {"recommended"}
    for c in corridor_rows(db, direction_id, ("confirmed", "retired")):
        if c["confirmed_utc"] and c["confirmed_utc"] <= (when or "") and c["status"] == "confirmed":
            keys.add(f"corridor:{c['id']}")
    return keys


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


def _scheduled_rows(db: Database, direction_id: int, start: datetime, end: datetime):
    cycles = db.all("SELECT id, slot_utc, status, reason FROM collection_cycles WHERE trigger='schedule' AND slot_utc >= ? AND slot_utc < ? "
                    "ORDER BY slot_utc", (iso(start), iso(end)))
    obs = db.all(
        "SELECT o.* FROM observations o JOIN collection_cycles c ON c.id = o.cycle_id WHERE c.trigger='schedule' AND o.direction_id = ? "
        "AND c.slot_utc >= ? AND c.slot_utc < ? AND o.role IN ('recommended', 'corridor')", (direction_id, iso(start), iso(end)))
    by_cycle: dict[int, list] = defaultdict(list)
    for o in obs:
        by_cycle[o["cycle_id"]].append(o)
    return cycles, by_cycle


def day_series(db: Database, cfg: Config, direction_id: int, day: date) -> dict:
    """Every expected slot of a local day with each route's values (or why it is missing)."""
    tz = cfg.tz
    start, end = timeutil.local_day_bounds(day, tz)
    cycles, by_cycle = _scheduled_rows(db, direction_id, start, end)
    by_slot = {c["slot_utc"]: c for c in cycles}
    slots = []
    prev_rec = None
    for s in timeutil.slots_between(start, end, cfg.collection_interval_minutes):
        k = iso(s)
        c = by_slot.get(k)
        entry = {"slot_utc": k, "local": s.astimezone(tz).isoformat(timespec="minutes"), "cycle_id": c["id"] if c else None,
                 "cycle_status": c["status"] if c else ("future" if s > utcnow() else "no_record"),
                 "cycle_reason": c["reason"] if c else None, "routes": {}, "recommendation": None, "recommendation_changed": False}
        if c:
            for o in by_cycle.get(c["id"], []):
                rk = route_key(o)
                entry["routes"][rk] = {"status": o["status"], "travel_time_s": o["travel_time_s"], "traffic_delay_s": o["traffic_delay_s"],
                                       "free_flow_s": o["free_flow_s"], "distance_m": o["distance_m"], "failure_reason": o["failure_reason"],
                                       "deviation_flag": o["deviation_flag"], "variant_id": o["variant_id"]}
                if rk == "recommended" and o["status"] == "ok":
                    entry["recommendation"] = {"resembles_corridor_id": o["resembles_corridor_id"], "variant_id": o["variant_id"]}
                    if prev_rec is not None and not _same_recommendation(db, prev_rec, o):
                        entry["recommendation_changed"] = True
                    prev_rec = o
        slots.append(entry)
    return {"date": day.isoformat(), "timezone": cfg.timezone, "slots": slots, "routes": route_labels(db, direction_id),
            "slot_count": len(slots)}


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
          weekdays: set[int] | None = None, t_from: int | None = None, t_to: int | None = None) -> dict:
    tz = cfg.tz
    start, _ = timeutil.local_day_bounds(start_day, tz)
    _, end = timeutil.local_day_bounds(end_day, tz)
    end = min(end, utcnow())
    cycles, by_cycle = _scheduled_rows(db, direction_id, start, end)
    labels = route_labels(db, direction_id)
    corr = {f"corridor:{c['id']}": c for c in corridor_rows(db, direction_id, ("confirmed", "retired"))}
    recorded = {c["slot_utc"]: c for c in cycles}
    first = db.scalar("SELECT MIN(slot_utc) FROM collection_cycles WHERE trigger='schedule'")
    slots = [s for s in timeutil.slots_between(max(start, parse_iso(first)) if first else end, end, cfg.collection_interval_minutes)
             if _filters_match(s, tz, weekdays, t_from, t_to)]
    acc = {l["key"]: {"expected": 0, "ok": 0, "failed": 0, "not_collected": 0, "travel": [], "delay": [], "excess": [],
                      "wins": 0, "no_data_while_compared": 0} for l in labels}
    cycle_status = defaultdict(int)
    comparable = excluded_spread = corridor_compared = rec_compared = undetermined = 0
    rec_changes = rec_n = 0
    prev_rec = None
    for s in slots:
        k = iso(s)
        c = recorded.get(k)
        cycle_status[c["status"] if c else "no_record"] += 1
        obs = {route_key(o): o for o in by_cycle.get(c["id"], [])} if c else {}
        expected_now = set()
        for key, a in acc.items():
            if key.startswith("corridor:"):
                cr = corr[key]
                if not cr["confirmed_utc"] or cr["confirmed_utc"] > k:
                    if key not in obs:
                        continue  # corridor did not exist yet
            a["expected"] += 1
            expected_now.add(key)
            o = obs.get(key)
            if o is None:
                a["not_collected"] += 1
            elif o["status"] != "ok":
                a["failed"] += 1
            else:
                a["ok"] += 1
                if o["travel_time_s"] is not None:
                    a["travel"].append(o["travel_time_s"])
                if o["traffic_delay_s"] is not None:
                    a["delay"].append(o["traffic_delay_s"])
                if o["travel_time_s"] is not None and o["free_flow_s"] is not None:
                    a["excess"].append(o["travel_time_s"] - o["free_flow_s"])
        ok = {kk: o for kk, o in obs.items() if o["status"] == "ok" and o["travel_time_s"] is not None}
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
        r = obs.get("recommended")
        if r is not None and r["status"] == "ok":
            if prev_rec is not None:
                rec_n += 1
                if not _same_recommendation(db, prev_rec, r):
                    rec_changes += 1
            prev_rec = r
    routes = []
    for l in labels:
        a = acc[l["key"]]
        routes.append({**l, "expected": a["expected"], "ok": a["ok"], "failed": a["failed"], "not_collected": a["not_collected"],
                       "coverage": a["ok"] / a["expected"] if a["expected"] else None,
                       "missing_rate": 1 - a["ok"] / a["expected"] if a["expected"] else None,
                       "travel_time_s": _summ(a["travel"]), "traffic_delay_s": _summ(a["delay"]),
                       "excess_over_free_flow_s": _summ(a["excess"]),
                       "fastest_count": a["wins"],
                       "fastest_basis": rec_compared if l["key"] == "recommended" else corridor_compared,
                       "fastest_share": (a["wins"] / (rec_compared if l["key"] == "recommended" else corridor_compared))
                       if (rec_compared if l["key"] == "recommended" else corridor_compared) else None,
                       "no_data_in_compared_cycles": a["no_data_while_compared"]})
    return {
        "direction_id": direction_id, "from": start_day.isoformat(), "to": end_day.isoformat(),
        "filters": {"weekdays": sorted(weekdays) if weekdays is not None else None, "time_from": t_from, "time_to": t_to},
        "expected_slots": len(slots), "cycle_status": dict(cycle_status),
        "comparable_cycles": comparable, "excluded_for_timing_spread": excluded_spread,
        "corridor_comparisons": corridor_compared, "undetermined_comparisons": undetermined,
        "comparison_tolerance_s": cfg.comparison_tolerance_seconds,
        "recommendation_changes": rec_changes, "recommendation_transitions": rec_n,
        "routes": routes,
        "method": "Scheduled cycles only. Percentiles use linear interpolation. Coverage = successful observations / expected slots; "
                  "failed and missing slots count against coverage and are never treated as zero. 'Fastest' compares routes "
                  "from the same cycle only, when their responses arrived within the comparison tolerance. For corridors it is 'fastest of all expected "
                  "corridors'; when a corridor has no data the cycle is undetermined and nobody wins it; for the recommendation it is 'faster than every corridor by more than a minute' (TomTom found a better road).",
    }


def heatmap(db: Database, cfg: Config, direction_id: int, key: str, start_day: date, end_day: date,
            bucket_minutes: int = 30, metric: str = "travel_time_s") -> dict:
    tz = cfg.tz
    start, _ = timeutil.local_day_bounds(start_day, tz)
    _, end = timeutil.local_day_bounds(end_day, tz)
    cycles, by_cycle = _scheduled_rows(db, direction_id, start, end)
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    expected: dict[tuple[int, int], int] = defaultdict(int)
    for c in cycles:
        s = parse_iso(c["slot_utc"]).astimezone(tz)
        cell = (s.weekday(), (s.hour * 60 + s.minute) // bucket_minutes)
        expected[cell] += 1
        for o in by_cycle.get(c["id"], []):
            if route_key(o) == key and o["status"] == "ok" and o[metric] is not None:
                cells[cell].append(o[metric])
    out = []
    for (wd, b), n in sorted(expected.items()):
        v = cells.get((wd, b), [])
        out.append({"weekday": wd, "bucket": b, "minute": b * bucket_minutes, "n": len(v), "expected": n,
                    "median": percentile(v, 50), "p90": percentile(v, 90)})
    return {"key": key, "metric": metric, "bucket_minutes": bucket_minutes, "cells": out}


def plan_arrival(db: Database, cfg: Config, direction_id: int, day: date, arrive_minute: int, pct: int = 90,
                 weeks: int = 12, min_samples: int = 4, window_minutes: int = 180, same_weekday: bool = True) -> dict:
    """Latest departure per route whose `pct`-percentile historical arrival meets the target."""
    tz = cfg.tz
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
    cycles, by_cycle = _scheduled_rows(db, direction_id, hist_start, hist_end)
    weekday = day.weekday()
    samples: dict[tuple[str, int], list[int]] = defaultdict(list)
    failures: dict[tuple[str, int], int] = defaultdict(int)
    dates_used: set[str] = set()
    for c in cycles:
        s = parse_iso(c["slot_utc"]).astimezone(tz)
        if same_weekday and s.weekday() != weekday:
            continue
        if not same_weekday and (s.weekday() < 5) != (weekday < 5):
            continue
        m = s.hour * 60 + s.minute
        for o in by_cycle.get(c["id"], []):
            k = route_key(o)
            if o["status"] == "ok" and o["travel_time_s"] is not None:
                samples[(k, m)].append(o["travel_time_s"])
                dates_used.add(s.date().isoformat())
            else:
                failures[(k, m)] += 1
    labels = route_labels(db, direction_id)
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
        rows = []
        for m, dep_utc in candidates:
            v = samples.get((l["key"], m), [])
            row = {"departure_local": dep_utc.astimezone(tz).isoformat(timespec="minutes"), "minute": m, "n": len(v),
                   "failed": failures.get((l["key"], m), 0)}
            if len(v) >= min_samples:
                q = percentile(v, pct)
                med = percentile(v, 50)
                row.update(median_s=med, quantile_s=q,
                           median_arrival_local=(dep_utc + timedelta(seconds=med)).astimezone(tz).isoformat(timespec="minutes"),
                           quantile_arrival_local=(dep_utc + timedelta(seconds=q)).astimezone(tz).isoformat(timespec="minutes"),
                           meets_target=dep_utc + timedelta(seconds=q) <= target_utc,
                           margin_s=(target_utc - dep_utc - timedelta(seconds=q)).total_seconds())
            rows.append(row)
        feasible = [r for r in rows if r.get("meets_target")]
        best = max(feasible, key=lambda r: r["minute"]) if feasible else None
        per_route.append({**l, "slots": rows, "latest_departure": best,
                          "sufficient": any(r["n"] >= min_samples for r in rows)})
    viable = [r for r in per_route if r["latest_departure"]]
    if not any(r["sufficient"] for r in per_route):
        state = "insufficient_data"
        rec = None
    elif not viable:
        state = "no_feasible_departure"
        rec = None
    else:
        state = "ok"
        best = max(viable, key=lambda r: (r["latest_departure"]["minute"], -r["latest_departure"]["median_s"]))
        rec = {"key": best["key"], "name": best["name"], **best["latest_departure"]}
    return {
        "state": state, "recommendation": rec, "routes": per_route, "notes": notes,
        "target_arrival_local": target_local.isoformat(timespec="minutes"),
        "percentile": pct, "min_samples": min_samples,
        "history": {"from": iso(hist_start), "to": iso(hist_end), "weeks": weeks, "distinct_dates": len(dates_used),
                    "matching": "same weekday" if same_weekday else ("weekdays" if weekday < 5 else "weekend days")},
        "method": (f"For each departure slot in the {window_minutes} minutes before the target, we take TomTom's estimated travel "
                   f"times observed at that same local time on {'the same weekday' if same_weekday else 'comparable days'} over the last "
                   f"{weeks} weeks. A slot qualifies when its {pct}th-percentile estimate still arrives on time. With few samples the "
                   f"{pct}th percentile is close to the worst observed value. These are TomTom estimates, not measured journeys; "
                   "holidays, incidents and roadworks are not modelled."),
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
            "first_seen_utc": row["first_seen_utc"], "last_seen_utc": row["last_seen_utc"], "seen_count": row["seen_count"]}
