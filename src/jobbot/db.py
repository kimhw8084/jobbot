from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import ConfigBundle
from .migrations import all_migrations


@dataclass(frozen=True)
class MigrationResult:
    before: tuple[int, ...]
    applied: tuple[int, ...]
    integrity_before: str
    integrity_after: str
    backup_path: Path | None


def configure_connection(conn: sqlite3.Connection, *, busy_timeout_ms: int = 10000, synchronous: str = "FULL") -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={max(1, int(busy_timeout_ms))}")
    conn.execute("PRAGMA journal_mode=WAL")
    if synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        raise ValueError(f"invalid sqlite synchronous setting: {synchronous}")
    conn.execute(f"PRAGMA synchronous={synchronous}")


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
      version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
    )""")
    conn.commit()


def current_versions(conn: sqlite3.Connection) -> tuple[int, ...]:
    ensure_migration_table(conn)
    return tuple(int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))


def apply_pending(conn: sqlite3.Connection) -> tuple[int, ...]:
    present = set(current_versions(conn))
    applied: list[int] = []
    for migration in all_migrations():
        if migration.VERSION in present:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration.upgrade(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                (migration.VERSION, migration.NAME, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(int(migration.VERSION))
    return tuple(applied)


class Database:
    def __init__(self, bundle: ConfigBundle):
        self.bundle = bundle
        self.path = bundle.database_path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        runtime = self.bundle.runtime["runtime"]
        configure_connection(conn, busy_timeout_ms=int(runtime["sqlite_busy_timeout_ms"]), synchronous=str(runtime["sqlite_synchronous"]))
        return conn

    def backup(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            check = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
            if check != "ok":
                raise RuntimeError(f"backup integrity_check failed: {check}")
        finally:
            destination.close()
            source.close()
        return target

    def migrate(self, *, backup_existing: bool = True) -> MigrationResult:
        existed = self.path.exists() and self.path.stat().st_size > 0
        conn = self.connect()
        backup_path: Path | None = None
        try:
            before_check = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if before_check != "ok":
                raise RuntimeError(f"database integrity_check failed before migration: {before_check}")
            before = current_versions(conn)
            pending = [m.VERSION for m in all_migrations() if m.VERSION not in set(before)]
            if existed and pending and backup_existing:
                conn.close()
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                directory = (self.bundle.root / self.bundle.runtime["ledger"]["backup_dir"]).resolve()
                backup_path = self.backup(directory / f"jobs_pre_v3_2_migration_{stamp}.sqlite3")
                conn = self.connect()
            applied = apply_pending(conn)
            after_check = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if after_check != "ok":
                raise RuntimeError(f"database integrity_check failed after migration: {after_check}")
            return MigrationResult(before, applied, before_check, after_check, backup_path)
        finally:
            conn.close()

    def integrity_check(self) -> str:
        conn = self.connect()
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
