"""TomTom Routing API client, request builder and response parser.

The API key is added only at send time and is never part of a stored URL,
log line or exception message.
"""

from __future__ import annotations

import email.utils
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

log = logging.getLogger("routes.tomtom")
# httpx logs full request URLs (including the key query parameter) at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BASE = "https://api.tomtom.com/routing/1/calculateRoute"


def _loc(p) -> str:
    return f"{p[0]},{p[1]}"


@dataclass
class RouteRequest:
    direction_id: int
    target: str  # unrestricted | corridor
    origin: tuple[float, float]
    destination: tuple[float, float]
    corridor_id: int | None = None
    waypoints: list[list[float]] = field(default_factory=list)
    supporting_points: list[list[float]] | None = None
    max_alternatives: int = 0

    def path(self) -> str:
        locs = [self.origin, *([] if self.supporting_points else self.waypoints), self.destination]
        return "/" + ":".join(_loc(p) for p in locs) + "/json"

    def params(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "traffic": "true",
            "departAt": "now",
            "routeType": "fastest",
            "travelMode": "car",
            "computeTravelTimeFor": "all",
            "routeRepresentation": "polyline",
            "sectionType": "traffic",
            "instructionsType": "coded",
        }
        if self.target == "unrestricted" and self.max_alternatives > 0:
            p["maxAlternatives"] = str(self.max_alternatives)
            p["alternativeType"] = "anyRoute"
        return p

    def body(self) -> dict | None:
        if not self.supporting_points:
            return None
        return {"supportingPoints": [{"latitude": a, "longitude": b} for a, b in self.supporting_points]}

    def method(self) -> str:
        return "POST" if self.supporting_points else "GET"

    def sanitized(self) -> dict:
        """Everything needed to reproduce the request, without credentials."""
        d = {"method": self.method(), "endpoint": "routing/1/calculateRoute", "path": self.path(), "params": self.params()}
        if self.supporting_points:
            d["supporting_points_count"] = len(self.supporting_points)
        return d


@dataclass
class RawResult:
    ok: bool
    http_status: int | None
    request_utc: datetime
    response_utc: datetime | None
    tracking_id: str | None = None
    data: dict | None = None
    error_kind: str | None = None  # auth | quota | invalid | rate_limited | server | timeout | network | invalid_response
    error_message: str | None = None
    retry_after_s: int | None = None

    @property
    def retriable(self) -> bool:
        return self.error_kind in ("server", "timeout", "network", "rate_limited")


@dataclass
class ParsedRoute:
    index: int
    coords: list[list[float]]
    distance_m: int | None
    travel_time_s: int | None
    traffic_delay_s: int | None
    free_flow_s: int | None
    historic_s: int | None
    live_s: int | None
    traffic_length_m: int | None
    departure_time: str | None
    arrival_time: str | None
    roads: list[str]
    traffic_sections: list[dict]


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def parse_routes(data: dict) -> list[ParsedRoute]:
    out = []
    for i, r in enumerate(data.get("routes") or []):
        s = r.get("summary") or {}
        coords: list[list[float]] = []
        for leg in r.get("legs") or []:
            for p in leg.get("points") or []:
                coords.append([p["latitude"], p["longitude"]])
        roads: list[str] = []
        for ins in (r.get("guidance") or {}).get("instructions") or []:
            names = list(ins.get("roadNumbers") or [])
            if ins.get("street"):
                names.append(ins["street"])
            for n in names:
                if n and (not roads or roads[-1] != n) and n not in roads[-3:]:
                    roads.append(n)
        sections = []
        for sec in r.get("sections") or []:
            if (sec.get("sectionType") or "").upper() != "TRAFFIC":
                continue
            sections.append({
                "start": sec.get("startPointIndex"),
                "end": sec.get("endPointIndex"),
                "category": sec.get("simpleCategory"),
                "delay_s": sec.get("delayInSeconds"),
                "speed_kmh": sec.get("effectiveSpeedInKmh"),
                "magnitude": sec.get("magnitudeOfDelay"),
            })
        out.append(ParsedRoute(
            index=i,
            coords=coords,
            distance_m=_int_or_none(s.get("lengthInMeters")),
            travel_time_s=_int_or_none(s.get("travelTimeInSeconds")),
            traffic_delay_s=_int_or_none(s.get("trafficDelayInSeconds")),
            free_flow_s=_int_or_none(s.get("noTrafficTravelTimeInSeconds")),
            historic_s=_int_or_none(s.get("historicTrafficTravelTimeInSeconds")),
            live_s=_int_or_none(s.get("liveTrafficIncidentsTravelTimeInSeconds")),
            traffic_length_m=_int_or_none(s.get("trafficLengthInMeters")),
            departure_time=s.get("departureTime"),
            arrival_time=s.get("arrivalTime"),
            roads=roads[:40],
            traffic_sections=sections,
        ))
    return out


