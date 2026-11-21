"""SQLite connection management and schema migrations."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True, slots=True)
class Migration:
    """A schema change applied exactly once, in ascending ``version`` order."""

    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial schema",
        statements=(
            """
            CREATE TABLE guilds (
                guild_id         INTEGER PRIMARY KEY,
                panel_channel_id INTEGER,
                alert_channel_id INTEGER,
                alert_role_id    INTEGER,
                panel_count      INTEGER NOT NULL DEFAULT 5
                                 CHECK (panel_count BETWEEN 1 AND 10),
                platform_preset  TEXT NOT NULL DEFAULT 'all',
                min_hypes        INTEGER NOT NULL DEFAULT 0 CHECK (min_hypes >= 0),
                include_dlc      INTEGER NOT NULL DEFAULT 0
                                 CHECK (include_dlc IN (0, 1)),
                timezone         TEXT NOT NULL DEFAULT 'Europe/Rome'
            )
            """,
            """
            CREATE TABLE panel_messages (
                guild_id     INTEGER NOT NULL
                             REFERENCES guilds (guild_id) ON DELETE CASCADE,
                slot_index   INTEGER NOT NULL,
                channel_id   INTEGER NOT NULL,
                message_id   INTEGER NOT NULL,
                payload_hash TEXT NOT NULL,
                PRIMARY KEY (guild_id, slot_index)
            )
            """,
            """
            CREATE TABLE notified_games (
                guild_id    INTEGER NOT NULL
                            REFERENCES guilds (guild_id) ON DELETE CASCADE,
                game_id     INTEGER NOT NULL,
                notified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, game_id)
            )
            """,
            """
            CREATE TABLE game_reminders (
                user_id    INTEGER NOT NULL,
                game_id    INTEGER NOT NULL,
                guild_id   INTEGER,
                channel_id INTEGER,
                sent       INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, game_id)
            )
            """,
            """
            CREATE INDEX idx_game_reminders_pending
                ON game_reminders (game_id) WHERE sent = 0
            """,
        ),
    ),
)


def connect(path: Path) -> sqlite3.Connection:
    """Open ``path`` (creating its parent directory) with the standard pragmas.

    The connection is in autocommit mode; use :func:`transaction` for writes.
    It may be handed between threads, so callers must serialize access.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """Run the enclosed statements atomically, taking the write lock up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or ``0`` for a new file."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> int:
    """Apply every pending migration and return the resulting schema version."""
    with transaction(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    applied = current_version(conn)
    latest = max((m.version for m in migrations), default=0)
    if applied > latest:
        raise RuntimeError(
            f"database schema version {applied} is newer than the "
            f"supported version {latest}"
        )

    for migration in sorted(migrations, key=lambda m: m.version):
        if migration.version <= applied:
            continue
        with transaction(conn):
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        logger.info(
            "Applied database migration %d (%s)", migration.version, migration.name
        )
    return max(applied, latest)


class Database:
    """A shared SQLite connection whose work runs off the event loop."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    @classmethod
    async def open(cls, path: Path) -> Database:
        """Create the database file if needed, migrate it and return a handle."""

        def setup() -> sqlite3.Connection:
            conn = connect(path)
            try:
                apply_migrations(conn)
            except BaseException:
                conn.close()
                raise
            return conn

        return cls(await asyncio.to_thread(setup))

    async def run[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` with the connection on a worker thread."""

        def locked() -> T:
            with self._lock:
                return work(self._conn)

        return await asyncio.to_thread(locked)

    async def close(self) -> None:
        """Close the connection once any in-flight work has finished."""

        def shutdown() -> None:
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(shutdown)
