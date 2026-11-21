"""Core domain types shared by the data, storage and presentation layers.

This module must stay free of Discord, HTTP and database imports so that it
can be unit tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum, StrEnum

DEFAULT_TIMEZONE = "Europe/Rome"
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 90
TOP_LIST_SIZE = 10

PANEL_COUNT_MIN = 1
PANEL_COUNT_MAX = 10
PANEL_COUNT_DEFAULT = 5

# Panel slot that holds the compact overview message above the game cards.
OVERVIEW_SLOT = -1


class GameType(IntEnum):
    """IGDB ``game_type`` identifiers."""

    MAIN_GAME = 0
    DLC_ADDON = 1
    EXPANSION = 2
    BUNDLE = 3
    STANDALONE_EXPANSION = 4
    MOD = 5
    EPISODE = 6
    SEASON = 7
    REMAKE = 8
    REMASTER = 9
    EXPANDED_GAME = 10
    PORT = 11
    FORK = 12
    PACK = 13
    UPDATE = 14


class DateFormat(IntEnum):
    """Precision of an IGDB release date (``release_dates.date_format``)."""

    EXACT_DAY = 0
    MONTH = 1
    YEAR = 2
    QUARTER_1 = 3
    QUARTER_2 = 4
    QUARTER_3 = 5
    QUARTER_4 = 6
    TBD = 7


class StoreKind(StrEnum):
    """Link destinations, declared in title-link priority order."""

    STEAM = "steam"
    EPIC = "epic"
    GOG = "gog"
    PLAYSTATION = "playstation"
    XBOX = "xbox"
    NINTENDO = "nintendo"
    OFFICIAL = "official"
    IGDB = "igdb"

    @property
    def priority(self) -> int:
        """Lower values win when choosing the embed title link."""
        return _STORE_PRIORITY[self]


_STORE_PRIORITY: dict[StoreKind, int] = {kind: i for i, kind in enumerate(StoreKind)}


class PlatformGroup(StrEnum):
    """Normalized platform families used for filtering."""

    PC = "pc"
    PS5 = "ps5"
    XBOX_SERIES = "xbox_series"
    SWITCH = "switch"
    LAST_GEN = "last_gen"
    MOBILE = "mobile"
    OTHER = "other"


class PlatformPreset(StrEnum):
    """Guild-selectable platform filters."""

    ALL = "all"
    PC_CONSOLE = "pc_console"
    PC_ONLY = "pc_only"
    PLAYSTATION = "playstation"
    XBOX = "xbox"
    NINTENDO = "nintendo"


@dataclass(frozen=True, slots=True)
class Platform:
    id: int
    name: str
    abbreviation: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformRelease:
    """A single per-platform release date entry."""

    platform_id: int | None
    date: datetime | None
    date_format: DateFormat | None
    human: str | None = None


@dataclass(frozen=True, slots=True)
class StoreLink:
    kind: StoreKind
    url: str


@dataclass(frozen=True, slots=True)
class Game:
    """A normalized game record, independent of the upstream API schema."""

    id: int
    name: str
    slug: str
    summary: str | None = None
    first_release_date: datetime | None = None
    hypes: int = 0
    follows: int = 0
    total_rating: float | None = None
    total_rating_count: int = 0
    game_type: GameType | None = None
    version_parent: int | None = None
    cover_url: str | None = None
    trailer_url: str | None = None
    genres: tuple[str, ...] = ()
    platforms: tuple[Platform, ...] = ()
    release_dates: tuple[PlatformRelease, ...] = ()
    stores: tuple[StoreLink, ...] = ()


@dataclass(frozen=True, slots=True)
class RankedGame:
    """A game paired with its release date resolved in the guild time zone."""

    game: Game
    release_date: date


@dataclass(frozen=True, slots=True)
class GuildConfig:
    guild_id: int
    panel_channel_id: int | None = None
    alert_channel_id: int | None = None
    alert_role_id: int | None = None
    panel_count: int = PANEL_COUNT_DEFAULT
    platform_preset: PlatformPreset = PlatformPreset.ALL
    min_hypes: int = 0
    include_dlc: bool = False
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        if not PANEL_COUNT_MIN <= self.panel_count <= PANEL_COUNT_MAX:
            raise ValueError(
                f"panel_count must be between {PANEL_COUNT_MIN} and "
                f"{PANEL_COUNT_MAX}, got {self.panel_count}"
            )
        if self.min_hypes < 0:
            raise ValueError(f"min_hypes must be non-negative, got {self.min_hypes}")


@dataclass(frozen=True, slots=True)
class PanelMessage:
    """A tracked panel message; ``slot`` is ``OVERVIEW_SLOT`` or a card index."""

    guild_id: int
    slot: int
    channel_id: int
    message_id: int
    payload_hash: str


@dataclass(frozen=True, slots=True)
class Reminder:
    user_id: int
    game_id: int
    guild_id: int | None
    channel_id: int | None
    sent: bool = False
