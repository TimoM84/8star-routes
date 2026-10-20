"""SQLite access, migrations and consistent backups."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("routes.db")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class Database:
    """One connection per thread; explicit transactions via `tx()`."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.path)
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextmanager
    def tx(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        # IMMEDIATE by default: a deferred transaction that later writes can fail with
        # "database is locked" under concurrency instead of waiting for busy_timeout.
        c = self.conn
        c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Any = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    # --- settings -----------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        v = self.scalar("SELECT value_json FROM settings WHERE key = ?", (key,))
        return default if v is None else json.loads(v)

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value_json, updated_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_utc = excluded.updated_utc",
            (key, json.dumps(value), iso(utcnow())),
        )

    def event(self, level: str, kind: str, message: str) -> None:
        self.execute("INSERT INTO events(ts_utc, level, kind, message) VALUES (?, ?, ?, ?)", (iso(utcnow()), level, kind, message))
        getattr(log, "warning" if level == "warning" else "info")("%s: %s", kind, message)


def applied_migrations(conn: sqlite3.Connection) -> set[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_utc TEXT NOT NULL)")
    return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}


def migrate(db: Database, backup_dir: Path | None = None) -> list[str]:
    """Apply pending migrations in order, each in its own transaction.

    A consistent backup is written first when the database already holds data.
    """
    conn = db.conn
    done = applied_migrations(conn)
    pending = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.stem not in done)
    if not pending:
        return []
    if done and backup_dir is not None:
        backup(db, backup_dir, prefix="pre-migration")
    applied = []
    for p in pending:
        sql = p.read_text(encoding="utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would commit implicitly; split on statement terminators instead.
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(name, applied_utc) VALUES (?, ?)", (p.stem, iso(utcnow())))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(p.stem)
        log.info("applied migration %s", p.stem)
    return applied


def _split_sql(sql: str) -> list[str]:
    stmts, buf = [], []
    for raw in sql.splitlines():
        ln = raw
        i = ln.find("--")
        while i != -1:
            if ln[:i].count("'") % 2 == 0:  # not inside a string literal
                ln = ln[:i]
                break
            i = ln.find("--", i + 2)
        ln = ln.rstrip()
        if not ln:
            continue
        buf.append(ln)
        if ln.endswith(";"):
            s = "\n".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def backup(db: Database, backup_dir: Path, prefix: str = "backup", keep: int | None = None) -> Path:
    """Write a transactionally consistent copy using SQLite's online backup API."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{prefix}-{stamp}.sqlite"
    n = 1
    while target.exists():
        target = backup_dir / f"{prefix}-{stamp}-{n}.sqlite"
        n += 1
    dst = sqlite3.connect(str(target))
    try:
        db.conn.backup(dst)
    finally:
        dst.close()
    check = sqlite3.connect(str(target))
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        raise RuntimeError(f"backup integrity check failed: {ok}")
    if keep:
        olds = sorted(backup_dir.glob(f"{prefix}-*.sqlite"))
        for old in olds[:-keep]:
            old.unlink(missing_ok=True)
    return target


def restore(backup_file: Path, db_path: Path) -> None:
    """Replace the database file with a backup. Run only while the app is stopped."""
    src = sqlite3.connect(str(backup_file))
    try:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup file failed integrity check")
        # Backing up *into* the target through SQLite keeps its WAL consistent; no file juggling needed.
        dst = sqlite3.connect(str(db_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
