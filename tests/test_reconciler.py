"""Tests for panel reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from gamereleases.bot.reconciler import (
    MessageForbidden,
    MessageNotFound,
    PanelReconciler,
    ReconcileResult,
)
from gamereleases.domain.formatting import EmbedData, hash_payload
from gamereleases.domain.models import OVERVIEW_SLOT, GuildConfig
from gamereleases.storage.db import Database
from gamereleases.storage.repository import Repository

GUILD = 100
CHANNEL = 10
OLD_CHANNEL = 11


class FakeMessenger:
    """An in-memory channel store that records every call it receives."""

    def __init__(self) -> None:
        self.messages: dict[tuple[int, int], EmbedData] = {}
        self.calls: list[tuple[str, int, int]] = []
        self.forbidden_deletes: set[int] = set()
        self._next_id = 1000

    async def send(self, channel_id: int, payload: EmbedData) -> int:
        self._next_id += 1
        self.messages[(channel_id, self._next_id)] = payload
        self.calls.append(("send", channel_id, self._next_id))
        return self._next_id

    async def edit(self, channel_id: int, message_id: int, payload: EmbedData) -> None:
        self.calls.append(("edit", channel_id, message_id))
        if (channel_id, message_id) not in self.messages:
            raise MessageNotFound
        self.messages[(channel_id, message_id)] = payload

    async def delete(self, channel_id: int, message_id: int) -> None:
        self.calls.append(("delete", channel_id, message_id))
        if message_id in self.forbidden_deletes:
            raise MessageForbidden
        if (channel_id, message_id) not in self.messages:
            raise MessageNotFound
        del self.messages[(channel_id, message_id)]

    def kinds(self, kind: str) -> list[tuple[str, int, int]]:
        return [call for call in self.calls if call[0] == kind]

    def reset_calls(self) -> None:
        self.calls.clear()

    def titles_in(self, channel_id: int) -> list[str]:
        return [
            payload.title
            for (channel, _), payload in sorted(self.messages.items())
            if channel == channel_id
        ]


def embed(title: str, body: str = "") -> EmbedData:
    return EmbedData(title=title, description=body)


def cards(*titles: str) -> list[EmbedData]:
    return [embed(title) for title in titles]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    db = await Database.open(tmp_path / "reconciler.db")
    repository = Repository(db)
    await repository.upsert_guild(GuildConfig(guild_id=GUILD))
    yield repository
    await db.close()


@pytest.fixture
def messenger() -> FakeMessenger:
    return FakeMessenger()


@pytest.fixture
def reconciler(
    repo: Repository, messenger: FakeMessenger
) -> PanelReconciler[EmbedData]:
    return PanelReconciler(repo, messenger)


async def slots(repo: Repository) -> list[int]:
    return [m.slot for m in await repo.get_panel_messages(GUILD)]


async def test_first_pass_posts_overview_and_cards(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    result = await reconciler.reconcile(
        GUILD, CHANNEL, embed("overview"), cards("a", "b"), panel_count=5
    )

    assert result == ReconcileResult(created=3)
    assert messenger.titles_in(CHANNEL) == ["overview", "a", "b"]
    tracked = await repo.get_panel_messages(GUILD)
    assert [m.slot for m in tracked] == [OVERVIEW_SLOT, 0, 1]
    assert all(m.channel_id == CHANNEL for m in tracked)
    assert tracked[1].payload_hash == hash_payload(embed("a"))
    assert {m.message_id for m in tracked} == {mid for _, mid in messenger.messages}


async def test_unchanged_panel_makes_no_calls(
    reconciler: PanelReconciler[EmbedData], messenger: FakeMessenger
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)
    messenger.reset_calls()

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)

    assert result == ReconcileResult(unchanged=3)
    assert messenger.calls == []


async def test_only_changed_messages_are_edited(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b", "c"), 5)
    before = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    messenger.reset_calls()

    result = await reconciler.reconcile(
        GUILD, CHANNEL, embed("o"), cards("a", "B2", "c"), 5
    )

    assert result == ReconcileResult(edited=1, unchanged=3)
    assert messenger.calls == [("edit", CHANNEL, before[1].message_id)]
    after = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    assert after[1].message_id == before[1].message_id
    assert after[1].payload_hash == hash_payload(embed("B2"))
    assert after[0] == before[0]
    assert after[2] == before[2]
    assert messenger.titles_in(CHANNEL) == ["o", "a", "B2", "c"]


async def test_changed_overview_is_edited_alone(
    reconciler: PanelReconciler[EmbedData], messenger: FakeMessenger
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o", "v1"), cards("a"), 5)
    messenger.reset_calls()

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o", "v2"), cards("a"), 5)

    assert result == ReconcileResult(edited=1, unchanged=1)
    assert [call[0] for call in messenger.calls] == ["edit"]


async def test_deleted_message_is_recreated(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)
    before = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    del messenger.messages[(CHANNEL, before[0].message_id)]
    messenger.reset_calls()

    result = await reconciler.reconcile(
        GUILD, CHANNEL, embed("o"), cards("a-new", "b"), 5
    )

    assert result == ReconcileResult(recreated=1, unchanged=2)
    after = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    assert after[0].message_id != before[0].message_id
    assert after[0].payload_hash == hash_payload(embed("a-new"))
    assert after[0].message_id in {mid for _, mid in messenger.messages}
    assert after[1] == before[1]
    assert sorted(messenger.titles_in(CHANNEL)) == ["a-new", "b", "o"]


async def test_deleted_message_with_unchanged_payload_is_not_noticed(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    # Matching hashes mean no API call, so a manual deletion only surfaces
    # once the content changes. This pins down that documented trade-off.
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a"), 5)
    tracked = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    del messenger.messages[(CHANNEL, tracked[0].message_id)]
    messenger.reset_calls()

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a"), 5)

    assert result == ReconcileResult(unchanged=2)
    assert messenger.calls == []


async def test_deleted_overview_is_recreated(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o", "v1"), cards("a"), 5)
    tracked = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    del messenger.messages[(CHANNEL, tracked[OVERVIEW_SLOT].message_id)]

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o", "v2"), cards("a"), 5)

    assert result == ReconcileResult(recreated=1, unchanged=1)
    assert "o" in messenger.titles_in(CHANNEL)


async def test_channel_change_removes_old_messages_first(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, OLD_CHANNEL, embed("o"), cards("a", "b"), 5)
    old_ids = {mid for _, mid in messenger.messages}
    messenger.reset_calls()

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)

    assert result == ReconcileResult(created=3, deleted=3, migrated=True)
    assert messenger.titles_in(OLD_CHANNEL) == []
    assert messenger.titles_in(CHANNEL) == ["o", "a", "b"]
    kinds = [call[0] for call in messenger.calls]
    assert kinds == ["delete"] * 3 + ["send"] * 3
    assert {call[2] for call in messenger.kinds("delete")} == old_ids
    assert all(call[1] == OLD_CHANNEL for call in messenger.kinds("delete"))
    tracked = await repo.get_panel_messages(GUILD)
    assert [m.slot for m in tracked] == [OVERVIEW_SLOT, 0, 1]
    assert all(m.channel_id == CHANNEL for m in tracked)
    assert not {m.message_id for m in tracked} & old_ids


async def test_channel_change_tolerates_missing_and_forbidden_old_messages(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, OLD_CHANNEL, embed("o"), cards("a", "b"), 5)
    old = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    del messenger.messages[(OLD_CHANNEL, old[0].message_id)]
    messenger.forbidden_deletes.add(old[1].message_id)

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)

    assert result.migrated
    assert messenger.titles_in(CHANNEL) == ["o", "a", "b"]
    assert all(m.channel_id == CHANNEL for m in await repo.get_panel_messages(GUILD))


async def test_channel_change_does_not_touch_other_guilds(
    reconciler: PanelReconciler[EmbedData], repo: Repository
) -> None:
    other = 200
    await repo.upsert_guild(GuildConfig(guild_id=other))
    await reconciler.reconcile(other, OLD_CHANNEL, embed("o"), cards("a"), 5)
    await reconciler.reconcile(GUILD, OLD_CHANNEL, embed("o"), cards("a"), 5)

    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a"), 5)

    assert {m.channel_id for m in await repo.get_panel_messages(other)} == {OLD_CHANNEL}


async def test_fewer_games_delete_surplus_cards(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b", "c", "d"), 5)
    before = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    messenger.reset_calls()

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)

    assert result == ReconcileResult(unchanged=3, deleted=2)
    assert await slots(repo) == [OVERVIEW_SLOT, 0, 1]
    assert {call[2] for call in messenger.kinds("delete")} == {
        before[2].message_id,
        before[3].message_id,
    }
    assert messenger.titles_in(CHANNEL) == ["o", "a", "b"]


async def test_lower_panel_count_deletes_surplus_cards(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b", "c"), 3)

    result = await reconciler.reconcile(
        GUILD, CHANNEL, embed("o"), cards("a", "b", "c"), 1
    )

    assert result == ReconcileResult(unchanged=2, deleted=2)
    assert await slots(repo) == [OVERVIEW_SLOT, 0]
    assert messenger.titles_in(CHANNEL) == ["o", "a"]


async def test_no_games_leaves_only_the_overview(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o", "v1"), cards("a", "b"), 5)

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o", "empty"), [], 5)

    assert result == ReconcileResult(edited=1, deleted=2)
    assert await slots(repo) == [OVERVIEW_SLOT]
    assert len(messenger.messages) == 1


async def test_surplus_already_gone_is_still_untracked(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)
    tracked = {m.slot: m for m in await repo.get_panel_messages(GUILD)}
    del messenger.messages[(CHANNEL, tracked[1].message_id)]
    messenger.forbidden_deletes.add(tracked[0].message_id)

    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), [], 5)

    assert await slots(repo) == [OVERVIEW_SLOT]


async def test_growth_appends_new_cards(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a"), 5)
    messenger.reset_calls()

    result = await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)

    assert result == ReconcileResult(created=1, unchanged=2)
    assert [call[0] for call in messenger.calls] == ["send"]
    assert await slots(repo) == [OVERVIEW_SLOT, 0, 1]
    assert messenger.titles_in(CHANNEL) == ["o", "a", "b"]


async def test_cards_beyond_panel_count_are_not_posted(
    reconciler: PanelReconciler[EmbedData], repo: Repository
) -> None:
    result = await reconciler.reconcile(
        GUILD, CHANNEL, embed("o"), cards("a", "b", "c"), 2
    )

    assert result == ReconcileResult(created=3)
    assert await slots(repo) == [OVERVIEW_SLOT, 0, 1]


async def test_failed_send_keeps_earlier_progress(
    repo: Repository, messenger: FakeMessenger
) -> None:
    class Flaky(FakeMessenger):
        async def send(self, channel_id: int, payload: EmbedData) -> int:
            if payload.title == "b":
                raise MessageForbidden
            return await super().send(channel_id, payload)

    flaky = Flaky()
    reconciler = PanelReconciler(repo, flaky)

    with pytest.raises(MessageForbidden):
        await reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)

    assert await slots(repo) == [OVERVIEW_SLOT, 0]
    flaky_ok = PanelReconciler(repo, messenger)
    messenger.messages.update(flaky.messages)
    result = await flaky_ok.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)
    assert result == ReconcileResult(created=1, unchanged=2)


async def test_concurrent_passes_for_a_guild_do_not_duplicate(
    reconciler: PanelReconciler[EmbedData],
    repo: Repository,
    messenger: FakeMessenger,
) -> None:
    results = await asyncio.gather(
        *(
            reconciler.reconcile(GUILD, CHANNEL, embed("o"), cards("a", "b"), 5)
            for _ in range(4)
        )
    )

    assert len(messenger.messages) == 3
    assert sum(r.created for r in results) == 3
    assert await slots(repo) == [OVERVIEW_SLOT, 0, 1]
