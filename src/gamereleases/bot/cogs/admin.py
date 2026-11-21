"""Server management commands."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

from gamereleases.bot.app import GameReleasesBot
from gamereleases.bot.sync import SyncReport
from gamereleases.domain.formatting import (
    PRESET_LABELS,
    discord_timestamp,
    format_duration,
)
from gamereleases.domain.models import (
    PANEL_COUNT_MAX,
    PANEL_COUNT_MIN,
    GuildConfig,
    PlatformPreset,
)

logger = logging.getLogger(__name__)

EMBED_COLOR = 0x5865F2

# Permissions the bot needs where it posts the panels, and where it posts alerts.
PANEL_PERMISSIONS = (
    "view_channel",
    "send_messages",
    "embed_links",
    "read_message_history",
)
ALERT_PERMISSIONS = ("view_channel", "send_messages", "embed_links")

PERMISSION_LABELS = {
    "view_channel": "Visualizzare il canale",
    "send_messages": "Inviare messaggi",
    "embed_links": "Incorporare link",
    "read_message_history": "Leggere la cronologia dei messaggi",
}

PLATFORM_CHOICES = [
    app_commands.Choice(name=label, value=preset.value)
    for preset, label in PRESET_LABELS.items()
]

NO_PERMISSION = "⛔ Per usare questo comando ti serve il permesso **Gestisci server**."
NO_PANEL_CHANNEL = (
    "ℹ️ Nessun canale impostato: usa prima `/setchannel` per scegliere dove "
    "pubblicare i pannelli."
)
INVALID_TIMEZONE = (
    "⚠️ Fuso orario non valido. Usa un nome della banca dati IANA, "
    "ad esempio `Europe/Rome`."
)


def parse_timezone(name: str) -> str | None:
    """Return the canonical zone name if it is a valid IANA zone, else ``None``."""
    name = name.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def missing_permissions(
    channel: discord.abc.GuildChannel, required: Iterable[str]
) -> list[str]:
    """Return the labels of the permissions the bot lacks in ``channel``."""
    granted = channel.permissions_for(channel.guild.me)
    return [PERMISSION_LABELS[name] for name in required if not getattr(granted, name)]


def _format_missing(channel: discord.abc.GuildChannel, missing: list[str]) -> str:
    lines = "\n".join(f"• {label}" for label in missing)
    return f"⚠️ Mi mancano dei permessi in {channel.mention}:\n{lines}"


def describe_config(config: GuildConfig) -> str:
    """Summarize a guild's settings, in Italian."""
    panel = (
        f"<#{config.panel_channel_id}>" if config.panel_channel_id else "non impostato"
    )
    alerts = (
        f"<#{config.alert_channel_id}>"
        if config.alert_channel_id
        else "come i pannelli"
    )
    role = f"<@&{config.alert_role_id}>" if config.alert_role_id else "nessuno"
    return (
        f"• Canale pannelli: {panel}\n"
        f"• Piattaforme: {PRESET_LABELS[config.platform_preset]}\n"
        f"• Numero pannelli: {config.panel_count}\n"
        f"• Canale avvisi: {alerts}\n"
        f"• Ruolo avvisi: {role}\n"
        f"• Fuso orario: {config.timezone}"
    )


def describe_sync(report: SyncReport | None) -> str:
    """Describe the last full sync for the status embed."""
    if report is None:
        return "Non ancora eseguita"
    when = discord_timestamp(int(report.finished_at.timestamp()), "R")
    outcome = "✅ Riuscita" if report.ok else "⚠️ Con errori"
    return f"{when} — {outcome}"


async def require_manage_guild(interaction: discord.Interaction) -> bool:
    """Check the caller's permission, answering them privately if it is missing."""
    if interaction.guild_id is not None and interaction.permissions.manage_guild:
        return True
    await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
    return False


