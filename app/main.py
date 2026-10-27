"""FastAPI application: web UI, JSON API, exports, health and tile proxy."""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, analysis
from . import geometry as geo
from .auth import COOKIE, SESSION_MAX_AGE, Auth
from .budget import Budget
from .collector import Collector, corridor_rows, direction_rows, plan_targets, requests_per_cycle, variant_geometry
from .config import Config, load_config
from .db import Database, backup, iso, migrate, parse_iso, utcnow
from .scheduler import Scheduler, collection_enabled, recover_after_restart
from .seed import CORRIDOR_COLORS
from .tiles import TileProxy
from .tomtom import TomTomClient

log = logging.getLogger("routes")
STATIC = Path(__file__).parent / "static"


def _parse_day(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"Invalid date: {s}") from None


def _parse_hhmm(s: str | None) -> int | None:
    if not s:
        return None
    try:
        h, m = s.split(":")
        v = int(h) * 60 + int(m)
    except ValueError:
        raise HTTPException(400, f"Invalid time: {s}") from None
    if not 0 <= v <= 24 * 60:
        raise HTTPException(400, f"Invalid time: {s}")
    return v


def _coord(v, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a number") from None
    return f


def _check_latlon(lat: float, lon: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Coordinates out of range")


class AppState:
    def __init__(self, cfg: Config, client=None, start_scheduler: bool = True, demo_days: int = 35):
        self.cfg = cfg
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(cfg.db_path)
        self.auth = Auth(cfg)
        self.start_scheduler = start_scheduler
        self.demo_days = demo_days
        self.demo_generating = False
        self.manual_lock = threading.Lock()
        self.last_manual: float | None = None
        self.scheduler: Scheduler | None = None
        if client is not None:
            self.client = client
        elif cfg.is_demo:
            from .demo import FakeTomTom
            self.client = FakeTomTom(utcnow, cfg.tz)
        else:
            self.client = TomTomClient(cfg.tomtom_api_key, cfg.request_timeout_seconds)
        self.tiles = TileProxy(cfg, self.budget)

    def budget(self) -> Budget:
        return Budget(self.db, self.cfg)

    def collector(self, db: Database | None = None) -> Collector:
        db = db or self.db
        sleep = self.scheduler.interruptible_sleep if self.scheduler else None
        return Collector(db, self.cfg, Budget(db, self.cfg), self.client, sleep=sleep)

    def startup(self) -> None:
        migrate(self.db, self.cfg.backup_dir)
        if self.db.get_setting("first_period") is None:
            self.db.set_setting("first_period", self.budget().period().key)
        recover_after_restart(self.db, self.budget())
        if self.cfg.is_demo and not self.db.scalar("SELECT COUNT(*) FROM observations"):
            self.demo_generating = True
            threading.Thread(target=self._generate_demo, daemon=True).start()
        elif self.start_scheduler:
            self._start_scheduler()

    def _generate_demo(self) -> None:
        from .demo import generate_history
        db = Database(self.cfg.db_path)
        try:
            generate_history(db, self.cfg, days=self.demo_days, end=utcnow() - timedelta(minutes=1))
        finally:
            db.close()
            self.demo_generating = False
        if self.start_scheduler:
            self._start_scheduler()

    def _start_scheduler(self) -> None:
        self.scheduler = Scheduler(lambda: Database(self.cfg.db_path), self.cfg, lambda db: self.collector(db))
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler:
            self.scheduler.stop(timeout=self.cfg.request_timeout_seconds + 10)
        if hasattr(self.client, "close"):
            self.client.close()
        self.db.close()


def create_app(cfg: Config | None = None, client=None, start_scheduler: bool = True, demo_days: int = 35) -> FastAPI:
    cfg = cfg or load_config()
    problems = cfg.validate()
    if problems:
        raise SystemExit("Configuration problems:\n- " + "\n- ".join(problems))
    state = AppState(cfg, client=client, start_scheduler=start_scheduler, demo_days=demo_days)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.startup()
        if state.auth.ephemeral_secret and cfg.auth_mode == "password":
            log.warning("SESSION_SECRET not set: sessions will not survive a restart")
        if cfg.auth_mode == "none":
            log.warning("AUTH_MODE=none: the interface is unprotected. Only use behind an authenticating proxy.")
        yield
        await state.tiles.close()
        state.shutdown()

    app = FastAPI(title="8star-routes", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.s = state
    B = cfg.base_path
    r = APIRouter(prefix=B)
    def _viewer(request: Request) -> str:
        return state.auth.require_viewer(request)

    def _admin(request: Request) -> str:
        return state.auth.require_admin(request)

    viewer, admin = Depends(_viewer), Depends(_admin)
    db = state.db

    def source() -> str:
        return "demo" if cfg.is_demo else "live"

    def J(data, status: int = 200) -> JSONResponse:
        if isinstance(data, dict):
            data = {"data_source": source(), **data}
        return JSONResponse(data, status_code=status)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        img = "'self' data:" + (" https://api.tomtom.com" if cfg.map_tile_mode == "direct" else "")
        resp.headers.setdefault("Content-Security-Policy",
                                f"default-src 'self'; img-src {img}; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                                f"connect-src 'self'; frame-ancestors {cfg.frame_ancestors}; base-uri 'self'; form-action 'self'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith(B + "/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # --- pages & health ------------------------------------------------------------
    def page(name: str) -> HTMLResponse:
        html = (STATIC / name).read_text(encoding="utf-8").replace("{{BASE}}", B or "").replace("{{VERSION}}", __version__)
        return HTMLResponse(html)

    @r.get("/", include_in_schema=False)
    def index():
        return page("index.html")

    @r.get("/dashboard", include_in_schema=False)
    def dashboard():
        return page("dashboard.html")

    @r.get("/healthz")
    def healthz():
        try:
            db.scalar("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        last = db.one("SELECT slot_utc, status FROM collection_cycles WHERE trigger='schedule' ORDER BY slot_utc DESC LIMIT 1")
        sch = state.scheduler
        alive = bool(sch and sch.thread and sch.thread.is_alive())
        body = {"status": "ok" if db_ok else "error", "mode": cfg.app_mode, "version": __version__, "database": db_ok,
                "scheduler_running": alive, "scheduler_active": bool(sch and sch.active),
                "demo_generating": state.demo_generating,
                "last_slot": dict(last) if last else None}
        return JSONResponse(body, status_code=200 if db_ok else 503)

    # --- session -------------------------------------------------------------------
    @r.post("/api/login")
    def login(request: Request, payload: dict = Body(...)):
        ip = request.client.host if request.client else "?"
        if state.auth.rate_limited(ip):
            raise HTTPException(429, "Too many attempts, try again in a few minutes")
        if not state.auth.check_password(ip, str(payload.get("password", ""))):
            time.sleep(0.5)
            raise HTTPException(401, "Wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, state.auth.issue(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
                        secure=state.auth.cookie_secure(request), path=(B or "/"))
        return resp

    @r.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path=(B or "/"))
        return resp

    @r.get("/api/session")
    def session(request: Request):
        return {"role": state.auth.role(request), "auth_mode": cfg.auth_mode}

    # --- meta ----------------------------------------------------------------------
    @r.get("/api/meta", dependencies=[viewer])
    def meta(request: Request):
        b = state.budget()
        rpc = requests_per_cycle(db, cfg)
        enabled = collection_enabled(db, cfg)
        st = b.status(rpc, enabled)
        return J({
            "version": __version__, "mode": cfg.app_mode, "timezone": cfg.timezone, "role": state.auth.role(request),
            "interval_minutes": cfg.collection_interval_minutes, "stale_after_minutes": cfg.stale_after_minutes,
            "collection_enabled": enabled, "collection_allowed": st["collection_allowed"],
            "blocked_reason": st["blocked_reason"], "blocked_message": st["blocked_message"],
            "budget_warning": st["routing"]["warning"], "requests_per_cycle": rpc,
            "tomtom_key_configured": bool(cfg.tomtom_api_key) or cfg.is_demo,
            "demo_generating": state.demo_generating,
            "map": {"mode": cfg.map_tile_mode,
                    "tile_url": (f"{B}/tiles/map/{{z}}/{{x}}/{{y}}.png" if cfg.map_tile_mode == "proxy" else
                                 cfg.map_tile_url + f"&key={cfg.tomtom_map_key}" if cfg.map_tile_mode == "direct" and cfg.tomtom_map_key else None),
                    "traffic_url": (f"{B}/tiles/traffic/{{z}}/{{x}}/{{y}}.png" if cfg.map_tile_mode == "proxy" else
                                    cfg.traffic_tile_url + f"&key={cfg.tomtom_map_key}" if cfg.map_tile_mode == "direct" and cfg.tomtom_map_key else None),
                    "traffic_refresh_minutes": max(5, cfg.traffic_refresh_minutes),
                    "tiles_available": cfg.map_tile_mode != "off" and (bool(cfg.tomtom_api_key) if cfg.map_tile_mode == "proxy" else bool(cfg.tomtom_map_key))},
            "directions": analysis.overview(db, cfg),
            "pending_drafts": db.scalar("SELECT COUNT(*) FROM corridors WHERE status='draft'"),
            "open_deviations": db.scalar("SELECT COUNT(*) FROM observations WHERE deviation_flag=1 AND intended_utc > ?",
                                         (iso(utcnow() - timedelta(days=7)),)),
        })

    def _dir(direction_id: int) -> dict:
        d = next((x for x in direction_rows(db, enabled_only=False) if x["id"] == direction_id), None)
        if d is None:
            raise HTTPException(404, "Unknown direction")
        return d

    # --- current & history ---------------------------------------------------------
    @r.get("/api/latest", dependencies=[viewer])
    def api_latest(direction_id: int):
        _dir(direction_id)
        return J(analysis.latest(db, cfg, direction_id))

    @r.get("/api/cycles/nearest", dependencies=[viewer])
    def nearest_cycle(direction_id: int, at: str):
        _dir(direction_id)
        t = parse_iso(at)
        if t is None:
            raise HTTPException(400, "at must be an ISO timestamp")
        row = db.one("SELECT id, slot_utc FROM collection_cycles WHERE trigger='schedule' ORDER BY ABS(julianday(slot_utc) - julianday(?)) LIMIT 1",
                     (iso(t),))
        if not row:
            raise HTTPException(404, "No cycles recorded yet")
        return J({"cycle_id": row["id"], "slot_utc": row["slot_utc"]})

    @r.get("/api/cycles/{cycle_id}", dependencies=[viewer])
    def api_cycle(cycle_id: int, direction_id: int):
        _dir(direction_id)
        d = analysis.cycle_detail(db, cfg, cycle_id, direction_id)
        if d is None:
            raise HTTPException(404, "Unknown cycle")
        return J(d)

    @r.get("/api/variants/{variant_id}", dependencies=[viewer])
    def api_variant(variant_id: int):
        v = analysis.variant_geojson(db, variant_id)
        if v is None:
            raise HTTPException(404, "Unknown variant")
        return J(v)

    @r.get("/api/variants", dependencies=[viewer])
    def api_variants(direction_id: int, key: str = "recommended"):
        _dir(direction_id)
        if key == "recommended":
            where, params = "o.role = 'recommended'", ()
        elif key == "unrestricted":
            where, params = "o.target = 'unrestricted'", ()
        elif key.startswith("corridor:"):
            where, params = "o.role = 'corridor' AND o.corridor_id = ?", (int(key.split(":")[1]),)
        else:
            raise HTTPException(400, "Invalid key")
        rows = db.all(
            f"SELECT v.id, v.length_m, v.point_count, v.roads_json, MIN(o.intended_utc) first_utc, MAX(o.intended_utc) last_utc, COUNT(*) n, "
            f"SUM(o.deviation_flag) deviations, MAX(o.deviation_max_m) max_dev_m, MAX(o.resembles_corridor_id) resembles "
            f"FROM observations o JOIN route_variants v ON v.id = o.variant_id WHERE o.direction_id = ? AND {where} "
            f"GROUP BY v.id ORDER BY n DESC LIMIT 200", (direction_id, *params))
        return J({"variants": [{**dict(x), "roads": json.loads(x["roads_json"])} for x in rows]})

    @r.get("/api/day", dependencies=[viewer])
    def api_day(direction_id: int, day: str | None = Query(None, alias="date")):
        _dir(direction_id)
        d = _parse_day(day, utcnow().astimezone(cfg.tz).date())
        return J(analysis.day_series(db, cfg, direction_id, d))

    def _range(frm, to) -> tuple[date, date]:
        today = utcnow().astimezone(cfg.tz).date()
        a = _parse_day(frm, today - timedelta(days=27))
        b = _parse_day(to, today)
        if b < a:
            raise HTTPException(400, "'to' is before 'from'")
        if (b - a).days > 400:
            raise HTTPException(400, "Range too long (max 400 days)")
        return a, b

    @r.get("/api/stats", dependencies=[viewer])
    def api_stats(direction_id: int, frm: str | None = Query(None, alias="from"), to: str | None = None,
                  weekdays: str | None = None, time_from: str | None = None, time_to: str | None = None):
        _dir(direction_id)
        a, b = _range(frm, to)
        wd = None
        if weekdays:
            try:
                wd = {int(x) for x in weekdays.split(",") if x != ""}
            except ValueError:
                raise HTTPException(400, "weekdays must be a comma separated list of 0-6") from None
        return J(analysis.stats(db, cfg, direction_id, a, b, wd, _parse_hhmm(time_from), _parse_hhmm(time_to)))

    @r.get("/api/heatmap", dependencies=[viewer])
    def api_heatmap(direction_id: int, key: str = "recommended", frm: str | None = Query(None, alias="from"), to: str | None = None,
                    bucket: int = 30, metric: str = "travel_time_s"):
        _dir(direction_id)
        if metric not in ("travel_time_s", "traffic_delay_s"):
            raise HTTPException(400, "metric must be travel_time_s or traffic_delay_s")
        if bucket not in (15, 30, 60):
            raise HTTPException(400, "bucket must be 15, 30 or 60")
        a, b = _range(frm, to)
        return J(analysis.heatmap(db, cfg, direction_id, key, a, b, bucket, metric))

    @r.get("/api/plan", dependencies=[viewer])
    def api_plan(direction_id: int, day: str = Query(..., alias="date"), arrive: str = "09:00", pct: int = 90, weeks: int = 12,
                 min_samples: int = 4, same_weekday: bool = True):
        _dir(direction_id)
        if pct not in (50, 75, 80, 90, 95):
            raise HTTPException(400, "pct must be one of 50, 75, 80, 90, 95")
        if not 1 <= weeks <= 52 or not 2 <= min_samples <= 50:
            raise HTTPException(400, "weeks must be 1-52 and min_samples 2-50")
        return J(analysis.plan_arrival(db, cfg, direction_id, _parse_day(day, date.today()), _parse_hhmm(arrive), pct, weeks,
                                       min_samples, same_weekday=same_weekday))

    @r.get("/api/events", dependencies=[viewer])
    def api_events(limit: int = 50):
        rows = db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(500, max(1, limit)),))
        return J({"events": [dict(x) for x in rows]})

    # --- budget --------------------------------------------------------------------
    @r.get("/api/budget", dependencies=[viewer])
    def api_budget():
        b = state.budget()
        st = b.status(requests_per_cycle(db, cfg), collection_enabled(db, cfg))
        st["ledger"] = [dict(x) for x in db.all("SELECT id, purpose, outcome, counted, created_utc, sent_utc, completed_utc, http_status, "
                                                "tracking_id, endpoint, cycle_id, note FROM request_ledger ORDER BY id DESC LIMIT 100")]
        return J(st)

    @r.put("/api/budget", dependencies=[admin])
    def api_budget_update(payload: dict = Body(...)):
        b = state.budget()
        opening = payload.pop("opening_usage", None)
        try:
            if payload:
                b.update_settings(payload)
            if opening is not None:
                b.set_opening_usage(int(opening))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from None
        db.event("info", "budget", f"Budget settings changed: {', '.join(list(payload) + (['opening_usage'] if opening is not None else []))}")
        return api_budget()

    @r.post("/api/budget/resume", dependencies=[admin])
    def api_resume():
        state.budget().clear_pauses()
        return {"ok": True}

    @r.put("/api/collection", dependencies=[admin])
    def api_collection(payload: dict = Body(...)):
        enabled = bool(payload.get("enabled"))
        db.set_setting("collection_enabled", enabled)
        db.event("info", "collection", f"Scheduled collection {'enabled' if enabled else 'disabled'} by administrator.")
        return {"enabled": enabled}

    @r.post("/api/collect", dependencies=[admin])
    def api_collect(payload: dict = Body(default={})):
        """Explicit manual refresh. Goes through the same budget guard as scheduled cycles."""
        ids = payload.get("direction_ids") or None
        if ids:
            for i in ids:
                _dir(int(i))
        targets = plan_targets(db, cfg, ids)
        if not targets:
            raise HTTPException(400, "Nothing to collect (no enabled directions)")
        if not state.manual_lock.acquire(blocking=False):
            raise HTTPException(409, "A manual refresh is already running")
        try:
            if state.last_manual is not None and time.monotonic() - state.last_manual < 60:
                raise HTTPException(429, "Manual refreshes are limited to one per minute")
            ok, reason, message = state.budget().check(len(targets))
            if not ok:
                raise HTTPException(409, f"Blocked by budget guard ({reason}): {message}")
            state.last_manual = time.monotonic()
            now = utcnow()
            cid = db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status, started_utc, worker) VALUES ('manual', ?, 'running', ?, 'api')",
                             (iso(now), iso(now))).lastrowid
            status = state.collector().run_cycle(cid, now, [int(i) for i in ids] if ids else None, purpose="manual")
            return J({"cycle_id": cid, "status": status, "requests_planned": len(targets)})
        finally:
            state.manual_lock.release()

    # --- setup: journeys, directions, corridors -----------------------------------------
    @r.get("/api/setup", dependencies=[viewer])
    def api_setup():
        journeys = [dict(x) for x in db.all("SELECT * FROM journeys ORDER BY id")]
        dirs = []
        for d in direction_rows(db, enabled_only=False):
            cs = []
            for c in corridor_rows(db, d["id"], ("draft", "confirmed", "retired"), include_hidden=True):
                c = dict(c)
                c["hidden"] = c["hidden_utc"] is not None
                c["waypoints"] = json.loads(c.pop("waypoints_json") or "[]")
                sp = c.pop("supporting_points_json")
                c["supporting_points_count"] = len(json.loads(sp)) if sp else 0
                c["observations"] = db.scalar("SELECT COUNT(*) FROM observations WHERE corridor_id = ?", (c["id"],))
                c["deviations"] = db.scalar("SELECT COUNT(*) FROM observations WHERE corridor_id = ? AND deviation_flag = 1", (c["id"],))
                cs.append(c)
            dirs.append({k: d[k] for k in ("id", "code", "enabled", "journey_id", "name", "from", "to", "from_label", "to_label")} | {"corridors": cs})
        return J({"journeys": journeys, "directions": dirs, "colors": CORRIDOR_COLORS})

    def _journey_fields(p: dict) -> dict:
        out = {}
        for k in ("name", "origin_label", "dest_label"):
            if k in p:
                v = str(p[k]).strip()
                if not v or len(v) > 120:
                    raise HTTPException(400, f"{k} must be 1-120 characters")
                out[k] = v
        for k in ("origin_lat", "origin_lon", "dest_lat", "dest_lon"):
            if k in p:
                out[k] = _coord(p[k], k)
        if "origin_lat" in out or "origin_lon" in out:
            _check_latlon(out.get("origin_lat", 0), out.get("origin_lon", 0))
        if "dest_lat" in out or "dest_lon" in out:
            _check_latlon(out.get("dest_lat", 0), out.get("dest_lon", 0))
        return out

    @r.post("/api/journeys", dependencies=[admin])
    def api_journey_create(payload: dict = Body(...)):
        f = _journey_fields(payload)
        need = ("name", "origin_label", "origin_lat", "origin_lon", "dest_label", "dest_lat", "dest_lon")
        if any(k not in f for k in need):
            raise HTTPException(400, f"Required: {', '.join(need)}")
        now = iso(utcnow())
        with db.tx():
            jid = db.execute("INSERT INTO journeys(name, origin_label, origin_lat, origin_lon, dest_label, dest_lat, dest_lon, created_utc, updated_utc) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", (*(f[k] for k in need), now, now)).lastrowid
            for code in ("outbound", "return"):
                db.execute("INSERT INTO directions(journey_id, code, enabled) VALUES (?, ?, ?)", (jid, code, int(bool(payload.get("enabled", False)))))
        return {"id": jid}

    @r.put("/api/journeys/{journey_id}", dependencies=[admin])
    def api_journey_update(journey_id: int, payload: dict = Body(...)):
        if not db.one("SELECT 1 FROM journeys WHERE id = ?", (journey_id,)):
            raise HTTPException(404)
        f = _journey_fields(payload)
        if f:
            sets = ", ".join(f"{k} = ?" for k in f)
            db.execute(f"UPDATE journeys SET {sets}, updated_utc = ? WHERE id = ?", (*f.values(), iso(utcnow()), journey_id))
            if any(k.endswith(("_lat", "_lon")) for k in f):
                db.event("info", "journey", f"Journey {journey_id} coordinates changed. Earlier observations keep their original request parameters.")
        return {"ok": True}

    @r.put("/api/directions/{direction_id}", dependencies=[admin])
    def api_direction_update(direction_id: int, payload: dict = Body(...)):
        _dir(direction_id)
        db.execute("UPDATE directions SET enabled = ? WHERE id = ?", (int(bool(payload.get("enabled"))), direction_id))
        return {"ok": True}

    def _corridor(cid: int) -> dict:
        row = db.one("SELECT * FROM corridors WHERE id = ?", (cid,))
        if not row:
            raise HTTPException(404, "Unknown corridor")
        return dict(row)

    def _waypoints(v) -> list[list[float]]:
        if not isinstance(v, list) or len(v) > 20:
            raise HTTPException(400, "waypoints must be a list of at most 20 [lat, lon] pairs")
        out = []
        for p in v:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise HTTPException(400, "each waypoint must be [lat, lon]")
            lat, lon = _coord(p[0], "lat"), _coord(p[1], "lon")
            _check_latlon(lat, lon)
            out.append([lat, lon])
        return out

    @r.post("/api/corridors", dependencies=[admin])
    def api_corridor_create(payload: dict = Body(...)):
        did = int(payload.get("direction_id", 0))
        _dir(did)
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 80:
            raise HTTPException(400, "name must be 1-80 characters")
        wps = _waypoints(payload.get("waypoints", []))
        if not wps:
            raise HTTPException(400, "A waypoint corridor needs at least one via point")
        color = str(payload.get("color") or CORRIDOR_COLORS[0])
        now = iso(utcnow())
        cid = db.execute("INSERT INTO corridors(direction_id, name, color, method, waypoints_json, status, source, sort, created_utc, updated_utc) "
                         "VALUES (?, ?, ?, 'waypoints', ?, 'draft', 'user', 99, ?, ?)", (did, name, color, json.dumps(wps), now, now)).lastrowid
        return {"id": cid}

    @r.post("/api/corridors/from-variant", dependencies=[admin])
    def api_corridor_from_variant(payload: dict = Body(...)):
        """Create a draft 'exact geometry' corridor from a route TomTom returned earlier (no request needed)."""
        vid = int(payload.get("variant_id", 0))
        row = db.one("SELECT id, direction_id FROM route_variants WHERE id = ?", (vid,))
        if not row:
            raise HTTPException(404, "Unknown route variant")
        name = str(payload.get("name", "")).strip() or f"Route variant {vid}"
        coords = variant_geometry(db, vid)
        sp = geo.supporting_points(coords)
        now = iso(utcnow())
        used = {c["color"] for c in corridor_rows(db, row["direction_id"], ("draft", "confirmed"))}
        color = next((c for c in CORRIDOR_COLORS if c not in used), CORRIDOR_COLORS[0])
        cid = db.execute(
            "INSERT INTO corridors(direction_id, name, color, method, waypoints_json, supporting_points_json, status, source, "
            "preview_variant_id, preview_utc, note, sort, created_utc, updated_utc) VALUES (?, ?, ?, 'reconstruct', '[]', ?, 'draft', 'from_route', ?, ?, ?, 99, ?, ?)",
            (row["direction_id"], name[:80], color, json.dumps(sp), vid, now,
             f"Exact geometry from route variant {vid}, rebuilt with {len(sp)} supporting points.", now, now)).lastrowid
        return {"id": cid}

    @r.put("/api/corridors/{cid}", dependencies=[admin])
    def api_corridor_update(cid: int, payload: dict = Body(...)):
        c = _corridor(cid)
        sets, vals = [], []
        if "name" in payload:
            n = str(payload["name"]).strip()
            if not n or len(n) > 80:
                raise HTTPException(400, "name must be 1-80 characters")
            sets.append("name = ?"); vals.append(n)
        if "color" in payload:
            sets.append("color = ?"); vals.append(str(payload["color"])[:20])
        if "deviation_threshold_m" in payload:
            try:
                t = int(payload["deviation_threshold_m"])
            except (TypeError, ValueError):
                raise HTTPException(400, "deviation_threshold_m must be a whole number") from None
            if not 50 <= t <= 5000:
                raise HTTPException(400, "deviation_threshold_m must be 50-5000")
            sets.append("deviation_threshold_m = ?"); vals.append(t)
            if t != c["deviation_threshold_m"]:
                db.event("info", "corridor", f"Corridor '{c['name']}' deviation threshold changed from {c['deviation_threshold_m']} m to {t} m "
                                             "(applies to new observations).")
        if "note" in payload:
            sets.append("note = ?"); vals.append(str(payload["note"])[:500])
        if "waypoints" in payload:
            if c["status"] != "draft":
                raise HTTPException(409, "A confirmed corridor's route cannot change, or its history would mix two routes. Retire it and create a new one.")
            if c["method"] != "waypoints":
                raise HTTPException(400, "This corridor uses an exact geometry, not via points")
            sets.append("waypoints_json = ?"); vals.append(json.dumps(_waypoints(payload["waypoints"])))
            sets.append("preview_variant_id = NULL"); sets.append("preview_utc = NULL")
        if sets:
            db.execute(f"UPDATE corridors SET {', '.join(sets)}, updated_utc = ? WHERE id = ?", (*vals, iso(utcnow()), cid))
        return {"ok": True}

    @r.post("/api/corridors/{cid}/preview", dependencies=[admin])
    def api_corridor_preview(cid: int):
        _corridor(cid)
        res = state.collector().preview_corridor(cid)
        if not res.get("ok"):
            return J(res, 409)
        return J(res)

    @r.post("/api/corridors/{cid}/confirm", dependencies=[admin])
    def api_corridor_confirm(cid: int):
        c = _corridor(cid)
        if c["status"] != "draft":
            raise HTTPException(409, "Only draft corridors can be confirmed")
        if not c["preview_variant_id"]:
            raise HTTPException(409, "Preview the corridor on the map before confirming it")
        db.execute("UPDATE corridors SET status='confirmed', reference_variant_id = preview_variant_id, confirmed_utc = ?, updated_utc = ? WHERE id = ?",
                   (iso(utcnow()), iso(utcnow()), cid))
        db.event("info", "corridor", f"Corridor '{c['name']}' confirmed by administrator.")
        return {"ok": True}

    @r.post("/api/corridors/{cid}/retire", dependencies=[admin])
    def api_corridor_retire(cid: int):
        c = _corridor(cid)
        db.execute("UPDATE corridors SET status='retired', updated_utc = ? WHERE id = ?", (iso(utcnow()), cid))
        db.event("info", "corridor", f"Corridor '{c['name']}' retired; its history is kept.")
        return {"ok": True}

    @r.post("/api/corridors/{cid}/hide", dependencies=[admin])
    def api_corridor_hide(cid: int):
        c = _corridor(cid)
        if c["status"] != "retired":
            raise HTTPException(409, "Retire the corridor before hiding it")
        db.execute("UPDATE corridors SET hidden_utc = ?, updated_utc = ? WHERE id = ?", (iso(utcnow()), iso(utcnow()), cid))
        db.event("info", "corridor", f"Retired corridor '{c['name']}' hidden; its observations are kept.")
        return {"ok": True}

    @r.post("/api/corridors/{cid}/unhide", dependencies=[admin])
    def api_corridor_unhide(cid: int):
        c = _corridor(cid)
        db.execute("UPDATE corridors SET hidden_utc = NULL, updated_utc = ? WHERE id = ?", (iso(utcnow()), cid))
        db.event("info", "corridor", f"Corridor '{c['name']}' shown again.")
        return {"ok": True}

    @r.delete("/api/corridors/{cid}", dependencies=[admin])
    def api_corridor_delete(cid: int, purge: bool = False):
        """Delete a corridor. With observations only when retired and purge=true, which deletes its history too."""
        c = _corridor(cid)
        n = db.scalar("SELECT COUNT(*) FROM observations WHERE corridor_id = ?", (cid,))
        if n and not (purge and c["status"] == "retired"):
            raise HTTPException(409, "This corridor has observations. Retire and hide it to keep its history, "
                                     "or delete a retired corridor with purge=true to remove its observations as well.")
        with db.tx():
            db.execute("DELETE FROM observations WHERE corridor_id = ? AND role = 'corridor'", (cid,))
            db.execute("DELETE FROM corridors WHERE id = ?", (cid,))  # remaining references become NULL (ON DELETE SET NULL)
        db.event("info", "corridor", f"Corridor '{c['name']}' deleted" + (f" with {n} observations." if n else "."))
        return {"ok": True}

    # --- admin misc ------------------------------------------------------------------
    @r.get("/api/config", dependencies=[admin])
    def api_config():
        return J({"config": cfg.describe(), "data_dir": str(cfg.data_dir)})

    @r.post("/api/backup", dependencies=[admin])
    def api_backup():
        p = backup(db, cfg.backup_dir, prefix="manual", keep=cfg.backup_keep)
        db.event("info", "backup", f"Manual backup written: {p.name}")
        return {"file": p.name, "size_bytes": p.stat().st_size}

    # --- exports ---------------------------------------------------------------------
    @r.get("/api/export/observations.csv", dependencies=[viewer])
    def export_csv(direction_id: int | None = None, frm: str | None = Query(None, alias="from"), to: str | None = None):
        a, b = _range(frm, to) if (frm or to) else (date(2000, 1, 1), date(2100, 1, 1))
        start = datetime.combine(a, datetime.min.time(), tzinfo=cfg.tz)
        end = datetime.combine(b + timedelta(days=1), datetime.min.time(), tzinfo=cfg.tz)
        cols = ["observation_id", "data_source", "cycle_id", "cycle_trigger", "cycle_status", "direction_id", "direction_code", "intended_utc",
                "intended_local", "request_utc", "response_utc", "target", "role", "corridor_id", "corridor_name", "returned_index",
                "variant_id", "resembles_corridor_id", "status", "failure_reason", "http_status", "distance_m", "travel_time_s",
                "traffic_delay_s", "free_flow_s", "historic_s", "live_s", "traffic_length_m", "departure_time", "arrival_time",
                "deviation_flag", "deviation_share", "deviation_max_m", "tracking_id"]
        q = ("SELECT o.*, c.trigger cycle_trigger, c.status cycle_status, d.code direction_code, cr.name corridor_name FROM observations o "
             "JOIN collection_cycles c ON c.id = o.cycle_id JOIN directions d ON d.id = o.direction_id LEFT JOIN corridors cr ON cr.id = o.corridor_id "
             "WHERE o.intended_utc >= ? AND o.intended_utc < ?" + (" AND o.direction_id = ?" if direction_id else "") + " ORDER BY o.intended_utc, o.id")
        params = (iso(start), iso(end), *( [direction_id] if direction_id else []))

        def gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            yield buf.getvalue()
            local_db = Database(cfg.db_path)
            try:
                for o in local_db.conn.execute(q, params):
                    buf.seek(0); buf.truncate()
                    row = dict(o)
                    row["observation_id"] = row["id"]
                    row["data_source"] = source()
                    row["intended_local"] = parse_iso(row["intended_utc"]).astimezone(cfg.tz).isoformat()
                    vals = []
                    for c_ in cols:
                        v = row.get(c_)
                        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") and c_ in ("corridor_name", "failure_reason"):
                            v = "'" + v  # neutralise spreadsheet formulas
                        vals.append("" if v is None else v)
                    w.writerow(vals)
                    yield buf.getvalue()
            finally:
                local_db.close()
        return StreamingResponse(gen(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="8star-routes-observations-{source()}.csv"'})

    @r.get("/api/export/geometries.geojson", dependencies=[viewer])
    def export_geojson(direction_id: int | None = None):
        rows = db.all("SELECT v.*, d.code FROM route_variants v JOIN directions d ON d.id = v.direction_id"
                      + (" WHERE v.direction_id = ?" if direction_id else "") + " ORDER BY v.id", ((direction_id,) if direction_id else ()))
        feats = []
        for v in rows:
            coords = geo.decompress(v["geometry_z"])
            refs = db.one("SELECT COUNT(*) n, GROUP_CONCAT(DISTINCT role) roles, GROUP_CONCAT(DISTINCT corridor_id) corridors FROM observations WHERE variant_id = ?", (v["id"],))
            feats.append({"type": "Feature", "id": v["id"],
                          "geometry": {"type": "LineString", "coordinates": [[c[1], c[0]] for c in coords]},
                          "properties": {"variant_id": v["id"], "direction_id": v["direction_id"], "direction_code": v["code"],
                                         "fingerprint": v["fingerprint"], "length_m": v["length_m"], "point_count": v["point_count"],
                                         "roads": json.loads(v["roads_json"]), "first_seen_utc": v["first_seen_utc"],
                                         "last_seen_utc": v["last_seen_utc"], "observations": refs["n"], "roles": refs["roles"],
                                         "corridor_ids": refs["corridors"], "data_source": source()}})
        return Response(json.dumps({"type": "FeatureCollection", "features": feats}), media_type="application/geo+json",
                        headers={"Content-Disposition": f'attachment; filename="8star-routes-geometries-{source()}.geojson"'})

    # --- read-only API for Home Assistant -------------------------------------------
    @r.get("/api/v1/latest", dependencies=[viewer])
    def v1_latest():
        return J({"note": "TomTom estimates at the time of observation, not measured journeys.", "directions": _v1_directions()})

    @r.get("/api/v1/latest/{code}", dependencies=[viewer])
    def v1_latest_direction(code: str):
        for d in _v1_directions():
            if d["code"] == code or str(d["direction_id"]) == code:
                return J(d)
        raise HTTPException(404, "Unknown direction code")

    def _v1_directions() -> list[dict]:
        out = []
        for d in direction_rows(db, enabled_only=False):
            lt = analysis.latest(db, cfg, d["id"])
            det = lt["detail"]
            routes = []
            if det:
                for x in det["routes"]:
                    if x["role"] == "alternative":
                        continue
                    routes.append({"key": x["key"], "name": x["name"], "color": x["color"], "status": x["status"],
                                   "travel_time_min": None if x["travel_time_s"] is None else round(x["travel_time_s"] / 60, 1),
                                   "travel_time_s": x["travel_time_s"], "traffic_delay_s": x["traffic_delay_s"],
                                   "distance_km": None if x["distance_m"] is None else round(x["distance_m"] / 1000, 1),
                                   "resembles": x["resembles"]["name"] if x.get("resembles") else None,
                                   "deviation_flag": bool(x["deviation_flag"])})
            ok = [x for x in routes if x["status"] == "ok" and x["travel_time_s"] is not None]
            fastest = min(ok, key=lambda x: x["travel_time_s"]) if ok else None
            rec = next((x for x in routes if x["key"] == "recommended"), None)
            out.append({"direction_id": d["id"], "code": d["code"], "name": d["name"],
                        "observed_utc": det["intended_utc"] if det else None,
                        "age_minutes": None if lt["age_s"] is None else round(lt["age_s"] / 60, 1),
                        "stale": lt["stale"], "partial": lt["partial"],
                        "recommended_travel_time_min": rec["travel_time_min"] if rec else None,
                        "recommended_traffic_delay_min": None if not rec or rec["traffic_delay_s"] is None else round(rec["traffic_delay_s"] / 60, 1),
                        "recommended_resembles": rec["resembles"] if rec else None,
                        "fastest_route": fastest["name"] if fastest else None,
                        "fastest_travel_time_min": fastest["travel_time_min"] if fastest else None,
                        "routes": routes})
        return out

    # --- tiles -----------------------------------------------------------------------
    @r.get("/tiles/{kind}/{z}/{x}/{y}.png")
    async def tile(kind: str, z: int, x: int, y: int, request: Request, embed: str | None = None):
        role = state.auth.role(request)
        if role is None and embed and cfg.embed_token:
            if hmac.compare_digest(embed.encode(), cfg.embed_token.encode()):
                role = "viewer"
        if role is None:
            raise HTTPException(401, "Login required")
        return await state.tiles.fetch(kind, z, x, y, request)

    app.include_router(r)
    app.mount((B or "") + "/static", StaticFiles(directory=STATIC), name="static")
    if B:
        @app.get("/", include_in_schema=False)
        def root_redirect():
            return Response(status_code=307, headers={"Location": B + "/"})
    return app
