"""Dry-run the database upgrade on a COPY and prove that no stored data changes.

    python scripts/check_upgrade.py [/data/routes.sqlite] [--work-dir /tmp]

The live database is only read (with SQLite's online backup API, safe while the
application runs). The copy is migrated exactly as at startup (pending SQL
migrations, then route-family assignment) and every row of every table that
existed before is compared column by column. No routing requests are made.
Exit code 0 means: all existing rows are unchanged.
"""
import hashlib
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import families  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database, migrate  # noqa: E402


def table_digests(path: Path, columns: dict | None = None) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = (columns or {}).get(t) or [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        rows = {}
        for row in conn.execute(f"SELECT rowid, {', '.join(cols)} FROM {t}"):
            rows[row[0]] = hashlib.sha256(repr(row[1:]).encode()).hexdigest()
        out[t] = (cols, rows)
    conn.close()
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    work = Path(sys.argv[sys.argv.index("--work-dir") + 1]) if "--work-dir" in sys.argv else Path(tempfile.gettempdir())
    src = Path(args[0]) if args else load_config().db_path
    if not src.is_file():
        print(f"Database not found: {src}")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="routes-upgrade-check-", dir=work))
    copy = tmp / "routes.sqlite"
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(copy)
    s.backup(d)
    s.close(), d.close()
    before = table_digests(copy)
    db = Database(copy)
    t0 = time.time()
    applied = migrate(db, backup_dir=None)
    assigned = families.backfill(db)
    took = time.time() - t0
    integrity = db.scalar("PRAGMA integrity_check")
    fk = db.all("PRAGMA foreign_key_check")
    db.close()
    after = table_digests(copy, {t: v[0] for t, v in before.items()})
    problems = []
    print(f"Source: {src}\nCopy:   {copy}\nApplied migrations: {applied or 'none (already up to date)'}; "
          f"route families assigned to {assigned} geometries in {took:.1f} s")
    print(f"{'table':22} {'rows before':>12} {'rows after':>12}  existing rows")
    for t, (cols, rows) in sorted(before.items()):
        new_rows = after.get(t, (None, {}))[1]
        missing = [k for k in rows if k not in new_rows]
        changed = [k for k in rows if k in new_rows and new_rows[k] != rows[k]]
        state = "unchanged" if not missing and not changed else f"{len(missing)} missing, {len(changed)} changed"
        if missing or changed:
            problems.append(t)
        print(f"{t:22} {len(rows):12} {len(new_rows):12}  {state}")
    print(f"integrity_check: {integrity}; foreign key violations: {len(fk)}")
    if integrity != "ok" or fk:
        problems.append("integrity")
    print("RESULT: OK, all existing rows are unchanged." if not problems else f"RESULT: PROBLEM in {', '.join(problems)}")
    copy.unlink()
    for p in tmp.glob("*"):
        p.unlink()
    tmp.rmdir()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