class AdminCog(commands.Cog):
    """Commands for server managers."""

    def __init__(self, bot: GameReleasesBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="setchannel", description="Imposta il canale dove pubblicare i pannelli"
    )
    @app_commands.describe(canale="Canale di testo per i pannelli delle uscite")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setchannel(
        self, interaction: discord.Interaction, canale: discord.TextChannel
    ) -> None:
        if not await require_manage_guild(interaction):
            return
        missing = missing_permissions(canale, PANEL_PERMISSIONS)
        if missing:
            await interaction.response.send_message(
                _format_missing(canale, missing), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = canale.guild.id
        await self.bot.repository.set_panel_channel(guild_id, canale.id)
        report = await self.bot.sync_guild(guild_id)
        if report.ok:
            message = f"✅ Pannelli pubblicati in {canale.mention}."
        else:
            message = (
                f"⚠️ Canale impostato su {canale.mention}, ma non sono riuscito ad "
                "aggiornare i pannelli. Riprova con `/checknow`."
            )
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(
        name="config", description="Mostra o modifica le impostazioni del server"
    )
    @app_commands.describe(
        piattaforme="Quali piattaforme mostrare",
        numero_pannelli="Quante schede di gioco pubblicare (da 1 a 10)",
        canale_avvisi="Canale per gli avvisi di uscita",
        ruolo_avvisi="Ruolo da menzionare negli avvisi di uscita",
        azzera_avvisi="Rimuovi canale e ruolo degli avvisi (poi applica gli altri)",
        fuso_orario="Fuso orario IANA, ad esempio Europe/Rome",
    )
    @app_commands.choices(piattaforme=PLATFORM_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def config(
        self,
        interaction: discord.Interaction,
        piattaforme: str | None = None,
        numero_pannelli: app_commands.Range[int, PANEL_COUNT_MIN, PANEL_COUNT_MAX]
        | None = None,
        canale_avvisi: discord.TextChannel | None = None,
        ruolo_avvisi: discord.Role | None = None,
        azzera_avvisi: bool = False,
        fuso_orario: str | None = None,
    ) -> None:
        if not await require_manage_guild(interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        # Everything is validated before anything is written.
        preset = None if piattaforme is None else _parse_preset(piattaforme)
        if piattaforme is not None and preset is None:
            await interaction.response.send_message(
                "⚠️ Piattaforme non valide.", ephemeral=True
            )
            return
        if numero_pannelli is not None and not (
            PANEL_COUNT_MIN <= numero_pannelli <= PANEL_COUNT_MAX
        ):
            await interaction.response.send_message(
                f"⚠️ Il numero di pannelli deve essere tra {PANEL_COUNT_MIN} "
                f"e {PANEL_COUNT_MAX}.",
                ephemeral=True,
            )
            return
        timezone = None if fuso_orario is None else parse_timezone(fuso_orario)
        if fuso_orario is not None and timezone is None:
            await interaction.response.send_message(INVALID_TIMEZONE, ephemeral=True)
            return
        if canale_avvisi is not None:
            missing = missing_permissions(canale_avvisi, ALERT_PERMISSIONS)
            if missing:
                await interaction.response.send_message(
                    _format_missing(canale_avvisi, missing), ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True)
        repository = self.bot.repository
        before = await repository.get_guild(guild_id) or GuildConfig(
            guild_id=guild_id, timezone=self.bot.settings.timezone
        )

        changed = False
        if preset is not None and preset != before.platform_preset:
            await repository.update_platform_preset(guild_id, preset)
            changed = True
        if numero_pannelli is not None and numero_pannelli != before.panel_count:
            await repository.update_panel_count(guild_id, numero_pannelli)
            changed = True
        if timezone is not None and timezone != before.timezone:
            await repository.update_timezone(guild_id, timezone)
            changed = True
        # Clearing comes first so a channel or role passed alongside replaces it.
        if azzera_avvisi:
            await repository.update_alert_channel(guild_id, None)
            await repository.update_alert_role(guild_id, None)
        if canale_avvisi is not None:
            await repository.update_alert_channel(guild_id, canale_avvisi.id)
        if ruolo_avvisi is not None:
            await repository.update_alert_role(guild_id, ruolo_avvisi.id)

        after = await repository.get_guild(guild_id) or before
        lines = [describe_config(after)]
        passed = azzera_avvisi or any(
            value is not None
            for value in (
                piattaforme,
                numero_pannelli,
                canale_avvisi,
                ruolo_avvisi,
                fuso_orario,
            )
        )
        title = "✅ Impostazioni aggiornate." if passed else "⚙️ Impostazioni attuali."

        # Only the settings that change what the panel shows need a refresh.
        if changed and after.panel_channel_id is not None:
            report = await self.bot.sync_guild(guild_id)
            lines.append(
                "🔄 Pannelli aggiornati."
                if report.ok
                else "⚠️ Non sono riuscito ad aggiornare i pannelli: "
                "riprova con `/checknow`."
            )
        await interaction.followup.send("\n".join([title, *lines]), ephemeral=True)

    @app_commands.command(
        name="checknow", description="Controlla subito le uscite e aggiorna i pannelli"
    )
    @app_commands.describe(
        ripristina="Elimina i messaggi dei pannelli e li ripubblica in ordine"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def checknow(
        self, interaction: discord.Interaction, ripristina: bool = False
    ) -> None:
        if not await require_manage_guild(interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        await interaction.response.defer(ephemeral=True)
        config = await self.bot.repository.get_guild(guild_id)
        if config is None or config.panel_channel_id is None:
            await interaction.followup.send(NO_PANEL_CHANNEL, ephemeral=True)
            return

        lines: list[str] = []
        if ripristina:
            removed = await self.bot.reconciler.clear(guild_id)
            lines.append(f"🧹 Pannelli ripristinati ({removed} messaggi rimossi).")
        report = await self.bot.run_sync_cycle(bypass_cache=True)
        lines.extend(_describe_report(report))
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="status", description="Mostra lo stato del bot")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if not await require_manage_guild(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        stats = await self.bot.repository.count_stats()
        latency = self.bot.latency
        latency_text = f"{round(latency * 1000)} ms" if math.isfinite(latency) else "—"
        next_sync = self.bot.next_sync_at
        next_text = (
            "Non pianificata"
            if next_sync is None
            else discord_timestamp(int(next_sync.timestamp()), "R")
        )

        embed = discord.Embed(title="📊 Stato del bot", colour=EMBED_COLOR)
        embed.add_field(name="⏱️ Uptime", value=format_duration(self.bot.uptime))
        embed.add_field(name="📡 Latenza Discord", value=latency_text)
        embed.add_field(
            name="🗄️ Database",
            value=(
                f"Server configurati: {stats.guilds}\n"
                f"Promemoria attivi: {stats.pending_reminders}\n"
                f"Giochi notificati: {stats.notified_games}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔄 Ultima sincronizzazione",
            value=describe_sync(self.bot.last_sync),
            inline=False,
        )
        embed.add_field(
            name="⏭️ Prossima sincronizzazione", value=next_text, inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


def _parse_preset(value: str) -> PlatformPreset | None:
    try:
        return PlatformPreset(value)
    except ValueError:
        return None


def _describe_report(report: SyncReport) -> list[str]:
    lines = [
        "✅ Controllo completato."
        if report.ok
        else "⚠️ Controllo completato con errori.",
        f"• Server sincronizzati: {report.guilds_synced}",
        f"• Messaggi modificati: {report.messages_changed}",
    ]
    if report.guilds_failed:
        lines.append(f"• Server con errori: {report.guilds_failed}")
    if report.catalog_failed:
        lines.append("• IGDB non risponde: i dati potrebbero non essere aggiornati.")
    return lines


async def setup(bot: GameReleasesBot) -> None:
    await bot.add_cog(AdminCog(bot))
