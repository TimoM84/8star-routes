import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import timeutil
from app.budget import Budget
from app.collector import Collector
from app.db import Database
from app.scheduler import Scheduler, acquire_lease, recover_after_restart
from conftest import Clock, ScriptedClient, confirm_corridors

AMS = ZoneInfo("Europe/Amsterdam")


def make_scheduler(env, clock=None, client=None):
    clock = clock or env.clock
    client = client or env.client
    col = Collector(env.db, env.cfg, Budget(env.db, env.cfg, clock), client, clock=clock, sleep=lambda s: True)
    return Scheduler(lambda: env.db, env.cfg, lambda db: col, clock=clock), col


def scheduled(db):
    return db.all("SELECT slot_utc, status FROM collection_cycles WHERE trigger='schedule' ORDER BY slot_utc")


def test_runs_each_slot_once(env):
    confirm_corridors(env.db, env.clock, only_outbound=False)
    s, col = make_scheduler(env)
    assert s.step(env.db, col, env.clock()) == "ran:complete"
    assert len(env.client.calls) == 6  # 2 directions x (unrestricted + 2 corridors)
    env.clock.advance(seconds=5)
    assert s.step(env.db, col, env.clock()) == "done"
    assert len(env.client.calls) == 6


def test_no_duplicate_cycles_from_concurrent_workers(env):
    """Several workers (own connections, own lease holders) racing on the same slot."""
    clock = Clock(env.clock())
    calls = []
    lock = threading.Lock()

    class CountingClient(ScriptedClient):
        def calculate_route(self, req):
            with lock:
                calls.append(req)
            return self.ok(self.default_routes(req))

    barrier = threading.Barrier(4)
    results = []

    def worker():
        db = Database(env.cfg.db_path)
        client = CountingClient(clock)
        col = Collector(db, env.cfg, Budget(db, env.cfg, clock), client, clock=clock, sleep=lambda s: True)
        sch = Scheduler(lambda: db, env.cfg, lambda d: col, clock=clock)
        sch.holder = f"worker-{threading.get_ident()}"
        barrier.wait()
        results.append(sch.step(db, col, clock()))
        # The lease expired / was stolen: even then the UNIQUE slot claim prevents a second run.
        db.execute("DELETE FROM scheduler_lease")
        results.append(sch.step(db, col, clock()))
        db.close()

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for r in results if r.startswith("ran")) == 1
    assert len(calls) == 2  # one cycle, two enabled directions, no confirmed corridors
    assert env.db.scalar("SELECT COUNT(*) FROM collection_cycles WHERE trigger='schedule'") == 1


def test_lease_allows_one_active_scheduler(env):
    now = env.clock()
    assert acquire_lease(env.db, "a", now)
    assert not acquire_lease(env.db, "b", now + timedelta(seconds=10))
    assert acquire_lease(env.db, "b", now + timedelta(seconds=200))  # a's lease expired


def test_no_catch_up_storm_after_downtime(env):
    s, col = make_scheduler(env)
    s.step(env.db, col, env.clock())  # 06:00 slot runs
    assert len(env.client.calls) == 2
    # Down for 5 hours; comes back 7 minutes after a slot boundary.
    env.clock.t = datetime(2026, 10, 20, 11, 7, 0, tzinfo=timezone.utc)
    for _ in range(20):  # loop wakes repeatedly
        s.step(env.db, col, env.clock())
        env.clock.advance(seconds=5)
    assert len(env.client.calls) == 2, "no requests for past slots"
    rows = scheduled(env.db)
    missed = [r for r in rows if r["status"] == "missed"]
    assert len(missed) == 19  # 06:15 .. 10:45; 11:00 is still the current slot (window passed, filled later)
    # Next slot runs normally, exactly once.
    env.clock.t = datetime(2026, 10, 20, 11, 15, 2, tzinfo=timezone.utc)
    s.step(env.db, col, env.clock())
    assert len(env.client.calls) == 4
    assert all(r["status"] != "running" for r in scheduled(env.db))
    assert scheduled(env.db)[-2]["slot_utc"] == "2026-10-20T11:00:00.000Z"


