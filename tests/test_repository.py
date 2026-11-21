"""Tests for the SQLite repository."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from gamereleases.domain.models import (
    OVERVIEW_SLOT,
    GuildConfig,
    PlatformPreset,
    Reminder,
)
from gamereleases.storage.db import (
    MIGRATIONS,
    Database,
    Migration,
    apply_migrations,
    connect,
    current_version,
)
from gamereleases.storage.repository import Repository, StorageStats

GUILD = 100
OTHER_GUILD = 200


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "nested" / "dir" / "test.db")
    yield database
    await database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    return Repository(db)


# -- database setup ----------------------------------------------------------


async def test_open_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c" / "releases.db"
    database = await Database.open(path)
    try:
        assert path.exists()
    finally:
        await database.close()


async def test_connection_pragmas(db: Database) -> None:
    def read(conn: sqlite3.Connection) -> tuple[str, int, int]:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fks = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        return str(mode), int(fks), int(busy)

    assert await db.run(read) == ("wal", 1, 5000)


async def test_migrations_create_expected_tables(db: Database) -> None:
    def tables(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {str(row[0]) for row in rows}

    assert {
        "schema_migrations",
        "guilds",
        "panel_messages",
        "notified_games",
        "game_reminders",
    } <= await db.run(tables)


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    try:
        latest = max(m.version for m in MIGRATIONS)
        assert current_version(conn) == 0
        assert apply_migrations(conn) == latest
        assert apply_migrations(conn) == latest
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == len(MIGRATIONS)
    finally:
        conn.close()


def test_pending_migrations_apply_in_order(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    try:
        apply_migrations(conn)
        extra = Migration(
            version=max(m.version for m in MIGRATIONS) + 1,
            name="add column",
            statements=("ALTER TABLE guilds ADD COLUMN note TEXT",),
        )
        assert apply_migrations(conn, (*MIGRATIONS, extra)) == extra.version
        conn.execute("SELECT note FROM guilds")
    finally:
        conn.close()


def test_failed_migration_is_rolled_back(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    try:
        apply_migrations(conn)
        broken = Migration(
            version=max(m.version for m in MIGRATIONS) + 1,
            name="broken",
            statements=(
                "CREATE TABLE half_done (id INTEGER)",
                "THIS IS NOT SQL",
            ),
        )
        with pytest.raises(sqlite3.Error):
            apply_migrations(conn, (*MIGRATIONS, broken))
        assert current_version(conn) == max(m.version for m in MIGRATIONS)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "half_done" not in tables
    finally:
        conn.close()


def test_newer_database_is_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    try:
        apply_migrations(conn)
        future = max(m.version for m in MIGRATIONS) + 1
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, 'future')",
            (future,),
        )
        with pytest.raises(RuntimeError, match="newer"):
            apply_migrations(conn)
    finally:
        conn.close()


async def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    first = await Database.open(path)
    await Repository(first).update_panel_count(GUILD, 7)
    await first.close()

    second = await Database.open(path)
    try:
        guild = await Repository(second).get_guild(GUILD)
    finally:
        await second.close()
    assert guild is not None
    assert guild.panel_count == 7


# -- guild configuration -----------------------------------------------------


async def test_unknown_guild_is_none(repo: Repository) -> None:
    assert await repo.get_guild(GUILD) is None


async def test_upsert_guild_inserts_then_overwrites(repo: Repository) -> None:
    config = GuildConfig(
        guild_id=GUILD,
        panel_channel_id=1,
        alert_channel_id=2,
        alert_role_id=3,
        panel_count=8,
        platform_preset=PlatformPreset.PC_ONLY,
        min_hypes=4,
        include_dlc=True,
        timezone="America/New_York",
    )
    await repo.upsert_guild(config)
    assert await repo.get_guild(GUILD) == config

    changed = GuildConfig(guild_id=GUILD, panel_count=2)
    await repo.upsert_guild(changed)
    assert await repo.get_guild(GUILD) == changed


async def test_setters_create_guild_with_defaults(repo: Repository) -> None:
    await repo.set_panel_channel(GUILD, 555)
    assert await repo.get_guild(GUILD) == GuildConfig(
        guild_id=GUILD, panel_channel_id=555
    )


async def test_setters_update_only_their_field(repo: Repository) -> None:
    await repo.set_panel_channel(GUILD, 10)
    await repo.update_platform_preset(GUILD, PlatformPreset.XBOX)
    await repo.update_panel_count(GUILD, 3)
    await repo.update_alert_channel(GUILD, 20)
    await repo.update_alert_role(GUILD, 30)
    await repo.update_timezone(GUILD, "Asia/Tokyo")

    assert await repo.get_guild(GUILD) == GuildConfig(
        guild_id=GUILD,
        panel_channel_id=10,
        alert_channel_id=20,
        alert_role_id=30,
        panel_count=3,
        platform_preset=PlatformPreset.XBOX,
        timezone="Asia/Tokyo",
    )

    await repo.set_panel_channel(GUILD, 11)
    guild = await repo.get_guild(GUILD)
    assert guild is not None
    assert guild.panel_channel_id == 11
    assert guild.panel_count == 3


async def test_alert_settings_can_be_cleared(repo: Repository) -> None:
    await repo.update_alert_channel(GUILD, 20)
    await repo.update_alert_role(GUILD, 30)
    await repo.update_alert_channel(GUILD, None)
    await repo.update_alert_role(GUILD, None)

    guild = await repo.get_guild(GUILD)
    assert guild is not None
    assert guild.alert_channel_id is None
    assert guild.alert_role_id is None


@pytest.mark.parametrize("count", [0, 11, -1])
async def test_panel_count_out_of_range_is_rejected(
    repo: Repository, count: int
) -> None:
    with pytest.raises(ValueError, match="panel_count"):
        await repo.update_panel_count(GUILD, count)
    assert await repo.get_guild(GUILD) is None


async def test_unknown_timezone_is_rejected(repo: Repository) -> None:
    with pytest.raises(ValueError, match="time zone"):
        await repo.update_timezone(GUILD, "Mars/Olympus_Mons")
    assert await repo.get_guild(GUILD) is None


async def test_large_snowflake_ids_round_trip(repo: Repository) -> None:
    snowflake = 1_234_567_890_123_456_789
    await repo.set_panel_channel(snowflake, snowflake - 1)
    guild = await repo.get_guild(snowflake)
    assert guild is not None
    assert guild.panel_channel_id == snowflake - 1


# -- panel messages ----------------------------------------------------------


async def test_panel_messages_are_returned_ordered_by_slot(repo: Repository) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))
    await repo.upsert_panel_message(GUILD, 1, 5, 51, "h1")
    await repo.upsert_panel_message(GUILD, OVERVIEW_SLOT, 5, 50, "h-1")
    await repo.upsert_panel_message(GUILD, 0, 5, 52, "h0")

    messages = await repo.get_panel_messages(GUILD)

    assert [m.slot for m in messages] == [OVERVIEW_SLOT, 0, 1]
    assert [m.message_id for m in messages] == [50, 52, 51]
    assert all(m.guild_id == GUILD and m.channel_id == 5 for m in messages)


async def test_upsert_panel_message_replaces_existing_slot(repo: Repository) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))
    await repo.upsert_panel_message(GUILD, 0, 5, 51, "old")
    await repo.upsert_panel_message(GUILD, 0, 6, 99, "new")

    (message,) = await repo.get_panel_messages(GUILD)

    assert (message.channel_id, message.message_id, message.payload_hash) == (
        6,
        99,
        "new",
    )


async def test_delete_panel_slot_removes_only_that_slot(repo: Repository) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))
    await repo.upsert_panel_message(GUILD, 0, 5, 1, "a")
    await repo.upsert_panel_message(GUILD, 1, 5, 2, "b")

    await repo.delete_panel_slot(GUILD, 0)
    await repo.delete_panel_slot(GUILD, 42)

    assert [m.slot for m in await repo.get_panel_messages(GUILD)] == [1]


async def test_clear_panel_messages_is_scoped_to_guild(repo: Repository) -> None:
    for guild in (GUILD, OTHER_GUILD):
        await repo.upsert_guild(GuildConfig(guild_id=guild))
        await repo.upsert_panel_message(guild, 0, 5, 1, "a")

    await repo.clear_panel_messages(GUILD)

    assert await repo.get_panel_messages(GUILD) == []
    assert len(await repo.get_panel_messages(OTHER_GUILD)) == 1


async def test_panel_message_requires_a_known_guild(repo: Repository) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await repo.upsert_panel_message(GUILD, 0, 5, 1, "a")


# -- release-day alerts ------------------------------------------------------


async def test_mark_notified_is_recorded_once(repo: Repository) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))

    assert not await repo.is_notified(GUILD, 7)
    assert await repo.mark_notified(GUILD, 7) is True
    assert await repo.is_notified(GUILD, 7)
    assert await repo.mark_notified(GUILD, 7) is False


async def test_notifications_are_per_guild_and_game(repo: Repository) -> None:
    for guild in (GUILD, OTHER_GUILD):
        await repo.upsert_guild(GuildConfig(guild_id=guild))
    await repo.mark_notified(GUILD, 7)

    assert not await repo.is_notified(OTHER_GUILD, 7)
    assert not await repo.is_notified(GUILD, 8)


async def test_concurrent_mark_notified_has_a_single_winner(repo: Repository) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))

    results = await asyncio.gather(*(repo.mark_notified(GUILD, 7) for _ in range(10)))

    assert results.count(True) == 1


async def test_deleting_a_guild_cascades(db: Database, repo: Repository) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))
    await repo.upsert_panel_message(GUILD, 0, 5, 1, "a")
    await repo.mark_notified(GUILD, 7)

    def delete(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM guilds WHERE guild_id = ?", (GUILD,))

    await db.run(delete)

    assert await repo.get_panel_messages(GUILD) == []
    assert not await repo.is_notified(GUILD, 7)


# -- reminders ---------------------------------------------------------------


async def test_toggle_reminder_subscribes_then_unsubscribes(repo: Repository) -> None:
    assert await repo.toggle_reminder(1, 50, GUILD, 5) is True
    assert await repo.get_due_reminders([50]) == [
        Reminder(user_id=1, game_id=50, guild_id=GUILD, channel_id=5)
    ]

    assert await repo.toggle_reminder(1, 50, GUILD, 5) is False
    assert await repo.get_due_reminders([50]) == []

    assert await repo.toggle_reminder(1, 50, GUILD, 5) is True


async def test_reminders_do_not_need_a_configured_guild(repo: Repository) -> None:
    assert await repo.toggle_reminder(1, 50, None, None) is True
    assert await repo.get_due_reminders([50]) == [
        Reminder(user_id=1, game_id=50, guild_id=None, channel_id=None)
    ]


async def test_reminders_are_independent_per_user_and_game(repo: Repository) -> None:
    await repo.toggle_reminder(1, 50, GUILD, 5)
    await repo.toggle_reminder(2, 50, GUILD, 5)
    await repo.toggle_reminder(1, 51, GUILD, 5)

    await repo.toggle_reminder(1, 50, GUILD, 5)

    due = await repo.get_due_reminders([50, 51])
    assert {(r.user_id, r.game_id) for r in due} == {(2, 50), (1, 51)}


async def test_get_due_reminders_filters_by_game(repo: Repository) -> None:
    await repo.toggle_reminder(1, 50, GUILD, 5)
    await repo.toggle_reminder(1, 51, GUILD, 5)

    assert await repo.get_due_reminders([]) == []
    assert await repo.get_due_reminders([99]) == []
    assert [r.game_id for r in await repo.get_due_reminders([51, 51])] == [51]


async def test_sent_reminders_are_not_due_again(repo: Repository) -> None:
    await repo.toggle_reminder(1, 50, GUILD, 5)

    await repo.mark_reminder_sent(1, 50)

    assert await repo.get_due_reminders([50]) == []


async def test_toggling_a_delivered_reminder_resubscribes(repo: Repository) -> None:
    await repo.toggle_reminder(1, 50, GUILD, 5)
    await repo.mark_reminder_sent(1, 50)

    assert await repo.toggle_reminder(1, 50, GUILD, 6) is True

    assert await repo.get_due_reminders([50]) == [
        Reminder(user_id=1, game_id=50, guild_id=GUILD, channel_id=6)
    ]


async def test_mark_reminder_sent_for_unknown_reminder_is_a_noop(
    repo: Repository,
) -> None:
    await repo.mark_reminder_sent(1, 50)

    assert (await repo.count_stats()).pending_reminders == 0


# -- stats -------------------------------------------------------------------


async def test_count_stats_on_empty_database(repo: Repository) -> None:
    assert await repo.count_stats() == StorageStats(0, 0, 0, 0)


async def test_count_stats_counts_every_table(repo: Repository) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))
    await repo.upsert_guild(GuildConfig(guild_id=OTHER_GUILD))
    await repo.upsert_panel_message(GUILD, OVERVIEW_SLOT, 5, 1, "a")
    await repo.upsert_panel_message(GUILD, 0, 5, 2, "b")
    await repo.mark_notified(GUILD, 7)
    await repo.toggle_reminder(1, 50, GUILD, 5)
    await repo.toggle_reminder(2, 50, GUILD, 5)
    await repo.mark_reminder_sent(2, 50)

    assert await repo.count_stats() == StorageStats(
        guilds=2, panel_messages=2, notified_games=1, pending_reminders=1
    )


async def test_pending_reminder_game_ids_are_distinct_and_unsent(
    repo: Repository,
) -> None:
    assert await repo.get_pending_reminder_game_ids() == []

    await repo.toggle_reminder(1, 70, GUILD, 5)
    await repo.toggle_reminder(2, 70, GUILD, 5)
    await repo.toggle_reminder(1, 60, GUILD, 5)
    await repo.toggle_reminder(1, 80, GUILD, 5)
    await repo.mark_reminder_sent(1, 80)

    assert await repo.get_pending_reminder_game_ids() == [60, 70]
