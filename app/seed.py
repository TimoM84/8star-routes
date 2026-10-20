"""Fictional demo fixtures only; live installations start without locations."""

from __future__ import annotations

import json

from .db import Database, iso, utcnow

DEMO_JOURNEY = {
    "name": "Demo origin ↔ Demo destination",
    "origin_label": "Demo origin",
    "origin_lat": 52.30,
    "origin_lon": 5.00,
    "dest_label": "Demo destination",
    "dest_lat": 52.34,
    "dest_lon": 5.33,
}

# Fictional routes used by demo mode and offline tests only.
DEMO_CORRIDORS = [
    {"name": "Demo south route (suggested)", "color": "#eb6834",
     "waypoints": [[52.31, 5.22]], "note": "Fictional demo corridor; not a real road route."},
    {"name": "Demo north route (suggested)", "color": "#1baf7a",
     "waypoints": [[52.37, 5.17]], "note": "Fictional demo corridor; not a real road route."},
]

CORRIDOR_COLORS = ["#eb6834", "#1baf7a", "#6250d6", "#e87ba4", "#008300", "#e34948"]


def seed_demo(db: Database) -> bool:
    if db.scalar("SELECT COUNT(*) FROM journeys"):
        return False
    now = iso(utcnow())
    j = DEMO_JOURNEY
    with db.tx():
        jid = db.execute(
            "INSERT INTO journeys(name, origin_label, origin_lat, origin_lon, dest_label, dest_lat, dest_lon, created_utc, updated_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (j["name"], j["origin_label"], j["origin_lat"], j["origin_lon"], j["dest_label"], j["dest_lat"], j["dest_lon"], now, now)).lastrowid
        for code in ("outbound", "return"):
            did = db.execute("INSERT INTO directions(journey_id, code) VALUES (?, ?)", (jid, code)).lastrowid
            for i, c in enumerate(DEMO_CORRIDORS):
                wps = c["waypoints"] if code == "outbound" else list(reversed(c["waypoints"]))
                db.execute(
                    "INSERT INTO corridors(direction_id, name, color, method, waypoints_json, status, source, note, sort, created_utc, updated_utc) "
                    "VALUES (?, ?, ?, 'waypoints', ?, 'draft', 'suggested', ?, ?, ?, ?)",
                    (did, c["name"], c["color"], json.dumps(wps), c["note"], i, now, now))
    db.event("info", "seed", "Created fictional demo journey and corridor drafts.")
    return True
