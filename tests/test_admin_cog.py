"""Tests for the server management commands."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from gamereleases.bot.app import GameReleasesBot
from gamereleases.bot.cogs.admin import (
    INVALID_TIMEZONE,
    NO_PANEL_CHANNEL,
    NO_PERMISSION,
    AdminCog,
    parse_timezone,
    setup,
)
from gamereleases.bot.reconciler import (
    MessageForbidden,
    MessageNotFound,
    PanelReconciler,
)
from gamereleases.bot.sync import SyncReport
from gamereleases.config import Settings
from gamereleases.domain.formatting import EmbedData
from gamereleases.domain.models import GuildConfig, PlatformPreset
from gamereleases.storage.db import Database
from gamereleases.storage.repository import Repository

GUILD = 100
PANEL_CHANNEL = 10
ALERT_CHANNEL = 11
ROLE = 555
USER = 7
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)

ALL_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
)


async def invoke(command: object, *args: Any, **kwargs: Any) -> None:
    """Run a slash command's callback directly, as the library would."""
    await cast(Any, command).callback(*args, **kwargs)


def report(**fields: Any) -> SyncReport:
    return SyncReport(finished_at=NOW, **fields)


class FakeMessenger:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []
        self.missing: set[int] = set()
        self.forbidden: set[int] = set()
        self._next_id = 1000

    async def send(self, channel_id: int, payload: EmbedData) -> int:
        self._next_id += 1
        return self._next_id

    async def edit(self, channel_id: int, message_id: int, payload: EmbedData) -> None:
        return None

    async def delete(self, channel_id: int, message_id: int) -> None:
        self.deleted.append((channel_id, message_id))
        if message_id in self.missing:
            raise MessageNotFound
        if message_id in self.forbidden:
            raise MessageForbidden


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    db = await Database.open(tmp_path / "admin.db")
    yield Repository(db)
    await db.close()


@pytest.fixture
def messenger() -> FakeMessenger:
    return FakeMessenger()


@pytest.fixture
def bot(repo: Repository, messenger: FakeMessenger) -> SimpleNamespace:
    calls: list[str] = []

    async def sync_guild(guild_id: int, bypass_cache: bool = False) -> SyncReport:
        calls.append("sync_guild")
        return report(guilds_synced=1)

    async def run_sync_cycle(bypass_cache: bool = False) -> SyncReport:
        calls.append("run_sync_cycle")
        return report(guilds_synced=2, messages_changed=5)

    return SimpleNamespace(
        repository=repo,
        reconciler=PanelReconciler(repo, messenger),
        settings=SimpleNamespace(timezone="Europe/Rome"),
        sync_guild=AsyncMock(side_effect=sync_guild),
        run_sync_cycle=AsyncMock(side_effect=run_sync_cycle),
        calls=calls,
        latency=0.0426,
        uptime=timedelta(days=1, hours=2, minutes=3, seconds=4),
        last_sync=None,
        next_sync_at=None,
    )


@pytest.fixture
def cog(bot: SimpleNamespace) -> AdminCog:
    return AdminCog(cast(Any, bot))


def make_interaction(
    *, manage_guild: bool = True, guild_id: int | None = GUILD
) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user.id = USER
    interaction.permissions = discord.Permissions(manage_guild=manage_guild)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def make_channel(
    channel_id: int = PANEL_CHANNEL,
    permissions: discord.Permissions = ALL_PERMISSIONS,
) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.mention = f"<#{channel_id}>"
    channel.guild = MagicMock()
    channel.guild.id = GUILD
    channel.permissions_for.return_value = permissions
    return channel


def make_role(role_id: int = ROLE) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    return role


def without(*names: str) -> discord.Permissions:
    permissions = discord.Permissions(ALL_PERMISSIONS.value)
    for name in names:
        setattr(permissions, name, False)
    return permissions


def followup_text(interaction: MagicMock) -> str:
    interaction.followup.send.assert_awaited_once()
    call = interaction.followup.send.await_args
    assert call.kwargs["ephemeral"] is True
    return call.args[0]


