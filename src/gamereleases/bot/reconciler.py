"""Keeps each guild panel in sync with the current release list."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from gamereleases.domain.formatting import hash_payload
from gamereleases.domain.models import OVERVIEW_SLOT, PanelMessage
from gamereleases.storage.repository import Repository

logger = logging.getLogger(__name__)


class MessageNotFound(Exception):
    """The target message (or its channel) no longer exists."""


class MessageForbidden(Exception):
    """The bot lacks permission to act on the target message."""


class ChannelMessenger[P](Protocol):
    """The Discord operations the reconciler needs.

    ``P`` is the description of one message (embed plus components). It must be
    hashable through :func:`gamereleases.domain.formatting.hash_payload`.
    Implementations translate the transport's own errors into
    :class:`MessageNotFound` and :class:`MessageForbidden`.
    """

    async def send(self, channel_id: int, payload: P) -> int:
        """Post a new message and return its id."""
        ...

    async def edit(self, channel_id: int, message_id: int, payload: P) -> None:
        """Replace a message's content; raise ``MessageNotFound`` if it is gone."""
        ...

    async def delete(self, channel_id: int, message_id: int) -> None:
        """Delete a message; raise ``MessageNotFound`` if it is already gone."""
        ...


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What a reconciliation pass did, by number of messages."""

    created: int = 0
    edited: int = 0
    unchanged: int = 0
    deleted: int = 0
    recreated: int = 0
    migrated: bool = False


class PanelReconciler[P]:
    """Brings a guild's posted messages in line with the desired panel.

    The panel is the overview in slot ``OVERVIEW_SLOT`` followed by one card
    per slot ``0..panel_count - 1``. Messages whose payload hash is unchanged
    are left alone. A message that was deleted by hand is posted again, which
    puts it after the other cards in the channel.
    """

    def __init__(self, repository: Repository, messenger: ChannelMessenger[P]) -> None:
        self._repository = repository
        self._messenger = messenger
        self._locks: dict[int, asyncio.Lock] = {}

    async def reconcile(
        self,
        guild_id: int,
        channel_id: int,
        overview: P,
        cards: Sequence[P],
        panel_count: int,
    ) -> ReconcileResult:
        """Sync the panel in ``channel_id``; passes for one guild never overlap.

        Cards beyond ``panel_count`` are ignored. Errors other than a missing
        message propagate; state recorded so far stays valid, so the next pass
        resumes where this one stopped.
        """
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            return await self._reconcile(
                guild_id, channel_id, overview, cards[: max(panel_count, 0)]
            )

    async def clear(self, guild_id: int) -> int:
        """Forget the guild's panel, deleting its messages on a best-effort basis.

        The next :meth:`reconcile` then posts every message again, in order.
        Returns how many tracked messages were dropped.
        """
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            tracked = await self._repository.get_panel_messages(guild_id)
            for message in tracked:
                try:
                    await self._delete_quietly(message)
                except Exception:
                    logger.warning(
                        "Could not delete message %d in channel %d",
                        message.message_id,
                        message.channel_id,
                        exc_info=True,
                    )
            await self._repository.clear_panel_messages(guild_id)
            return len(tracked)

    async def _reconcile(
        self,
        guild_id: int,
        channel_id: int,
        overview: P,
        cards: Sequence[P],
    ) -> ReconcileResult:
        tracked = {
            m.slot: m for m in await self._repository.get_panel_messages(guild_id)
        }

        migrated = any(m.channel_id != channel_id for m in tracked.values())
        deleted = 0
        if migrated:
            for message in tracked.values():
                await self._delete_quietly(message)
            deleted += len(tracked)
            await self._repository.clear_panel_messages(guild_id)
            tracked = {}

        desired: dict[int, P] = {OVERVIEW_SLOT: overview}
        desired.update(enumerate(cards))

        created = edited = unchanged = recreated = 0
        for slot, payload in desired.items():
            digest = hash_payload(payload)
            existing = tracked.get(slot)
            if existing is None:
                await self._post(guild_id, slot, channel_id, payload, digest)
                created += 1
            elif existing.payload_hash == digest:
                unchanged += 1
            else:
                try:
                    await self._messenger.edit(
                        existing.channel_id, existing.message_id, payload
                    )
                except MessageNotFound:
                    logger.info(
                        "Panel message for guild %d slot %d is gone; recreating",
                        guild_id,
                        slot,
                    )
                    await self._post(guild_id, slot, channel_id, payload, digest)
                    recreated += 1
                else:
                    await self._repository.upsert_panel_message(
                        guild_id,
                        slot,
                        existing.channel_id,
                        existing.message_id,
                        digest,
                    )
                    edited += 1

        for slot, message in tracked.items():
            if slot in desired:
                continue
            await self._delete_quietly(message)
            await self._repository.delete_panel_slot(guild_id, slot)
            deleted += 1

        return ReconcileResult(
            created=created,
            edited=edited,
            unchanged=unchanged,
            deleted=deleted,
            recreated=recreated,
            migrated=migrated,
        )

    async def _post(
        self, guild_id: int, slot: int, channel_id: int, payload: P, digest: str
    ) -> None:
        message_id = await self._messenger.send(channel_id, payload)
        await self._repository.upsert_panel_message(
            guild_id, slot, channel_id, message_id, digest
        )

    async def _delete_quietly(self, message: PanelMessage) -> None:
        """Delete a message, ignoring one that is gone or off limits."""
        try:
            await self._messenger.delete(message.channel_id, message.message_id)
        except (MessageNotFound, MessageForbidden):
            logger.debug(
                "Could not delete message %d in channel %d",
                message.message_id,
                message.channel_id,
            )
