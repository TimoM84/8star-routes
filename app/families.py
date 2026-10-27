"""Route families: stable identities for route geometries.

A variant is one exact geometry (SHA-256 of the coordinates). TomTom can return
the same road route with slightly different coordinates (a different lane
split, a shifted ramp), which creates a new variant every time. A family
groups variants that follow the same roads: at least FAMILY_MIN_SHARE of each
line lies within FAMILY_DISTANCE_M of the other and their lengths differ by
less than FAMILY_LENGTH_TOLERANCE. Families never depend on the position in
TomTom's response, and assignment needs no routing requests.

The first variant of a family is its representative and is never replaced, so
a family's identity does not drift. Backfilling processes the most frequently
seen variants first, so representatives are the common geometries.
"""

from __future__ import annotations

import json

from . import geometry as geo
from .db import Database, iso, utcnow

FAMILY_DISTANCE_M = 150.0
FAMILY_MIN_SHARE = 0.97
FAMILY_LENGTH_TOLERANCE = 0.05

_rep_cache: dict[int, list[list[float]]] = {}


def same_family(a: list[list[float]], b: list[list[float]]) -> bool:
    la, lb = geo.length_m(a), geo.length_m(b)
    if max(la, lb) <= 0 or abs(la - lb) / max(la, lb) > FAMILY_LENGTH_TOLERANCE:
        return False
    return geo.similarity(a, b, FAMILY_DISTANCE_M) >= FAMILY_MIN_SHARE


def _rep_coords(db: Database, family_id: int, variant_id: int) -> list[list[float]]:
    c = _rep_cache.get(family_id)
    if c is None:
        blob = db.scalar("SELECT geometry_z FROM route_variants WHERE id = ?", (variant_id,))
        c = _rep_cache[family_id] = geo.decompress(blob) if blob else []
        if len(_rep_cache) > 2000:
            _rep_cache.clear()
    return c


def assign_family(db: Database, variant_id: int) -> int | None:
    """Family of a variant, creating one when no existing family matches. Call inside a transaction."""
    row = db.one("SELECT id, direction_id, family_id, geometry_z, length_m, roads_json FROM route_variants WHERE id = ?", (variant_id,))
    if row is None:
        return None
    if row["family_id"]:
        return row["family_id"]
    coords = geo.decompress(row["geometry_z"])
    match = None
    for f in db.all("SELECT id, representative_variant_id FROM route_families WHERE direction_id = ? ORDER BY id", (row["direction_id"],)):
        rep = _rep_coords(db, f["id"], f["representative_variant_id"])
        if len(rep) >= 2 and len(coords) >= 2 and same_family(coords, rep):
            match = f["id"]
            break
    if match is None:
        match = db.execute(
            "INSERT INTO route_families(direction_id, representative_variant_id, length_m, roads_json, created_utc) VALUES (?, ?, ?, ?, ?)",
            (row["direction_id"], variant_id, row["length_m"], row["roads_json"] or "[]", iso(utcnow()))).lastrowid
    db.execute("UPDATE route_variants SET family_id = ? WHERE id = ?", (match, variant_id))
    return match


def backfill(db: Database) -> int:
    """Assign families to all variants without one (idempotent, no requests)."""
    ids = [r[0] for r in db.all("SELECT id FROM route_variants WHERE family_id IS NULL ORDER BY seen_count DESC, id")]
    for i in range(0, len(ids), 200):
        with db.tx():
            for vid in ids[i:i + 200]:
                assign_family(db, vid)
    return len(ids)


def family_label(db: Database, family_id: int | None) -> str:
    if not family_id:
        return "unknown route"
    roads = db.scalar("SELECT roads_json FROM route_families WHERE id = ?", (family_id,))
    names = [r for r in json.loads(roads or "[]") if r][:3]
    return f"route {family_id}" + (f" via {', '.join(names)}" if names else "")
