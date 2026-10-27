"""Route geometry: fingerprints, compression, similarity and deviation.

Coordinates are [lat, lon] pairs at the precision TomTom returned them.
Distances use a local equirectangular projection, which is accurate to well
under 1 % at commuting scales.
"""

from __future__ import annotations

import hashlib
import json
import math
import zlib
from dataclasses import dataclass

EARTH_R = 6371008.8
Point = tuple[float, float]


def fingerprint(coords: list[list[float]]) -> str:
    canonical = json.dumps(coords, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def compress(coords: list[list[float]]) -> bytes:
    return zlib.compress(json.dumps(coords, separators=(",", ":")).encode(), 9)


def decompress(blob: bytes) -> list[list[float]]:
    return json.loads(zlib.decompress(blob))


def haversine(a: Point, b: Point) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def length_m(coords: list[list[float]]) -> float:
    return sum(haversine(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


class _Proj:
    def __init__(self, lat0: float):
        self.kx = math.cos(math.radians(lat0)) * math.pi * EARTH_R / 180
        self.ky = math.pi * EARTH_R / 180

    def xy(self, p) -> tuple[float, float]:
        return p[1] * self.kx, p[0] * self.ky


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(coords: list[list[float]], tolerance_m: float) -> list[list[float]]:
    """Douglas-Peucker simplification (iterative)."""
    if len(coords) < 3:
        return list(coords)
    proj = _Proj(coords[0][0])
    pts = [proj.xy(c) for c in coords]
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i, j = stack.pop()
        ax, ay = pts[i]
        bx, by = pts[j]
        best, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _seg_dist(pts[k][0], pts[k][1], ax, ay, bx, by)
            if d > best:
                best, idx = d, k
        if best > tolerance_m and idx > 0:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [c for c, k in zip(coords, keep) if k]


def resample(coords: list[list[float]], spacing_m: float) -> list[list[float]]:
    """Points roughly every `spacing_m` along the line, always including the ends."""
    if len(coords) < 2:
        return list(coords)
    out = [coords[0]]
    acc = 0.0
    for i in range(1, len(coords)):
        d = haversine(coords[i - 1], coords[i])
        acc += d
        if acc >= spacing_m:
            out.append(coords[i])
            acc = 0.0
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


class PolylineIndex:
    """Fast nearest-distance queries against one polyline via a uniform grid."""

    def __init__(self, coords: list[list[float]], cell_m: float = 500.0):
        self.proj = _Proj(coords[0][0] if coords else 52.0)
        self.cell = cell_m
        self.segs: list[tuple[float, float, float, float]] = []
        self.grid: dict[tuple[int, int], list[int]] = {}
        pts = [self.proj.xy(c) for c in coords]
        if len(pts) == 1:
            pts = pts * 2
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            self.segs.append((ax, ay, bx, by))
            for gx in range(int(math.floor(min(ax, bx) / cell_m)), int(math.floor(max(ax, bx) / cell_m)) + 1):
                for gy in range(int(math.floor(min(ay, by) / cell_m)), int(math.floor(max(ay, by) / cell_m)) + 1):
                    self.grid.setdefault((gx, gy), []).append(i)

    def distance(self, p, cap_m: float = 5000.0) -> float:
        """Distance in metres to the polyline, capped at `cap_m` (returned when further)."""
        px, py = self.proj.xy(p)
        cx, cy = int(math.floor(px / self.cell)), int(math.floor(py / self.cell))
        best = math.inf
        for r in range(0, int(math.ceil(cap_m / self.cell)) + 2):
            seen = set()
            for gx in range(cx - r, cx + r + 1):
                for gy in (range(cy - r, cy + r + 1) if abs(gx - cx) == r else (cy - r, cy + r)):
                    for i in self.grid.get((gx, gy), ()):
                        if i in seen:
                            continue
                        seen.add(i)
                        d = _seg_dist(px, py, *self.segs[i])
                        if d < best:
                            best = d
            # Anything in ring r+1 or beyond is at least r*cell away.
            if best <= r * self.cell:
                return min(best, cap_m)
        return min(best, cap_m)


_INDEX_CACHE: dict[str, PolylineIndex] = {}


def index_for(coords: list[list[float]]) -> PolylineIndex:
    """Cached index for reference geometries that are queried repeatedly."""
    key = fingerprint(coords)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        if len(_INDEX_CACHE) > 256:
            _INDEX_CACHE.clear()
        idx = _INDEX_CACHE[key] = PolylineIndex(coords)
    return idx


@dataclass
class Deviation:
    share_outside: float  # share of sampled route points further than threshold from reference
    max_m: float
    length_ratio: float
    flagged: bool
    outside_m: float = 0.0  # approximate length of route beyond the threshold


DEVIATION_SAMPLE_M = 100
DEVIATION_MIN_OUTSIDE_M = 500


def deviation(route: list[list[float]], reference: list[list[float]], threshold_m: float,
              share_limit: float = 0.05, length_tolerance: float = 0.15,
              min_outside_m: float = DEVIATION_MIN_OUTSIDE_M) -> Deviation:
    """How far `route` strays from `reference`.

    Flagged when more than `share_limit` of the route, and at least `min_outside_m`
    of it, lies beyond `threshold_m` from the reference, or when the length differs
    by more than `length_tolerance`. The minimum stretch keeps short, harmless
    geometry differences (a ramp, a parallel service road) from raising alerts.
    """
    if len(route) < 2 or len(reference) < 2:
        return Deviation(1.0, math.inf, 0.0, True)
    idx = index_for(reference)
    sample = resample(route, DEVIATION_SAMPLE_M)
    dists = [idx.distance(p) for p in sample]
    n_out = sum(1 for d in dists if d > threshold_m)
    outside = n_out / len(dists)
    outside_m = n_out * DEVIATION_SAMPLE_M  # samples are at least this far apart; spikes are not over-counted
    lr = length_m(route) / max(1.0, length_m(reference))
    flagged = (outside > share_limit and outside_m >= min_outside_m) or abs(lr - 1) > length_tolerance
    return Deviation(outside, max(dists), lr, flagged, outside_m)


def similarity(a: list[list[float]], b: list[list[float]], threshold_m: float = 250.0) -> float:
    """Symmetric share of each line lying within `threshold_m` of the other (0..1)."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ia, ib = index_for(a), index_for(b)
    sa, sb = resample(a, 150), resample(b, 150)
    cap = threshold_m * 2
    ab = sum(1 for p in sa if ib.distance(p, cap) <= threshold_m) / len(sa)
    ba = sum(1 for p in sb if ia.distance(p, cap) <= threshold_m) / len(sb)
    return min(ab, ba)


def supporting_points(coords: list[list[float]], max_points: int = 400) -> list[list[float]]:
    """Reduce a geometry to supporting points for TomTom route reconstruction."""
    pts = simplify(coords, 5)
    tol = 5.0
    while len(pts) > max_points:
        tol *= 1.6
        pts = simplify(coords, tol)
    return pts
