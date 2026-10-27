"""Request ledger and budget guard.

Every outbound routing call must obtain a ledger entry from `Budget.reserve()`
first. The entry is written, and counted, before the request is sent, so
failed or interrupted requests are always counted (conservative).

The ledger only sees this application's requests. Usage by other
applications on the same TomTom account is represented by the configurable
"other applications" allowance and the opening-usage value; it cannot be
observed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config
from .db import Database, iso, parse_iso, utcnow
from . import timeutil

EDITABLE = {
    "routing_monthly_allowance": int,
    "routing_safety_reserve": int,
    "routing_other_apps_allowance": int,
    "routing_hard_ceiling": int,
    "routing_warning_percent": int,
    "allowance_reset_confirmed": bool,
    "map_tile_allowance": int,
    "traffic_tile_allowance": int,
    "tile_safety_reserve": int,
}


class BudgetBlocked(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class PeriodInfo:
    key: str
    start: datetime
    end: datetime
    resume_at: datetime  # when a new period is trusted (end + grace if reset time unconfirmed)
    in_grace: bool
    previous_key: str


class Budget:
    def __init__(self, db: Database, cfg: Config, clock: Callable[[], datetime] = utcnow):
        self.db = db
        self.cfg = cfg
        self.clock = clock

    # --- settings --------------------------------------------------------------------
    def settings(self) -> dict:
        s = {k: getattr(self.cfg, k) for k in EDITABLE}
        s.update({k: v for k, v in (self.db.get_setting("budget", {}) or {}).items() if k in EDITABLE})
        return s

    def update_settings(self, changes: dict) -> dict:
        cur = self.db.get_setting("budget", {}) or {}
        for k, v in changes.items():
            if k not in EDITABLE:
                raise ValueError(f"unknown budget setting {k}")
            cast = EDITABLE[k]
            v = cast(v)
            if cast is int and v < 0:
                raise ValueError(f"{k} must not be negative")
            cur[k] = v
        self.db.set_setting("budget", cur)
        return self.settings()

    def ceiling(self) -> int:
        """Most counted routing requests this application may make in a period.

        A hard ceiling override replaces the safety reserve, but is never allowed above
        allowance - other applications: a typo must not let the app exceed the provider allowance.
        """
        s = self.settings()
        cap = max(0, s["routing_monthly_allowance"] - s["routing_other_apps_allowance"])
        if s["routing_hard_ceiling"] > 0:
            return min(s["routing_hard_ceiling"], cap)
        return max(0, cap - s["routing_safety_reserve"])

    # --- periods ---------------------------------------------------------------------
    def period(self, now: datetime | None = None) -> PeriodInfo:
        now = now or self.clock()
        tz = ZoneInfo(self.cfg.allowance_reset_timezone)
        key = timeutil.period_key(now, self.cfg.allowance_period, tz)
        start, end = timeutil.period_bounds(now, self.cfg.allowance_period, tz)
        prev_key = timeutil.period_key(start - timedelta(seconds=1), self.cfg.allowance_period, tz)
        grace = timedelta(minutes=0 if self.settings()["allowance_reset_confirmed"] else self.cfg.allowance_reset_grace_minutes)
        return PeriodInfo(key, start, end, end + grace, now < start + grace, prev_key)

    def opening_usage(self, key: str) -> int:
        v = self.db.get_setting(f"opening_usage:{key}")
        if v is not None:
            return int(v)
        if self.db.get_setting("first_period") == key:
            return self.cfg.routing_opening_usage
        return 0

    def set_opening_usage(self, value: int, key: str | None = None) -> None:
        if value < 0:
            raise ValueError("opening usage must not be negative")
        self.db.set_setting(f"opening_usage:{key or self.period().key}", int(value))

    def ledger_count(self, key: str, kind: str = "routing") -> int:
        return self.db.scalar("SELECT COUNT(*) FROM request_ledger WHERE kind = ? AND period_key = ? AND counted = 1", (kind, key)) or 0

    def used(self, key: str) -> int:
        return self.opening_usage(key) + self.ledger_count(key)

    # --- pauses ----------------------------------------------------------------------
    def _key_hash(self) -> str:
        return hashlib.sha256(self.cfg.tomtom_api_key.encode()).hexdigest()[:16]

    def pause_reason(self, now: datetime | None = None) -> tuple[str, str] | None:
        """A provider-driven pause that blocks all routing calls, or None."""
        now = now or self.clock()
        auth = self.db.get_setting("auth_pause")
        if auth:
            if auth.get("key_hash") != self._key_hash():
                self.db.set_setting("auth_pause", None)  # a new key was configured
            else:
                return ("auth_error", "TomTom rejected the API key. Fix the key, then resume collection in Budget.")
        quota = self.db.get_setting("quota_pause")
        if quota:
            p = self.period(now)
            if quota.get("period_key") == p.key or (p.in_grace and quota.get("period_key") == p.previous_key):
                return ("provider_quota", "TomTom reported that the allowance is exhausted. Collection resumes after the next reset.")
        until = parse_iso(self.db.get_setting("backoff_until"))
        if until and now < until:
            return ("backoff", f"TomTom asked to slow down (Retry-After). Waiting until {iso(until)}.")
        return None

    def note_auth_error(self) -> None:
        self.db.set_setting("auth_pause", {"since": iso(self.clock()), "key_hash": self._key_hash()})
        self.db.event("warning", "auth_pause", "Routing paused: TomTom rejected the API key (no automatic retry).")

    def note_quota_error(self) -> None:
        key = self.period().key
        self.db.set_setting("quota_pause", {"since": iso(self.clock()), "period_key": key})
        self.db.event("warning", "quota_pause", f"Routing paused: provider reported exhausted allowance for {key}.")

    def note_retry_after(self, seconds: int) -> None:
        until = self.clock() + timedelta(seconds=max(1, seconds))
        self.db.set_setting("backoff_until", iso(until))

    def clear_pauses(self) -> None:
        for k in ("auth_pause", "quota_pause", "backoff_until"):
            self.db.set_setting(k, None)
        self.db.event("info", "resume", "Routing pauses cleared by administrator.")

    # --- guard -----------------------------------------------------------------------
    def check(self, n: int = 1, now: datetime | None = None) -> tuple[bool, str | None, str | None]:
        """Would `n` more counted requests be allowed? Returns (ok, reason, message)."""
        now = now or self.clock()
        pr = self.pause_reason(now)
        if pr:
            return False, pr[0], pr[1]
        p = self.period(now)
        ceiling = self.ceiling()
        used = self.used(p.key)
        if used + n > ceiling:
            return False, "budget_protection", (
                f"Application ceiling reached: {used} of {ceiling} routing requests used in {p.key}. "
                f"Resumes after {iso(p.resume_at)}.")
        if p.in_grace:
            prev_used = self.used(p.previous_key)
            if prev_used >= ceiling:
                return False, "budget_protection", (
                    f"The previous period was exhausted and the reset time is unconfirmed. Resumes after {iso(p.start + (p.resume_at - p.end))}.")
        return True, None, None

    def reserve(self, purpose: str, endpoint: str, cycle_id: int | None = None, note: str | None = None) -> int:
        """Atomically check the budget and insert a counted ledger row. Raises BudgetBlocked."""
        now = self.clock()
        with self.db.tx(immediate=True) as c:
            ok, reason, message = self.check(1, now)
            p = self.period(now)
            if ok:
                cur = c.execute(
                    "INSERT INTO request_ledger(kind, purpose, period_key, counted, created_utc, outcome, endpoint, cycle_id, note) "
                    "VALUES ('routing', ?, ?, 1, ?, 'pending', ?, ?, ?)",
                    (purpose, p.key, iso(now), endpoint, cycle_id, note))
                return cur.lastrowid
            # Blocked attempts are logged (not counted) and committed before raising.
            outcome = "blocked_backoff" if reason == "backoff" else "blocked_budget"
            c.execute(
                "INSERT INTO request_ledger(kind, purpose, period_key, counted, created_utc, outcome, endpoint, cycle_id, note) "
                "VALUES ('routing', ?, ?, 0, ?, ?, ?, ?, ?)",
                (purpose, p.key, iso(now), outcome, endpoint, cycle_id, message))
        raise BudgetBlocked(reason or "budget_protection", message or "blocked")

    def complete(self, ledger_id: int, outcome: str, http_status: int | None, tracking_id: str | None,
                 sent: datetime | None, completed: datetime | None, note: str | None = None) -> None:
        self.db.execute(
            "UPDATE request_ledger SET outcome = ?, http_status = ?, tracking_id = ?, sent_utc = ?, completed_utc = ?, "
            "note = COALESCE(?, note) WHERE id = ?",
            (outcome, http_status, tracking_id, iso(sent), iso(completed), note, ledger_id))

    def retry_allowed(self) -> bool:
        """Retries only while comfortably below the warning threshold."""
        p = self.period()
        ceiling = self.ceiling()
        return ceiling > 0 and self.used(p.key) < ceiling * self.settings()["routing_warning_percent"] / 100

    # --- tiles -----------------------------------------------------------------------
    def tile_used(self, kind: str, now: datetime | None = None) -> int:
        p = self.period(now)
        return self.db.scalar(
            "SELECT COALESCE(SUM(requests), 0) FROM tile_usage WHERE kind = ? AND day_utc >= ? AND day_utc < ?",
            (kind, p.start.strftime("%Y-%m-%d"), p.end.strftime("%Y-%m-%d"))) or 0

    def tile_ceiling(self, kind: str) -> int:
        s = self.settings()
        allowance = s["map_tile_allowance"] if kind == "map" else s["traffic_tile_allowance"]
        return max(0, allowance - s["tile_safety_reserve"])

    def tile_reserve(self, kind: str) -> bool:
        now = self.clock()
        with self.db.tx(immediate=True) as c:
            if self.tile_used(kind, now) >= self.tile_ceiling(kind):
                return False
            c.execute("INSERT INTO tile_usage(day_utc, kind, requests) VALUES (?, ?, 1) "
                      "ON CONFLICT(day_utc, kind) DO UPDATE SET requests = requests + 1",
                      (now.strftime("%Y-%m-%d"), kind))
        return True

    def tile_error(self, kind: str) -> None:
        self.db.execute("INSERT INTO tile_usage(day_utc, kind, requests, errors) VALUES (?, ?, 0, 1) "
                        "ON CONFLICT(day_utc, kind) DO UPDATE SET errors = errors + 1",
                        (self.clock().strftime("%Y-%m-%d"), kind))

    # --- status & forecast -----------------------------------------------------------
    def daily_usage(self, p: PeriodInfo) -> list[dict]:
        rows = self.db.all(
            "SELECT substr(created_utc, 1, 10) day, purpose, SUM(counted) counted, SUM(outcome NOT IN ('ok', 'pending')) failed_or_blocked "
            "FROM request_ledger WHERE kind = 'routing' AND period_key = ? GROUP BY day, purpose ORDER BY day", (p.key,))
        days: dict[str, dict] = {}
        for r in rows:
            d = days.setdefault(r["day"], {"day_utc": r["day"], "counted": 0, "by_purpose": {}, "failed_or_blocked": 0})
            d["counted"] += r["counted"] or 0
            d["failed_or_blocked"] += r["failed_or_blocked"] or 0
            d["by_purpose"][r["purpose"]] = r["counted"] or 0
        return list(days.values())

    def status(self, requests_per_cycle: int, collection_enabled: bool) -> dict:
        now = self.clock()
        p = self.period(now)
        s = self.settings()
        ceiling = self.ceiling()
        used = self.used(p.key)
        interval = self.cfg.collection_interval_minutes
        remaining_slots = len(timeutil.slots_between(now, p.end, interval)) if collection_enabled else 0
        forecast = remaining_slots * requests_per_cycle
        projected = used + forecast
        exhaust_at = None
        if requests_per_cycle and projected > ceiling:
            cycles_left = max(0, (ceiling - used) // requests_per_cycle)
            exhaust_at = iso(timeutil.next_slot(now, interval) + timedelta(minutes=interval * cycles_left))
        period_days = (p.end - p.start).total_seconds() / 86400
        nominal = requests_per_cycle * (1440 // interval) * math.ceil(period_days)
        ok, reason, message = self.check(max(1, requests_per_cycle), now)
        warn_at = ceiling * s["routing_warning_percent"] / 100
        rows = self.db.all(
            "SELECT purpose, outcome, COUNT(*) n, SUM(counted) counted FROM request_ledger WHERE kind='routing' AND period_key = ? "
            "GROUP BY purpose, outcome ORDER BY purpose, outcome", (p.key,))
        return {
            "period": {"key": p.key, "start_utc": iso(p.start), "end_utc": iso(p.end), "resume_at_utc": iso(p.resume_at),
                       "period_type": self.cfg.allowance_period, "reset_timezone": self.cfg.allowance_reset_timezone,
                       "reset_confirmed": s["allowance_reset_confirmed"], "in_grace": p.in_grace},
            "routing": {
                "allowance": s["routing_monthly_allowance"],
                "safety_reserve": s["routing_safety_reserve"],
                "other_apps_allowance": s["routing_other_apps_allowance"],
                "hard_ceiling_override": s["routing_hard_ceiling"],
                "hard_ceiling_clamped": s["routing_hard_ceiling"] > 0 and ceiling < s["routing_hard_ceiling"],
                "ceiling": ceiling,
                "opening_usage": self.opening_usage(p.key),
                "ledger_counted": self.ledger_count(p.key),
                "used": used,
                "remaining_to_ceiling": max(0, ceiling - used),
                "warning_percent": s["routing_warning_percent"],
                "warning": used >= warn_at,
                "by_purpose_outcome": [dict(r) for r in rows],
            },
            "forecast": {
                "requests_per_cycle": requests_per_cycle,
                "interval_minutes": interval,
                "remaining_slots": remaining_slots,
                "remaining_scheduled_requests": forecast,
                "projected_period_total": projected,
                "projected_to_exceed_ceiling": projected > ceiling,
                "projected_pause_at_utc": exhaust_at,
                "nominal_full_period_requests": nominal,
                # Left for retries, manual refreshes and previews once every remaining scheduled cycle has run.
                "spare_after_schedule": ceiling - projected,
            },
            "daily": self.daily_usage(p),
            "collection_allowed": ok,
            "blocked_reason": reason,
            "blocked_message": message,
            "tiles": {
                "map": {"used": self.tile_used("map", now), "ceiling": self.tile_ceiling("map"), "allowance": s["map_tile_allowance"],
                        "observable": self.cfg.map_tile_mode == "proxy"},
                "traffic": {"used": self.tile_used("traffic", now), "ceiling": self.tile_ceiling("traffic"), "allowance": s["traffic_tile_allowance"],
                            "observable": self.cfg.map_tile_mode == "proxy"},
            },
            "scope_note": "Counts only requests made by this application (plus the opening usage you entered). "
                          "Usage by other applications on the same TomTom key is not visible here.",
        }