def rejection_text(interaction: MagicMock) -> str:
    interaction.response.send_message.assert_awaited_once()
    call = interaction.response.send_message.await_args
    assert call.kwargs["ephemeral"] is True
    interaction.response.defer.assert_not_awaited()
    return call.args[0]


async def run_config(cog: AdminCog, interaction: MagicMock, **params: Any) -> None:
    defaults: dict[str, Any] = {
        "piattaforme": None,
        "numero_pannelli": None,
        "canale_avvisi": None,
        "ruolo_avvisi": None,
        "azzera_avvisi": False,
        "fuso_orario": None,
    }
    await invoke(cog.config, cog, interaction, **{**defaults, **params})


async def configured_guild(repo: Repository, **fields: Any) -> GuildConfig:
    config = GuildConfig(guild_id=GUILD, panel_channel_id=PANEL_CHANNEL, **fields)
    await repo.upsert_guild(config)
    return config


# -- permission gate -----------------------------------------------------------


async def test_every_command_requires_manage_guild(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    channel = make_channel()
    attempts = {
        "setchannel": lambda i: invoke(cog.setchannel, cog, i, channel),
        "config": lambda i: run_config(cog, i, numero_pannelli=3),
        "checknow": lambda i: invoke(cog.checknow, cog, i, True),
        "status": lambda i: invoke(cog.status, cog, i),
    }
    for attempt in attempts.values():
        interaction = make_interaction(manage_guild=False)

        await attempt(interaction)

        assert rejection_text(interaction) == NO_PERMISSION

    assert await repo.get_guild(GUILD) is None
    assert bot.calls == []


# -- /setchannel ---------------------------------------------------------------


async def test_setchannel_saves_the_channel_and_syncs_the_guild(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    interaction = make_interaction()

    await invoke(cog.setchannel, cog, interaction, make_channel())

    config = await repo.get_guild(GUILD)
    assert config is not None
    assert config.panel_channel_id == PANEL_CHANNEL
    bot.sync_guild.assert_awaited_once_with(GUILD)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert followup_text(interaction) == "✅ Pannelli pubblicati in <#10>."


async def test_setchannel_keeps_the_other_settings(
    cog: AdminCog, repo: Repository
) -> None:
    await repo.upsert_guild(
        GuildConfig(guild_id=GUILD, panel_count=3, platform_preset=PlatformPreset.XBOX)
    )

    await invoke(cog.setchannel, cog, make_interaction(), make_channel())

    config = await repo.get_guild(GUILD)
    assert config is not None
    assert (config.panel_count, config.platform_preset) == (3, PlatformPreset.XBOX)


@pytest.mark.parametrize(
    ("missing", "label"),
    [
        ("view_channel", "Visualizzare il canale"),
        ("send_messages", "Inviare messaggi"),
        ("embed_links", "Incorporare link"),
        ("read_message_history", "Leggere la cronologia dei messaggi"),
    ],
)
async def test_setchannel_rejects_a_channel_the_bot_cannot_use(
    cog: AdminCog,
    bot: SimpleNamespace,
    repo: Repository,
    missing: str,
    label: str,
) -> None:
    interaction = make_interaction()
    channel = make_channel(permissions=without(missing))

    await invoke(cog.setchannel, cog, interaction, channel)

    text = rejection_text(interaction)
    assert label in text
    assert "<#10>" in text
    assert await repo.get_guild(GUILD) is None
    bot.sync_guild.assert_not_awaited()


async def test_setchannel_lists_every_missing_permission(cog: AdminCog) -> None:
    interaction = make_interaction()
    channel = make_channel(permissions=discord.Permissions.none())

    await invoke(cog.setchannel, cog, interaction, channel)

    text = rejection_text(interaction)
    assert text.count("•") == 4


async def test_setchannel_warns_when_the_panels_cannot_be_posted(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    bot.sync_guild.side_effect = None
    bot.sync_guild.return_value = report(guilds_failed=1)
    interaction = make_interaction()

    await invoke(cog.setchannel, cog, interaction, make_channel())

    assert "/checknow" in followup_text(interaction)
    config = await repo.get_guild(GUILD)
    assert config is not None
    assert config.panel_channel_id == PANEL_CHANNEL


# -- /config -------------------------------------------------------------------


async def test_config_updates_only_what_is_passed(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(
        repo,
        panel_count=3,
        platform_preset=PlatformPreset.XBOX,
        alert_channel_id=ALERT_CHANNEL,
        alert_role_id=ROLE,
        timezone="America/New_York",
    )
    interaction = make_interaction()

    await run_config(cog, interaction, numero_pannelli=7)

    config = await repo.get_guild(GUILD)
    assert config == GuildConfig(
        guild_id=GUILD,
        panel_channel_id=PANEL_CHANNEL,
        panel_count=7,
        platform_preset=PlatformPreset.XBOX,
        alert_channel_id=ALERT_CHANNEL,
        alert_role_id=ROLE,
        timezone="America/New_York",
    )
    text = followup_text(interaction)
    assert text.startswith("✅ Impostazioni aggiornate.")
    assert "Numero pannelli: 7" in text


async def test_config_applies_every_setting_at_once(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(repo)

    await run_config(
        cog,
        make_interaction(),
        piattaforme="pc_only",
        numero_pannelli=2,
        canale_avvisi=make_channel(ALERT_CHANNEL),
        ruolo_avvisi=make_role(),
        fuso_orario=" America/New_York ",
    )

    config = await repo.get_guild(GUILD)
    assert config == GuildConfig(
        guild_id=GUILD,
        panel_channel_id=PANEL_CHANNEL,
        panel_count=2,
        platform_preset=PlatformPreset.PC_ONLY,
        alert_channel_id=ALERT_CHANNEL,
        alert_role_id=ROLE,
        timezone="America/New_York",
    )
    bot.sync_guild.assert_awaited_once_with(GUILD)


@pytest.mark.parametrize(
    "params",
    [
        {"piattaforme": "xbox"},
        {"numero_pannelli": 9},
        {"fuso_orario": "Asia/Tokyo"},
    ],
)
async def test_display_settings_trigger_a_refresh(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository, params: dict[str, Any]
) -> None:
    await configured_guild(repo)
    interaction = make_interaction()

    await run_config(cog, interaction, **params)

    bot.sync_guild.assert_awaited_once_with(GUILD)
    assert "🔄 Pannelli aggiornati." in followup_text(interaction)


async def test_alert_settings_do_not_trigger_a_refresh(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(repo)

    await run_config(
        cog,
        make_interaction(),
        canale_avvisi=make_channel(ALERT_CHANNEL),
        ruolo_avvisi=make_role(),
    )

    bot.sync_guild.assert_not_awaited()
    config = await repo.get_guild(GUILD)
    assert config is not None
    assert (config.alert_channel_id, config.alert_role_id) == (ALERT_CHANNEL, ROLE)


async def test_config_can_clear_the_alert_settings(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(
        repo,
        panel_count=4,
        alert_channel_id=ALERT_CHANNEL,
        alert_role_id=ROLE,
    )
    interaction = make_interaction()

    await run_config(cog, interaction, azzera_avvisi=True)

    config = await repo.get_guild(GUILD)
    assert config is not None
    assert (config.alert_channel_id, config.alert_role_id) == (None, None)
    assert config.panel_count == 4
    text = followup_text(interaction)
    assert text.startswith("✅ Impostazioni aggiornate.")
    assert "Canale avvisi: come i pannelli" in text
    assert "Ruolo avvisi: nessuno" in text
    bot.sync_guild.assert_not_awaited()


async def test_clearing_alerts_applies_the_other_parameters_afterwards(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(
        repo,
        alert_channel_id=ALERT_CHANNEL,
        alert_role_id=ROLE,
    )

    await run_config(
        cog,
        make_interaction(),
        azzera_avvisi=True,
        ruolo_avvisi=make_role(777),
        numero_pannelli=8,
    )

    config = await repo.get_guild(GUILD)
    assert config is not None
    assert config.alert_channel_id is None
    assert config.alert_role_id == 777
    assert config.panel_count == 8
    bot.sync_guild.assert_awaited_once_with(GUILD)


async def test_clearing_alerts_still_validates_a_passed_alert_channel(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    config = await configured_guild(
        repo, alert_channel_id=ALERT_CHANNEL, alert_role_id=ROLE
    )
    interaction = make_interaction()

    await run_config(
        cog,
        interaction,
        azzera_avvisi=True,
        canale_avvisi=make_channel(ALERT_CHANNEL, without("send_messages")),
    )

    assert "Inviare messaggi" in rejection_text(interaction)
    # Nothing is cleared when validation fails.
    assert await repo.get_guild(GUILD) == config


async def test_clearing_alerts_on_a_new_guild_creates_defaults(
    cog: AdminCog, repo: Repository
) -> None:
    await run_config(cog, make_interaction(), azzera_avvisi=True)

    config = await repo.get_guild(GUILD)
    assert config is not None
    assert (config.alert_channel_id, config.alert_role_id) == (None, None)


async def test_unchanged_display_settings_do_not_trigger_a_refresh(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(repo, panel_count=4)

    await run_config(cog, make_interaction(), numero_pannelli=4, piattaforme="all")

    bot.sync_guild.assert_not_awaited()


async def test_no_refresh_without_a_panel_channel(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    interaction = make_interaction()

    await run_config(cog, interaction, numero_pannelli=8)

    bot.sync_guild.assert_not_awaited()
    config = await repo.get_guild(GUILD)
    assert config is not None
    assert config.panel_count == 8
    assert "Canale pannelli: non impostato" in followup_text(interaction)


async def test_config_warns_when_the_refresh_fails(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(repo)
    bot.sync_guild.side_effect = None
    bot.sync_guild.return_value = report(catalog_failed=True)
    interaction = make_interaction()

    await run_config(cog, interaction, numero_pannelli=2)

    assert "/checknow" in followup_text(interaction)


@pytest.mark.parametrize("name", ["Mars/Olympus", "Europe", "", "../etc", "rome"])
async def test_config_rejects_an_invalid_timezone(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository, name: str
) -> None:
    config = await configured_guild(repo)
    interaction = make_interaction()

    await run_config(cog, interaction, numero_pannelli=9, fuso_orario=name)

    text = rejection_text(interaction)
    assert text == INVALID_TIMEZONE
    assert "Europe/Rome" in text
    # Nothing is written, not even the valid settings passed alongside.
    assert await repo.get_guild(GUILD) == config
    bot.sync_guild.assert_not_awaited()


async def test_config_rejects_alert_channel_the_bot_cannot_write_to(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    config = await configured_guild(repo)
    interaction = make_interaction()

    await run_config(
        cog,
        interaction,
        numero_pannelli=9,
        canale_avvisi=make_channel(ALERT_CHANNEL, without("send_messages")),
    )

    assert "Inviare messaggi" in rejection_text(interaction)
    assert await repo.get_guild(GUILD) == config


async def test_config_without_parameters_shows_the_current_settings(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(repo, panel_count=6, alert_role_id=ROLE)
    interaction = make_interaction()

    await run_config(cog, interaction)

    text = followup_text(interaction)
    assert text.startswith("⚙️ Impostazioni attuali.")
    assert "Numero pannelli: 6" in text
    assert "<@&555>" in text
    assert "Piattaforme: Tutte" in text
    bot.sync_guild.assert_not_awaited()


async def test_config_defaults_a_new_guild_to_the_bot_timezone(
    cog: AdminCog, repo: Repository
) -> None:
    interaction = make_interaction()

    await run_config(cog, interaction, piattaforme="nintendo")

    config = await repo.get_guild(GUILD)
    assert config is not None
    assert config.platform_preset is PlatformPreset.NINTENDO
    assert config.timezone == "Europe/Rome"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Europe/Rome", "Europe/Rome"),
        ("  UTC ", "UTC"),
        ("Mars/Olympus", None),
        ("Europe", None),
        ("", None),
        ("../x", None),
    ],
)
def test_parse_timezone(name: str, expected: str | None) -> None:
    assert parse_timezone(name) == expected


# -- /checknow -----------------------------------------------------------------


async def test_checknow_bypasses_the_cache_and_reports(
    cog: AdminCog,
    bot: SimpleNamespace,
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await configured_guild(repo)
    await repo.upsert_panel_message(GUILD, 0, PANEL_CHANNEL, 501, "hash")
    interaction = make_interaction()

    await invoke(cog.checknow, cog, interaction, False)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    bot.run_sync_cycle.assert_awaited_once_with(bypass_cache=True)
    text = followup_text(interaction)
    assert text.startswith("✅ Controllo completato.")
    assert "Server sincronizzati: 2" in text
    assert "Messaggi modificati: 5" in text
    assert "ripristinati" not in text
    # Nothing is deleted without ``ripristina``.
    assert messenger.deleted == []
    assert len(await repo.get_panel_messages(GUILD)) == 1


async def test_checknow_defaults_to_keeping_the_panel(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository, messenger: FakeMessenger
) -> None:
    await configured_guild(repo)
    await repo.upsert_panel_message(GUILD, 0, PANEL_CHANNEL, 501, "hash")

    await invoke(cog.checknow, cog, make_interaction())

    assert messenger.deleted == []


async def test_checknow_restore_clears_the_panel_before_syncing(
    cog: AdminCog,
    bot: SimpleNamespace,
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await configured_guild(repo)
    for slot, message_id in ((-1, 500), (0, 501), (1, 502), (2, 503)):
        await repo.upsert_panel_message(GUILD, slot, PANEL_CHANNEL, message_id, "h")
    messenger.missing.add(501)  # deleted by hand
    messenger.forbidden.add(502)  # no longer deletable

    async def check_cleared(bypass_cache: bool = False) -> SyncReport:
        assert await repo.get_panel_messages(GUILD) == []
        return report(guilds_synced=1)

    bot.run_sync_cycle.side_effect = check_cleared
    interaction = make_interaction()

    await invoke(cog.checknow, cog, interaction, True)

    assert sorted(messenger.deleted) == [
        (PANEL_CHANNEL, 500),
        (PANEL_CHANNEL, 501),
        (PANEL_CHANNEL, 502),
        (PANEL_CHANNEL, 503),
    ]
    bot.run_sync_cycle.assert_awaited_once_with(bypass_cache=True)
    text = followup_text(interaction)
    assert "🧹 Pannelli ripristinati (4 messaggi rimossi)." in text
    assert "✅ Controllo completato." in text


async def test_checknow_reports_failures(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await configured_guild(repo)
    bot.run_sync_cycle.side_effect = None
    bot.run_sync_cycle.return_value = report(guilds_failed=1, catalog_failed=True)
    interaction = make_interaction()

    await invoke(cog.checknow, cog, interaction, False)

    text = followup_text(interaction)
    assert text.startswith("⚠️ Controllo completato con errori.")
    assert "Server con errori: 1" in text
    assert "IGDB non risponde" in text


@pytest.mark.parametrize("restore", [False, True])
async def test_checknow_needs_a_panel_channel(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository, restore: bool
) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))
    interaction = make_interaction()

    await invoke(cog.checknow, cog, interaction, restore)

    assert followup_text(interaction) == NO_PANEL_CHANNEL
    bot.run_sync_cycle.assert_not_awaited()


# -- /status -------------------------------------------------------------------


def status_fields(interaction: MagicMock) -> dict[str, str]:
    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    embed: discord.Embed = kwargs["embed"]
    return {field.name or "": field.value or "" for field in embed.fields}


async def test_status_shows_the_metadata(
    cog: AdminCog, bot: SimpleNamespace, repo: Repository
) -> None:
    await repo.upsert_guild(GuildConfig(guild_id=GUILD))
    await repo.upsert_guild(GuildConfig(guild_id=GUILD + 1))
    await repo.toggle_reminder(USER, 1, GUILD, PANEL_CHANNEL)
    await repo.toggle_reminder(USER, 2, GUILD, PANEL_CHANNEL)
    await repo.toggle_reminder(USER, 3, GUILD, PANEL_CHANNEL)
    await repo.toggle_reminder(USER, 3, GUILD, PANEL_CHANNEL)  # removed again
    await repo.mark_notified(GUILD, 1)
    bot.last_sync = report(guilds_synced=1)
    bot.next_sync_at = NOW + timedelta(hours=2)
    interaction = make_interaction()

    await invoke(cog.status, cog, interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    fields = status_fields(interaction)
    assert fields["⏱️ Uptime"] == "1g 2h 3m"
    assert fields["📡 Latenza Discord"] == "43 ms"
    assert "Server configurati: 2" in fields["🗄️ Database"]
    assert "Promemoria attivi: 2" in fields["🗄️ Database"]
    assert "Giochi notificati: 1" in fields["🗄️ Database"]
    last = int(NOW.timestamp())
    assert fields["🔄 Ultima sincronizzazione"] == f"<t:{last}:R> — ✅ Riuscita"
    following = int((NOW + timedelta(hours=2)).timestamp())
    assert fields["⏭️ Prossima sincronizzazione"] == f"<t:{following}:R>"


async def test_status_before_the_first_sync(
    cog: AdminCog, bot: SimpleNamespace
) -> None:
    bot.latency = math.nan  # not connected yet
    interaction = make_interaction()

    await invoke(cog.status, cog, interaction)

    fields = status_fields(interaction)
    assert fields["📡 Latenza Discord"] == "—"
    assert fields["🔄 Ultima sincronizzazione"] == "Non ancora eseguita"
    assert fields["⏭️ Prossima sincronizzazione"] == "Non pianificata"
    assert "Server configurati: 0" in fields["🗄️ Database"]


async def test_status_flags_a_sync_with_errors(
    cog: AdminCog, bot: SimpleNamespace
) -> None:
    bot.last_sync = report(guilds_failed=1)
    interaction = make_interaction()

    await invoke(cog.status, cog, interaction)

    assert status_fields(interaction)["🔄 Ultima sincronizzazione"].endswith(
        "⚠️ Con errori"
    )


# -- registration --------------------------------------------------------------


async def test_cog_registers_guarded_italian_commands() -> None:
    settings = Settings(
        discord_token="token",  # pyright: ignore[reportArgumentType]
        twitch_client_id="id",
        twitch_client_secret="secret",  # pyright: ignore[reportArgumentType]
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )
    bot = GameReleasesBot(settings)

    await setup(bot)

    commands = {command.name: command for command in bot.tree.get_commands()}
    assert set(commands) == {"setchannel", "config", "checknow", "status"}
    for command in commands.values():
        assert isinstance(command, discord.app_commands.Command)
        assert command.guild_only
        assert command.default_permissions is not None
        assert command.default_permissions.manage_guild
        assert command.description

    config = commands["config"]
    assert isinstance(config, discord.app_commands.Command)
    params = config._params  # pyright: ignore[reportPrivateUsage]
    assert [choice.name for choice in params["piattaforme"].choices] == [
        "Tutte",
        "PC + Console",
        "Solo PC",
        "PlayStation",
        "Xbox",
        "Nintendo",
    ]
    assert (
        params["numero_pannelli"].min_value,
        params["numero_pannelli"].max_value,
    ) == (
        1,
        10,
    )
    assert all(not params[name].required for name in params)
