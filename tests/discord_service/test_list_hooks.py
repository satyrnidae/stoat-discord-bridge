from __future__ import annotations

from types import SimpleNamespace

from tests.discord_service.conftest import FakeLinker, _make_sender


# ------------------------------------------------ DiscordLookupsMixin.list_* (autocomplete data source)


async def test_list_roles_skips_the_default_everyone_role(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = SimpleNamespace(
        roles=[
            SimpleNamespace(id=1, name="@everyone", is_default=lambda: True),
            SimpleNamespace(id=2, name="Admins", is_default=lambda: False),
        ]
    )
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    assert await sender.list_roles() == [("2", "Admins")]


async def test_list_channels_covers_text_and_voice(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = SimpleNamespace(
        text_channels=[SimpleNamespace(id=10, name="general")],
        voice_channels=[SimpleNamespace(id=20, name="Lounge")],
    )
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    assert await sender.list_channels() == [("10", "general"), ("20", "Lounge")]


async def test_list_hooks_return_empty_when_the_guild_is_uncached(monkeypatch):
    sender = _make_sender(FakeLinker())
    monkeypatch.setattr(sender, "_guild_or_none", lambda: None)

    assert await sender.list_roles() == []
    assert await sender.list_channels() == []
    assert await sender.list_categories() == []
    assert await sender.list_users() == []
    assert await sender.list_emotes() == []


# ---------------------------------------------------------------- _handle_ready member chunking (issue #80)


class _ChunkGuild:
    def __init__(self, chunked: bool, *, raises: bool = False) -> None:
        self.chunked = chunked
        self._raises = raises
        self.chunk_calls = 0

    async def chunk(self):
        self.chunk_calls += 1
        if self._raises:
            raise RuntimeError("no members intent")
        self.chunked = True


async def test_handle_ready_chunks_the_member_roster(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = _ChunkGuild(chunked=False)
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    async def _synced(*a, **k):
        return []

    monkeypatch.setattr(sender.tree, "sync", _synced)

    await sender._handle_ready()

    assert guild.chunk_calls == 1


async def test_handle_ready_skips_chunk_when_already_chunked(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = _ChunkGuild(chunked=True)
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    async def _synced(*a, **k):
        return []

    monkeypatch.setattr(sender.tree, "sync", _synced)

    await sender._handle_ready()

    assert guild.chunk_calls == 0


async def test_handle_ready_swallows_a_failing_chunk(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = _ChunkGuild(chunked=False, raises=True)
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    synced = []

    async def _synced(*a, **k):
        synced.append(True)
        return []

    monkeypatch.setattr(sender.tree, "sync", _synced)

    await sender._handle_ready()  # must not raise

    assert guild.chunk_calls == 1
    assert synced == [True]  # ready handler carried on to the command sync