def parse_retry_after(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0, int((dt - now).total_seconds()))
    except (TypeError, ValueError):
        return None


def sanitize(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text[:300]


def classify_error(status: int, body_text: str) -> str:
    low = body_text.lower()
    if status == 429:
        return "rate_limited"
    if status in (401,):
        return "auth"
    if status == 403:
        # TomTom answers 403 for bad keys, per-second throttling ("Over Qps") and exhausted allowances.
        if "qps" in low or "per second" in low:
            return "rate_limited"
        if "over" in low and any(w in low for w in ("limit", "quota", "rate", "queries", "per day", "per month")):
            return "quota"
        return "auth"
    if status in (400, 404, 405, 414):
        return "invalid"
    if status >= 500:
        return "server"
    return "invalid"


def _error_message(status: int, text: str) -> str:
    try:
        j = json.loads(text)
        de = j.get("detailedError") or {}
        if de:
            return f"{de.get('code', '')}: {de.get('message', '')}".strip(": ")
        err = j.get("error") or {}
        if isinstance(err, dict) and err.get("description"):
            return str(err["description"])
        if j.get("errorText"):
            return str(j["errorText"])
    except (ValueError, AttributeError):
        pass
    return f"HTTP {status}"


class TomTomClient:
    def __init__(self, api_key: str, timeout_s: float = 20, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], datetime] | None = None):
        self._key = api_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._http = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False,
                                  headers={"User-Agent": "8star-routes/1.0"})

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def close(self) -> None:
        self._http.close()

    def calculate_route(self, req: RouteRequest) -> RawResult:
        tracking = str(uuid.uuid4())
        params = dict(req.params(), key=self._key)
        started = self._clock()
        try:
            resp = self._http.request(req.method(), BASE + req.path(), params=params, json=req.body(),
                                      headers={"Tracking-ID": tracking, "Accept": "application/json"})
        except httpx.TimeoutException:
            return RawResult(False, None, started, self._clock(), tracking, None, "timeout", "request timed out")
        except httpx.HTTPError as e:
            return RawResult(False, None, started, self._clock(), tracking, None, "network",
                             sanitize(f"{type(e).__name__}: {e}", [self._key]))
        received = self._clock()
        tid = resp.headers.get("Tracking-ID") or tracking
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return RawResult(False, 200, started, received, tid, None, "invalid_response", "response was not JSON")
            if not isinstance(data, dict) or not data.get("routes"):
                return RawResult(False, 200, started, received, tid, None, "invalid_response", "response contained no routes")
            return RawResult(True, 200, started, received, tid, data)
        text = resp.text[:2000]
        kind = classify_error(resp.status_code, text)
        return RawResult(False, resp.status_code, started, received, tid, None, kind,
                         sanitize(_error_message(resp.status_code, text), [self._key]),
                         parse_retry_after(resp.headers.get("Retry-After"), received))