def test_late_wakeup_skips_slot_instead_of_running_it(env):
    s, col = make_scheduler(env)
    env.clock.t = datetime(2026, 10, 20, 6, 3, 30, tzinfo=timezone.utc)  # 210 s late, tolerance 120 s
    assert s.step(env.db, col, env.clock()) == "idle"
    assert env.client.calls == []


def test_budget_paused_cycles_are_recorded_explicitly(env):
    env.budget.update_settings({"routing_monthly_allowance": 1, "routing_safety_reserve": 0})
    s, col = make_scheduler(env)
    assert s.step(env.db, col, env.clock()) == "skipped"
    row = env.db.one("SELECT status, reason, planned_requests FROM collection_cycles")
    assert row["status"] == "skipped" and row["reason"].startswith("budget_protection") and row["planned_requests"] == 2
    assert env.client.calls == []


def test_collection_disabled_is_recorded(env):
    env.db.set_setting("collection_enabled", False)
    s, col = make_scheduler(env)
    assert s.step(env.db, col, env.clock()) == "skipped"
    assert env.client.calls == []


def test_dst_fall_back_day_has_100_distinct_slots():
    day = date(2026, 10, 25)  # CEST -> CET
    slots = timeutil.slots_in_local_day(day, AMS, 15)
    assert len(slots) == 100
    assert len(set(slots)) == 100
    locals_ = [s.astimezone(AMS) for s in slots]
    two_am = [l for l in locals_ if l.hour == 2]
    assert len(two_am) == 8  # 02:00-02:45 twice, with different offsets
    assert {l.utcoffset() for l in two_am} == {timedelta(hours=2), timedelta(hours=1)}


def test_dst_spring_forward_day_has_92_slots():
    slots = timeutil.slots_in_local_day(date(2026, 3, 29), AMS, 15)
    assert len(slots) == 92
    assert not [s for s in slots if s.astimezone(AMS).hour == 2]


def test_scheduler_across_dst_runs_every_slot_once(env):
    """Simulate the whole fall-back night: one cycle per UTC quarter hour, none doubled or skipped."""
    start = datetime(2026, 10, 24, 22, 0, 2, tzinfo=timezone.utc)  # 00:00 CEST
    env.clock.t = start
    s, col = make_scheduler(env)
    t = start
    while t < datetime(2026, 10, 25, 23, 0, 2, tzinfo=timezone.utc):  # 00:00 CET next day
        env.clock.t = t
        s.step(env.db, col, t)
        t += timedelta(minutes=15)
    rows = scheduled(env.db)
    assert len(rows) == 100
    assert all(r["status"] == "complete" for r in rows)
    assert len(env.client.calls) == 200


def test_local_time_helpers_handle_gaps():
    assert timeutil.local_from_parts(date(2026, 3, 29), 2 * 60 + 30, AMS) is None
    assert timeutil.local_from_parts(date(2026, 10, 25), 2 * 60 + 30, AMS) is not None


def test_restart_recovery_closes_open_cycles(env):
    env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('schedule', '2026-10-20T05:45:00.000Z', 'running')")
    env.db.execute("INSERT INTO request_ledger(kind, purpose, period_key, counted, created_utc, outcome) VALUES ('routing','schedule','2026-10',1,'x','pending')")
    recover_after_restart(env.db, env.budget)
    assert env.db.scalar("SELECT status FROM collection_cycles") == "interrupted"
    assert env.db.scalar("SELECT outcome FROM request_ledger") == "interrupted"
    assert env.db.scalar("SELECT counted FROM request_ledger") == 1  # still counted
    s, col = make_scheduler(env)
    s.step(env.db, col, env.clock())
    assert env.db.scalar("SELECT COUNT(*) FROM collection_cycles WHERE slot_utc='2026-10-20T05:45:00.000Z'") == 1


def test_shutdown_during_cycle_marks_it_interrupted(env):
    confirm_corridors(env.db, env.clock, only_outbound=False)
    s, col = make_scheduler(env)
    stops = iter([False, False, True])
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('schedule', 'x', 'running')").lastrowid
    assert col.run_cycle(cid, env.clock(), should_stop=lambda: next(stops, True)) == "interrupted"
    assert len(env.client.calls) == 2
