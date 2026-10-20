from datetime import datetime, timezone

import pytest

from app.budget import BudgetBlocked
from conftest import ScriptedClient, confirm_corridors, outbound_only


def counted(db):
    return db.scalar("SELECT COUNT(*) FROM request_ledger WHERE counted = 1")


def test_guard_blocks_before_any_outbound_request(env):
    env.budget.update_settings({"routing_monthly_allowance": 3, "routing_safety_reserve": 0})
    outbound_only(env.db)
    confirm_corridors(env.db, env.clock)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', '2026-10-20T06:00:00.000Z', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert len(env.client.calls) == 3
    # Ceiling reached: the next cycle must not reach the client at all.
    cid2 = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', '2026-10-20T06:15:00.000Z', 'running')").lastrowid
    status = env.collector.run_cycle(cid2, env.clock())
    assert len(env.client.calls) == 3
    assert status == "failed"
    reasons = [r["failure_reason"] for r in env.db.all("SELECT failure_reason FROM observations WHERE cycle_id = ?", (cid2,))]
    assert reasons and all(r.startswith("not requested: budget_protection") for r in reasons)
    blocked = env.db.scalar("SELECT COUNT(*) FROM request_ledger WHERE outcome = 'blocked_budget' AND counted = 0")
    assert blocked == 3


def test_ceiling_accounts_for_reserve_other_apps_and_opening_usage(env):
    env.budget.update_settings({"routing_monthly_allowance": 20000, "routing_safety_reserve": 2000, "routing_other_apps_allowance": 1500})
    assert env.budget.ceiling() == 16500
    env.budget.set_opening_usage(16499)
    ok, _, _ = env.budget.check(1)
    assert ok
    ok, reason, _ = env.budget.check(2)
    assert not ok and reason == "budget_protection"
    env.budget.update_settings({"routing_hard_ceiling": 17000})
    assert env.budget.ceiling() == 17000


def test_failed_requests_are_counted(env):
    env.client.script = lambda req, n: env.client.error(500, "server")
    env.cfg.max_retries = 0
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', '2026-10-20T06:00:00.000Z', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert counted(env.db) == 1
    assert env.db.scalar("SELECT outcome FROM request_ledger") == "http_error"


def test_auth_error_is_not_retried_and_pauses(env):
    env.client.script = lambda req, n: env.client.error(403, "auth")
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert len(env.client.calls) == 1  # no retry
    ok, reason, _ = env.budget.check(1)
    assert not ok and reason == "auth_error"
    env.budget.clear_pauses()
    assert env.budget.check(1)[0]


def test_invalid_parameters_not_retried(env):
    env.client.script = lambda req, n: env.client.error(400, "invalid", msg="MAP_MATCHING_FAILURE: bad waypoint")
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert len(env.client.calls) == 1
    reason = env.db.scalar("SELECT failure_reason FROM observations")
    assert "MAP_MATCHING_FAILURE" in reason


def test_rate_limit_honours_retry_after(env):
    waits = []
    env.collector.sleep = lambda s: (waits.append(s), True)[1]
    env.client.script = lambda req, n: env.client.error(429, "rate_limited", retry_after=7) if n == 1 else env.client.ok(env.client.default_routes(req))
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    assert env.collector.run_cycle(cid, env.clock()) == "complete"
    assert waits == [7]
    assert counted(env.db) == 2  # both attempts counted
    assert env.db.scalar("SELECT COUNT(*) FROM request_ledger WHERE purpose = 'retry'") == 1


def test_long_retry_after_sets_backoff_instead_of_waiting(env):
    env.client.script = lambda req, n: env.client.error(429, "rate_limited", retry_after=3600)
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert len(env.client.calls) == 1
    ok, reason, _ = env.budget.check(1)
    assert not ok and reason == "backoff"
    with pytest.raises(BudgetBlocked):
        env.budget.reserve("manual", "x")
    assert counted(env.db) == 1


def test_temporary_failures_retried_a_limited_number_of_times(env):
    env.cfg.max_retries = 2
    env.client.script = lambda req, n: env.client.error(503, "server")
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert len(env.client.calls) == 3


def test_no_retry_when_budget_is_low(env):
    env.cfg.max_retries = 3
    env.budget.update_settings({"routing_monthly_allowance": 100, "routing_safety_reserve": 0, "routing_warning_percent": 80})
    env.budget.set_opening_usage(85)
    env.client.script = lambda req, n: env.client.error(503, "server")
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert len(env.client.calls) == 1


def test_quota_error_pauses_until_next_period(env):
    env.client.script = lambda req, n: env.client.error(403, "quota")
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert env.budget.check(1)[1] == "provider_quota"
    # Next month, after the unconfirmed-reset grace period: resumes automatically.
    env.clock.t = datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)
    assert env.budget.check(1)[1] == "provider_quota"  # still within the grace hour
    env.clock.t = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)
    assert env.budget.check(1)[0]


def test_budget_pause_resumes_automatically_after_reset(env):
    env.budget.update_settings({"routing_monthly_allowance": 10, "routing_safety_reserve": 0})
    env.budget.set_opening_usage(10)
    assert env.budget.check(1)[1] == "budget_protection"
    env.clock.t = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)
    assert env.budget.check(1)[0]


def test_forecast(env):
    env.clock.t = datetime(2026, 10, 31, 0, 0, 3, tzinfo=timezone.utc)
    st = env.budget.status(requests_per_cycle=6, collection_enabled=True)
    assert st["forecast"]["remaining_slots"] == 95  # rest of the last day, current slot already started
    assert st["forecast"]["remaining_scheduled_requests"] == 570
    assert st["forecast"]["nominal_full_period_requests"] == 17856  # 31 days * 96 * 6
    assert "not visible" in st["scope_note"]


def test_tile_counters_and_ceiling(env):
    env.budget.update_settings({"map_tile_allowance": 3, "tile_safety_reserve": 1})
    assert env.budget.tile_reserve("map") and env.budget.tile_reserve("map")
    assert not env.budget.tile_reserve("map")
    assert env.budget.tile_used("map") == 2
    assert env.budget.tile_used("traffic") == 0


def test_concurrent_reservations_never_exceed_ceiling(env, tmp_path):
    import threading
    from app.budget import Budget
    from app.db import Database
    env.budget.update_settings({"routing_monthly_allowance": 25, "routing_safety_reserve": 0})
    got = []

    def worker():
        db = Database(env.cfg.db_path)
        b = Budget(db, env.cfg, env.clock)
        for _ in range(20):
            try:
                got.append(b.reserve("schedule", "x"))
            except BudgetBlocked:
                pass
        db.close()

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 25
    assert counted(env.db) == 25


def test_unconfigured_key_counts_nothing_externally(env):
    env.client.configured = False
    outbound_only(env.db)
    cid = env.db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status) VALUES ('manual', 'x', 'running')").lastrowid
    env.collector.run_cycle(cid, env.clock())
    assert env.client.calls == []
    assert isinstance(env.client, ScriptedClient)
