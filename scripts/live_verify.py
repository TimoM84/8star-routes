"""Limited live verification against the real TomTom Routing API.

Makes at most --max-requests routing requests (default 2: one unrestricted
request per direction) and never more than 10 verification requests in total
for the database it uses. Every request goes through the normal budget guard
and is recorded in the request ledger with purpose 'verification'. Each response
is stored as a regular observation in a manual cycle, and its body is saved
(no API key involved) as a fixture for development.

    python scripts/live_verify.py --max-requests 2 --out tests/fixtures/recorded
    python scripts/live_verify.py --corridors   # also preview draft corridors (1 request each)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.budget import Budget  # noqa: E402
from app.collector import Collector, corridor_rows, direction_rows  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database, iso, migrate, utcnow  # noqa: E402
from app.tomtom import TomTomClient  # noqa: E402

TOTAL_CAP = 10


class Recording(TomTomClient):
    def __init__(self, *a, out: Path, **kw):
        super().__init__(*a, **kw)
        self.out = out
        self.n = 0

    def calculate_route(self, req):
        res = super().calculate_route(req)
        self.n += 1
        self.out.mkdir(parents=True, exist_ok=True)
        name = f"recorded_{utcnow().strftime('%Y%m%dT%H%M%SZ')}_{req.target}_{req.direction_id}_{req.corridor_id or 0}.json"
        payload = {"_note": "Recorded TomTom calculateRoute response (API key never stored).",
                   "request": req.sanitized(), "http_status": res.http_status, "tracking_id": res.tracking_id,
                   "request_utc": iso(res.request_utc), "response_utc": iso(res.response_utc),
                   "error_kind": res.error_kind, "error_message": res.error_message, "response": res.data}
        (self.out / name).write_text(json.dumps(payload, indent=1))
        print(f"  {req.target} dir={req.direction_id} corridor={req.corridor_id}: "
              f"{'ok' if res.ok else res.error_kind} HTTP {res.http_status} -> {name}")
        return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-requests", type=int, default=2)
    ap.add_argument("--corridors", action="store_true", help="also preview draft corridors")
    ap.add_argument("--out", default="tests/fixtures/recorded")
    args = ap.parse_args()
    cfg = load_config()
    if cfg.is_demo:
        sys.exit("Refusing to run in demo mode.")
    if not cfg.tomtom_api_key:
        sys.exit("TOMTOM_API_KEY is not set.")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    migrate(db, cfg.backup_dir)
    if not direction_rows(db):
        sys.exit("No enabled journey. Configure your private locations in Setup first.")
    done = db.scalar("SELECT COUNT(*) FROM request_ledger WHERE purpose = 'verification' AND counted = 1") or 0
    allowed = min(args.max_requests, TOTAL_CAP - done)
    print(f"verification requests so far: {done} of {TOTAL_CAP}; allowed now: {allowed}")
    if allowed <= 0:
        sys.exit("Verification cap reached.")
    budget = Budget(db, cfg)
    client = Recording(cfg.tomtom_api_key, cfg.request_timeout_seconds, out=Path(args.out))
    cfg.max_retries = 0  # verification never retries
    col = Collector(db, cfg, budget, client)
    for d in direction_rows(db):
        if client.n >= allowed:
            break
        now = utcnow()
        cid = db.execute("INSERT INTO collection_cycles(trigger, slot_utc, status, started_utc, worker) VALUES ('manual', ?, 'running', ?, 'live_verify')",
                         (iso(now), iso(now))).lastrowid
        # Only the unrestricted target for this direction.
        from app.collector import Target, build_request
        t = Target(d["id"], build_request(d, None, cfg), None)
        res, lid, not_sent = col.call(t.request, "verification", cid)
        with db.tx():
            ok = col._store(cid, now, t, res, lid, not_sent)
        db.execute("UPDATE collection_cycles SET status=?, finished_utc=?, attempted_requests=1, planned_requests=1 WHERE id=?",
                   ("complete" if ok else "failed", iso(utcnow()), cid))
    if args.corridors:
        for d in direction_rows(db):
            for c in corridor_rows(db, d["id"], ("draft",)):
                if client.n >= allowed:
                    break
                print("  preview:", c["name"], col.preview_corridor(c["id"], "verification").get("ok"))
    print(f"made {client.n} request(s); all recorded in the ledger.")


if __name__ == "__main__":
    main()
