"""Quarter-hour scheduler.

Guarantees:
- Only one scheduler loop is active at a time (database lease), even with
  several workers or containers on the same database.
- A slot runs at most once: the cycle row has a UNIQUE slot timestamp and is
  claimed with INSERT before any request is made.
- Only the current slot can run, and only within the lateness tolerance. Past
  slots are written as 'missed' without making requests (no catch-up burst).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import datetime, timedelta
from typing import Callable

from . import timeutil
from .budget import Budget
from .collector import Collector, requests_per_cycle
from .config import Config
from .db import Database, backup, iso, parse_iso, utcnow

log = logging.getLogger("routes.scheduler")
LEASE_SECONDS = 90
MAX_MISSED_FILL_DAYS = 120


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def acquire_lease(db: Database, holder: str, now: datetime, name: str = "scheduler") -> bool:
    with db.tx(immediate=True) as c:
        row = c.execute("SELECT holder, expires_utc FROM scheduler_lease WHERE name = ?", (name,)).fetchone()
        exp = iso(now + timedelta(seconds=LEASE_SECONDS))
        if row is None:
            c.execute("INSERT INTO scheduler_lease(name, holder, expires_utc) VALUES (?, ?, ?)", (name, holder, exp))
            return True
        if row["holder"] == holder or parse_iso(row["expires_utc"]) < now:
            c.execute("UPDATE scheduler_lease SET holder = ?, expires_utc = ? WHERE name = ?", (holder, exp, name))
            return True
        return False


def release_lease(db: Database, holder: str, name: str = "scheduler") -> None:
    db.execute("DELETE FROM scheduler_lease WHERE name = ? AND holder = ?", (name, holder))


def collection_enabled(db: Database, cfg: Config) -> bool:
    v = db.get_setting("collection_enabled")
    return cfg.collection_enabled if v is None else bool(v)


def claim_slot(db: Database, slot: datetime, holder: str, now: datetime) -> int | None:
    """Insert the cycle row for `slot`. Returns its id, or None if it already exists."""
    cur = db.execute(
        "INSERT INTO collection_cycles(trigger, slot_utc, status, started_utc, worker) VALUES ('schedule', ?, 'running', ?, ?) "
        "ON CONFLICT DO NOTHING", (iso(slot), iso(now), holder))
    return cur.lastrowid if cur.rowcount == 1 else None


def record_skipped(db: Database, slot: datetime, status: str, reason: str, planned: int = 0) -> None:
    db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status, reason, planned_requests, finished_utc) "
               "VALUES ('schedule', ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", (iso(slot), status, reason, planned, iso(utcnow())))


def fill_missed(db: Database, cfg: Config, now: datetime) -> int:
    """Write 'missed' rows for past slots that have no cycle row, after the first recorded slot."""
    first = db.scalar("SELECT MIN(slot_utc) FROM collection_cycles WHERE trigger = 'schedule'")
    last = db.scalar("SELECT MAX(slot_utc) FROM collection_cycles WHERE trigger = 'schedule'")
    if not first:
        return 0
    interval = cfg.collection_interval_minutes
    start = max(parse_iso(last), now - timedelta(days=MAX_MISSED_FILL_DAYS))
    # Only slots before the current one: the current slot may still run.
    end = timeutil.floor_slot(now, interval)
    n = 0
    with db.tx():
        for s in timeutil.slots_between(start + timedelta(seconds=1), end, interval):
            cur = db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status, reason, finished_utc) VALUES "
                             "('schedule', ?, 'missed', 'no collection ran in this slot (application offline, restarting or late)', ?) "
                             "ON CONFLICT DO NOTHING", (iso(s), iso(now)))
            n += cur.rowcount
    return n


def recover_after_restart(db: Database, budget: Budget) -> None:
    """Close cycles and ledger entries left open by a crash or hard stop."""
    n = db.execute("UPDATE collection_cycles SET status = 'interrupted', reason = 'application stopped during cycle', "
                   "finished_utc = ? WHERE status = 'running'", (iso(utcnow()),)).rowcount
    m = db.execute("UPDATE request_ledger SET outcome = 'interrupted', note = 'outcome unknown: application stopped' "
                   "WHERE outcome = 'pending'").rowcount
    if n or m:
        db.event("warning", "recovery", f"Recovered after restart: {n} open cycle(s), {m} pending request(s) (still counted).")


def purge_before(db: Database, cutoff: str) -> int:
    """Retention: delete observations and cycles older than `cutoff`. The request ledger is kept (budget history).

    Ledger rows keep their counts but lose the link to the deleted cycle, otherwise the foreign key would
    block the delete. Geometries still used by a corridor or as a route family's representative are kept.
    """
    with db.tx():
        n = db.execute("DELETE FROM observations WHERE intended_utc < ?", (cutoff,)).rowcount
        db.execute("UPDATE request_ledger SET cycle_id = NULL WHERE cycle_id IN "
                   "(SELECT id FROM collection_cycles WHERE COALESCE(slot_utc, started_utc) < ?)", (cutoff,))
        db.execute("DELETE FROM collection_cycles WHERE COALESCE(slot_utc, started_utc) < ?", (cutoff,))
        db.execute("DELETE FROM route_variants WHERE last_seen_utc < ? AND id NOT IN (SELECT variant_id FROM observations WHERE variant_id IS NOT NULL) "
                   "AND id NOT IN (SELECT reference_variant_id FROM corridors WHERE reference_variant_id IS NOT NULL) "
                   "AND id NOT IN (SELECT preview_variant_id FROM corridors WHERE preview_variant_id IS NOT NULL) "
                   "AND id NOT IN (SELECT representative_variant_id FROM route_families)", (cutoff,))
    if n:
        db.event("info", "retention", f"Retention removed {n} observations older than {cutoff}.")
    return n


class Scheduler:
    def __init__(self, db_factory: Callable[[], Database], cfg: Config, make_collector: Callable[[Database], Collector],
                 clock: Callable[[], datetime] = utcnow):
        self.db_factory = db_factory
        self.cfg = cfg
        self.make_collector = make_collector
        self.clock = clock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.holder = worker_id()
        self.last_heartbeat: datetime | None = None
        self.active = False  # holds the lease

    # A single decision step; the loop calls this every few seconds. Tests call it directly.
    def step(self, db: Database, collector: Collector, now: datetime) -> str:
        self.last_heartbeat = now
        if not acquire_lease(db, self.holder, now):
            self.active = False
            return "standby"
        self.active = True
        fill_missed(db, self.cfg, now)
        slot = timeutil.due_slot(now, self.cfg.collection_interval_minutes, self.cfg.collection_late_tolerance_seconds)
        if slot is None:
            return "idle"
        if db.scalar("SELECT 1 FROM collection_cycles WHERE trigger='schedule' AND slot_utc = ?", (iso(slot),)):
            return "done"
        planned = requests_per_cycle(db, self.cfg)
        if not collection_enabled(db, self.cfg):
            record_skipped(db, slot, "skipped", "collection disabled by administrator", planned)
            return "skipped"
        ok, reason, message = collector.budget.check(max(1, planned), now)
        if not ok:
            record_skipped(db, slot, "skipped", f"{reason}: {message}", planned)
            return "skipped"
        cid = claim_slot(db, slot, self.holder, now)
        if cid is None:
            return "done"
        status = collector.run_cycle(cid, slot, should_stop=self.stop_event.is_set)
        return f"ran:{status}"

    def _housekeeping(self, db: Database, now: datetime) -> None:
        local = now.astimezone(self.cfg.tz)
        if self.cfg.daily_backup and local.hour == 3 and db.get_setting("last_daily_backup") != local.date().isoformat():
            try:
                path = backup(db, self.cfg.backup_dir, prefix="daily", keep=self.cfg.backup_keep)
                db.set_setting("last_daily_backup", local.date().isoformat())
                db.event("info", "backup", f"Daily backup written: {path.name}")
            except Exception as e:  # pragma: no cover - disk problems
                db.event("warning", "backup", f"Daily backup failed: {type(e).__name__}")
        if self.cfg.observation_retention_days > 0 and db.get_setting("last_retention") != local.date().isoformat():
            cutoff = iso(now - timedelta(days=self.cfg.observation_retention_days))
            purge_before(db, cutoff)
            db.set_setting("last_retention", local.date().isoformat())

    def _run(self) -> None:
        db = self.db_factory()
        collector = self.make_collector(db)
        log.info("scheduler started (%s, every %s min)", self.holder, self.cfg.collection_interval_minutes)
        while not self.stop_event.is_set():
            now = self.clock()
            try:
                result = self.step(db, collector, now)
                if result.startswith("ran") or result == "skipped":
                    log.info("slot %s: %s", iso(timeutil.floor_slot(now, self.cfg.collection_interval_minutes)), result)
                if self.active:
                    self._housekeeping(db, now)
            except Exception:
                log.exception("scheduler step failed")
            # Wake at the next slot boundary or every 15 s for the lease, whichever is first.
            nxt = timeutil.next_slot(self.clock(), self.cfg.collection_interval_minutes)
            wait = max(0.5, min(15.0, (nxt - self.clock()).total_seconds() + 0.2))
            self.stop_event.wait(wait)
        release_lease(db, self.holder)
        db.close()
        log.info("scheduler stopped")

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 30) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)

    def interruptible_sleep(self, seconds: float) -> bool:
        return not self.stop_event.wait(seconds)
