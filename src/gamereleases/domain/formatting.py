"""Builds presentation-neutral embed data for games and panels.

User-facing strings are Italian. Nothing here talks to Discord: the functions
return plain data that the bot layer turns into embeds.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, time
from enum import Enum
from itertools import groupby
from typing import Any, cast

from gamereleases.domain.models import TOP_LIST_SIZE, PlatformPreset, RankedGame
from gamereleases.domain.ranking import days_until

# Beyond this many days a countdown is replaced by the absolute date.
COUNTDOWN_MAX_DAYS = 30

EMBED_DESCRIPTION_LIMIT = 4096
# Room kept for the "and N more" line when a calendar has to be cut short.
_OVERFLOW_RESERVE = 64
_MAX_NAME_LENGTH = 60

PRESET_LABELS: Mapping[PlatformPreset, str] = {
    PlatformPreset.ALL: "Tutte",
    PlatformPreset.PC_CONSOLE: "PC + Console",
    PlatformPreset.PC_ONLY: "Solo PC",
    PlatformPreset.PLAYSTATION: "PlayStation",
    PlatformPreset.XBOX: "Xbox",
    PlatformPreset.NINTENDO: "Nintendo",
}

_MARKDOWN_SPECIALS = re.compile(r"([\\*_~`|>\[\]])")


@dataclass(frozen=True, slots=True)
class EmbedData:
    """Title and body of an embed, with Discord timestamps already inlined."""

    title: str
    description: str
    url: str | None = None


def format_countdown(release_date: date, today: date) -> str:
    """Describe how far away a release is, in Italian."""
    delta = days_until(release_date, today)
    if delta < 0:
        return "Uscito"
    if delta == 0:
        return "Esce oggi!"
    if delta == 1:
        return "Tra 1 giorno"
    if delta <= COUNTDOWN_MAX_DAYS:
        return f"Tra {delta} giorni"
    return release_date.strftime("%d/%m/%Y")


def release_timestamp(day: date) -> int:
    """Return a Unix timestamp that renders as ``day`` for most viewers.

    Noon UTC keeps the calendar day stable for every viewer between UTC-11 and
    UTC+11, whichever time zone Discord renders the timestamp in.
    """
    return int(datetime.combine(day, time(12), tzinfo=UTC).timestamp())


def discord_timestamp(unix: int, style: str) -> str:
    """Format a Discord dynamic timestamp tag (``d``, ``D``, ``R``, ...)."""
    return f"<t:{unix}:{style}>"


def escape_markdown(text: str) -> str:
    """Backslash-escape characters that Discord would render as markdown."""
    return _MARKDOWN_SPECIALS.sub(r"\\\1", text)


def _display_name(name: str) -> str:
    if len(name) > _MAX_NAME_LENGTH:
        name = name[: _MAX_NAME_LENGTH - 1].rstrip() + "…"
    return escape_markdown(name)


def _unix(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def build_overview(
    ranked: Sequence[RankedGame],
    *,
    synced_at: datetime,
    preset: PlatformPreset,
    limit: int = TOP_LIST_SIZE,
) -> EmbedData:
    """Build the compact overview: chronological top list plus sync status.

    ``ranked`` is expected in chronological order, as returned by the ranking
    step; only the first ``limit`` entries are shown.
    """
    lines = [
        f"• **{_display_name(item.game.name)}** — "
        f"{discord_timestamp(release_timestamp(item.release_date), 'd')}"
        for item in ranked[:limit]
    ]
    body = "\n".join(lines) if lines else "Nessuna uscita in programma al momento."
    footer = (
        f"🕒 Ultimo aggiornamento: {discord_timestamp(_unix(synced_at), 'R')}\n"
        f"🎯 Piattaforme: {PRESET_LABELS[preset]}"
    )
    return EmbedData(
        title="🎮 Prossime uscite",
        description=f"{body}\n\n{footer}",
    )


def group_by_release_date(
    ranked: Sequence[RankedGame],
) -> list[tuple[date, list[RankedGame]]]:
    """Group games by release day, in chronological order."""
    ordered = sorted(ranked, key=lambda item: item.release_date)
    return [
        (day, list(items))
        for day, items in groupby(ordered, key=lambda item: item.release_date)
    ]


def build_calendar(
    ranked: Sequence[RankedGame],
    *,
    today: date,
    days: int,
) -> EmbedData:
    """Build the compact calendar, one block per release day.

    The list is cut short, with a note, if it would not fit in an embed.
    """
    window = "prossimo giorno" if days == 1 else f"prossimi {days} giorni"
    title = f"📅 Calendario uscite ({window})"
    if not ranked:
        return EmbedData(title=title, description="Nessuna uscita in programma.")

    budget = EMBED_DESCRIPTION_LIMIT - _OVERFLOW_RESERVE
    lines: list[str] = []
    used = 0
    shown = 0
    for day, items in group_by_release_date(ranked):
        header = (
            f"**{discord_timestamp(release_timestamp(day), 'd')}** — "
            f"{format_countdown(day, today)}"
        )
        for index, item in enumerate(items):
            bullet = f"• {_display_name(item.game.name)}"
            addition = [header, bullet] if index == 0 else [bullet]
            cost = sum(len(line) + 1 for line in addition)
            if used + cost > budget:
                remaining = len(ranked) - shown
                lines.append(f"…e altre {remaining} uscite")
                return EmbedData(title=title, description="\n".join(lines))
            lines.extend(addition)
            used += cost
            shown += 1
    return EmbedData(title=title, description="\n".join(lines))


def _json_default(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(cast("Iterable[object]", value), key=repr)
    raise TypeError(f"cannot hash value of type {type(value).__name__}")


def hash_payload(payload: object) -> str:
    """Return a deterministic SHA-256 hex digest of a message payload.

    Mapping key order, and the difference between lists and tuples, do not
    affect the result, so an unchanged panel always hashes to the same value.
    """
    canonical = json.dumps(
        payload,
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
