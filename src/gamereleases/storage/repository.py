"""Typed data access for guild settings, panels and reminders."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gamereleases.domain.models import (
    PANEL_COUNT_MAX,
    PANEL_COUNT_MIN,
    GuildConfig,
    PanelMessage,
    PlatformPreset,
    Reminder,
)
from gamereleases.storage.db import Database, transaction

# Column names are interpolated into SQL, so only these constants may be used.
_COL_PANEL_CHANNEL = "panel_channel_id"
_COL_ALERT_CHANNEL = "alert_channel_id"
_COL_ALERT_ROLE = "alert_role_id"
_COL_PANEL_COUNT = "panel_count"
_COL_PRESET = "platform_preset"
_COL_TIMEZONE = "timezone"


@dataclass(frozen=True, slots=True)
class StorageStats:
    """Row counts shown by the status command."""

    guilds: int
    panel_messages: int
    notified_games: int
    pending_reminders: int


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))


def _guild_from_row(row: sqlite3.Row) -> GuildConfig:
    return GuildConfig(
        guild_id=int(row["guild_id"]),
        panel_channel_id=_optional_int(row["panel_channel_id"]),
        alert_channel_id=_optional_int(row["alert_channel_id"]),
        alert_role_id=_optional_int(row["alert_role_id"]),
        panel_count=int(row["panel_count"]),
        platform_preset=PlatformPreset(row["platform_preset"]),
        min_hypes=int(row["min_hypes"]),
        include_dlc=bool(row["include_dlc"]),
        timezone=str(row["timezone"]),
    )


def _panel_from_row(row: sqlite3.Row) -> PanelMessage:
    return PanelMessage(
        guild_id=int(row["guild_id"]),
        slot=int(row["slot_index"]),
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]),
        payload_hash=str(row["payload_hash"]),
    )


def _reminder_from_row(row: sqlite3.Row) -> Reminder:
    return Reminder(
        user_id=int(row["user_id"]),
        game_id=int(row["game_id"]),
        guild_id=_optional_int(row["guild_id"]),
        channel_id=_optional_int(row["channel_id"]),
        sent=bool(row["sent"]),
    )


class Repository:
    """Async facade over the SQLite tables.

    Every method runs its SQL on a worker thread, so none of them block the
    event loop. Writes are atomic.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- guild configuration -------------------------------------------------

    async def get_guild(self, guild_id: int) -> GuildConfig | None:
        """Return the stored configuration, or ``None`` for an unknown guild."""

        def work(conn: sqlite3.Connection) -> GuildConfig | None:
            row = conn.execute(
                "SELECT * FROM guilds WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            return None if row is None else _guild_from_row(row)

        return await self._db.run(work)

    async def upsert_guild(self, config: GuildConfig) -> None:
        """Insert the guild, or overwrite every setting of an existing one."""

        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO guilds (
                        guild_id, panel_channel_id, alert_channel_id,
                        alert_role_id, panel_count, platform_preset,
                        min_hypes, include_dlc, timezone
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id) DO UPDATE SET
                        panel_channel_id = excluded.panel_channel_id,
                        alert_channel_id = excluded.alert_channel_id,
                        alert_role_id    = excluded.alert_role_id,
                        panel_count      = excluded.panel_count,
                        platform_preset  = excluded.platform_preset,
                        min_hypes        = excluded.min_hypes,
                        include_dlc      = excluded.include_dlc,
                        timezone         = excluded.timezone
                    """,
                    (
                        config.guild_id,
                        config.panel_channel_id,
                        config.alert_channel_id,
                        config.alert_role_id,
                        config.panel_count,
                        config.platform_preset.value,
                        config.min_hypes,
                        int(config.include_dlc),
                        config.timezone,
                    ),
                )

        await self._db.run(work)

    async def _set_guild_column(
        self, guild_id: int, column: str, value: int | str | None
    ) -> None:
        """Set one setting, creating the guild with defaults if it is new."""

        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    f"INSERT INTO guilds (guild_id, {column}) VALUES (?, ?) "
                    "ON CONFLICT (guild_id) DO UPDATE "
                    f"SET {column} = excluded.{column}",
                    (guild_id, value),
                )

        await self._db.run(work)

    async def set_panel_channel(self, guild_id: int, channel_id: int) -> None:
        await self._set_guild_column(guild_id, _COL_PANEL_CHANNEL, channel_id)

    async def update_platform_preset(
        self, guild_id: int, preset: PlatformPreset
    ) -> None:
        await self._set_guild_column(guild_id, _COL_PRESET, preset.value)

    async def update_panel_count(self, guild_id: int, panel_count: int) -> None:
        if not PANEL_COUNT_MIN <= panel_count <= PANEL_COUNT_MAX:
            raise ValueError(
                f"panel_count must be between {PANEL_COUNT_MIN} and "
                f"{PANEL_COUNT_MAX}, got {panel_count}"
            )
        await self._set_guild_column(guild_id, _COL_PANEL_COUNT, panel_count)

    async def update_alert_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self._set_guild_column(guild_id, _COL_ALERT_CHANNEL, channel_id)

    async def update_alert_role(self, guild_id: int, role_id: int | None) -> None:
        await self._set_guild_column(guild_id, _COL_ALERT_ROLE, role_id)

    async def update_timezone(self, guild_id: int, timezone: str) -> None:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone: {timezone!r}") from exc
        await self._set_guild_column(guild_id, _COL_TIMEZONE, timezone)

    # -- panel messages ------------------------------------------------------

    async def get_panel_messages(self, guild_id: int) -> list[PanelMessage]:
        """Return the guild's tracked messages ordered by slot."""

        def work(conn: sqlite3.Connection) -> list[PanelMessage]:
            rows = conn.execute(
                "SELECT * FROM panel_messages WHERE guild_id = ? ORDER BY slot_index",
                (guild_id,),
            ).fetchall()
            return [_panel_from_row(row) for row in rows]

        return await self._db.run(work)

    async def upsert_panel_message(
        self,
        guild_id: int,
        slot: int,
        channel_id: int,
        message_id: int,
        payload_hash: str,
    ) -> None:
        """Track a message for a slot. The guild must already exist."""

        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO panel_messages (
                        guild_id, slot_index, channel_id, message_id, payload_hash
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, slot_index) DO UPDATE SET
                        channel_id   = excluded.channel_id,
                        message_id   = excluded.message_id,
                        payload_hash = excluded.payload_hash
                    """,
                    (guild_id, slot, channel_id, message_id, payload_hash),
                )

        await self._db.run(work)

    async def delete_panel_slot(self, guild_id: int, slot: int) -> None:
        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    "DELETE FROM panel_messages WHERE guild_id = ? AND slot_index = ?",
                    (guild_id, slot),
                )

        await self._db.run(work)

    async def clear_panel_messages(self, guild_id: int) -> None:
        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    "DELETE FROM panel_messages WHERE guild_id = ?", (guild_id,)
                )

        await self._db.run(work)

    # -- release-day alerts --------------------------------------------------

    async def is_notified(self, guild_id: int, game_id: int) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT 1 FROM notified_games WHERE guild_id = ? AND game_id = ?",
                (guild_id, game_id),
            ).fetchone()
            return row is not None

        return await self._db.run(work)

    async def mark_notified(self, guild_id: int, game_id: int) -> bool:
        """Record an alert; return ``False`` if it had already been recorded."""

        def work(conn: sqlite3.Connection) -> bool:
            with transaction(conn):
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO notified_games (guild_id, game_id) "
                    "VALUES (?, ?)",
                    (guild_id, game_id),
                )
                return cursor.rowcount == 1

        return await self._db.run(work)

    # -- personal reminders --------------------------------------------------

    async def toggle_reminder(
        self,
        user_id: int,
        game_id: int,
        guild_id: int | None,
        channel_id: int | None,
    ) -> bool:
        """Subscribe or unsubscribe a user; return ``True`` if now subscribed.

        A reminder that has already been delivered counts as inactive, so
        toggling it subscribes the user again.
        """

        def work(conn: sqlite3.Connection) -> bool:
            with transaction(conn):
                row = conn.execute(
                    "SELECT sent FROM game_reminders WHERE user_id = ? AND game_id = ?",
                    (user_id, game_id),
                ).fetchone()
                if row is not None and not row["sent"]:
                    conn.execute(
                        "DELETE FROM game_reminders WHERE user_id = ? AND game_id = ?",
                        (user_id, game_id),
                    )
                    return False
                conn.execute(
                    """
                    INSERT INTO game_reminders (user_id, game_id, guild_id, channel_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (user_id, game_id) DO UPDATE SET
                        guild_id   = excluded.guild_id,
                        channel_id = excluded.channel_id,
                        sent       = 0
                    """,
                    (user_id, game_id, guild_id, channel_id),
                )
                return True

        return await self._db.run(work)

    async def get_pending_reminder_game_ids(self) -> list[int]:
        """Return the distinct IDs of games that still have undelivered reminders."""

        def work(conn: sqlite3.Connection) -> list[int]:
            rows = conn.execute(
                "SELECT DISTINCT game_id FROM game_reminders "
                "WHERE sent = 0 ORDER BY game_id"
            ).fetchall()
            return [int(row["game_id"]) for row in rows]

        return await self._db.run(work)

    async def get_due_reminders(
        self, released_game_ids: Collection[int]
    ) -> list[Reminder]:
        """Return the undelivered reminders for games that are now out."""
        game_ids = sorted(set(released_game_ids))
        if not game_ids:
            return []

        def work(conn: sqlite3.Connection) -> list[Reminder]:
            placeholders = ", ".join("?" * len(game_ids))
            rows = conn.execute(
                "SELECT * FROM game_reminders "
                f"WHERE sent = 0 AND game_id IN ({placeholders}) "
                "ORDER BY created_at, user_id, game_id",
                game_ids,
            ).fetchall()
            return [_reminder_from_row(row) for row in rows]

        return await self._db.run(work)

    async def mark_reminder_sent(self, user_id: int, game_id: int) -> None:
        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    "UPDATE game_reminders SET sent = 1 "
                    "WHERE user_id = ? AND game_id = ?",
                    (user_id, game_id),
                )

        await self._db.run(work)

    # -- diagnostics ---------------------------------------------------------

    async def count_stats(self) -> StorageStats:
        def work(conn: sqlite3.Connection) -> StorageStats:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM guilds),
                    (SELECT COUNT(*) FROM panel_messages),
                    (SELECT COUNT(*) FROM notified_games),
                    (SELECT COUNT(*) FROM game_reminders WHERE sent = 0)
                """
            ).fetchone()
            return StorageStats(
                guilds=int(row[0]),
                panel_messages=int(row[1]),
                notified_games=int(row[2]),
                pending_reminders=int(row[3]),
            )

        return await self._db.run(work)
