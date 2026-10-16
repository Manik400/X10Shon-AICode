"""SQLite access with migrations and transactions.

One ``Database`` per process. SQLite serializes writers anyway, so a single ``aiosqlite``
connection guarded by a lock for write transactions is both simplest and fastest for a
single-user local service. Reads outside a transaction go through the same connection; WAL mode
keeps them cheap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from ..core.errors import DatabaseError

log = logging.getLogger("lfa.db")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for p in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return out


def _restrict_permissions(path: Path) -> None:
    """Owner-only permissions where the platform honours mode bits (POSIX). On Windows the
    file inherits the user's profile ACL, which is already private to the user."""
    try:
        if os.name == "posix":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - best effort
        pass


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._in_tx = False

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if self._conn is not None:
            return
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                try:
                    os.chmod(self.path.parent, stat.S_IRWXU)
                except OSError:  # pragma: no cover
                    pass
        self._conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        if isinstance(self.path, Path) and self.path.exists():
            _restrict_permissions(self.path)

    async def checkpoint(self, *, truncate: bool = False) -> None:
        """Fold the WAL into the main file so an unclean process kill can never cost more than the
        last few seconds of writes. PASSIVE never blocks on readers; TRUNCATE is used on close."""
        if self._conn is None:
            return
        try:
            async with self._write_lock:
                await self.conn.execute(f"PRAGMA wal_checkpoint({'TRUNCATE' if truncate else 'PASSIVE'})")
        except aiosqlite.Error as exc:  # pragma: no cover - best effort
            log.warning("wal checkpoint failed: %s", exc)

    async def close(self) -> None:
        if self._conn is not None:
            await self.checkpoint(truncate=True)
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("database is not connected")
        return self._conn

    # ------------------------------------------------------------------ migrations
    async def migrate(self, directory: Path = MIGRATIONS_DIR) -> list[int]:
        await self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {r["version"] for r in await self.fetch_all("SELECT version FROM schema_migrations")}
        newly: list[int] = []
        for version, path in list_migrations(directory):
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            # sqlite3.executescript() commits any pending transaction before running, so the
            # script carries its own BEGIN/COMMIT to stay atomic together with the version row.
            script = "\n".join(
                [
                    "BEGIN;",
                    sql,
                    f"INSERT INTO schema_migrations(version, name, applied_at) VALUES ({version}, '{path.name}', strftime('%Y-%m-%dT%H:%M:%fZ','now'));",
                    "COMMIT;",
                ]
            )
            async with self._write_lock:
                try:
                    await self.conn.executescript(script)
                except aiosqlite.Error as exc:
                    try:
                        await self.conn.execute("ROLLBACK")
                    except aiosqlite.Error:
                        pass
                    raise DatabaseError(f"migration {path.name} failed: {exc}") from exc
            newly.append(version)
            log.info("applied migration %s", path.name)
        return newly

    async def schema_version(self) -> int:
        row = await self.fetch_one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"] or 0) if row else 0

    # ------------------------------------------------------------------ transactions
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Serialized write transaction. Nested use re-enters the outer transaction."""
        if self._in_tx:
            yield
            return
        async with self._write_lock:
            self._in_tx = True
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                try:
                    yield
                except BaseException:
                    await self.conn.execute("ROLLBACK")
                    raise
                else:
                    await self.conn.execute("COMMIT")
            except aiosqlite.Error as exc:
                raise DatabaseError(str(exc)) from exc
            finally:
                self._in_tx = False

    # ------------------------------------------------------------------ helpers
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        try:
            cur = await self.conn.execute(sql, tuple(params))
            await cur.close()
            return cur.rowcount
        except aiosqlite.Error as exc:
            raise DatabaseError(str(exc)) from exc

    async def execute_returning_id(self, sql: str, params: Iterable[Any] = ()) -> int:
        try:
            cur = await self.conn.execute(sql, tuple(params))
            rowid = cur.lastrowid
            await cur.close()
            return int(rowid or 0)
        except aiosqlite.Error as exc:
            raise DatabaseError(str(exc)) from exc

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        try:
            cur = await self.conn.execute(sql, tuple(params))
            row = await cur.fetchone()
            await cur.close()
            return row
        except aiosqlite.Error as exc:
            raise DatabaseError(str(exc)) from exc

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        try:
            cur = await self.conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
            await cur.close()
            return list(rows)
        except aiosqlite.Error as exc:
            raise DatabaseError(str(exc)) from exc

    async def fetch_value(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = await self.fetch_one(sql, params)
        return None if row is None else row[0]
