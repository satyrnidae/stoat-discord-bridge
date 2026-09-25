from __future__ import annotations

import discord

from tests.discord_receiver.conftest import _make_receiver
from tests.fakes.fake_discord import FakeClient


class FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeEmoji:
    def __init__(self, id: int, name: str, *, edit_error: BaseException | None = None) -> None:
        self.id = id
        self.name = name
        self.edits: list[dict] = []
        self._edit_error = edit_error

    async def edit(self, **kwargs) -> "FakeEmoji":
        if self._edit_error is not None:
            raise self._edit_error
        self.edits.append(kwargs)
        self.name = kwargs["name"]
        return self


async def test_rename_emoji_renames_the_emoji():
    client = FakeClient()
    emoji = client.add_emoji(7, FakeEmoji(7, "pog"))
    receiver = _make_receiver(client)

    applied = await receiver.rename_emoji(target_emoji_id="7", new_name="poggers")

    assert emoji.edits == [{"name": "poggers", "reason": "bridge emote rename sync"}]
    assert applied == "poggers"


async def test_rename_emoji_is_a_noop_when_already_named_that():
    client = FakeClient()
    emoji = client.add_emoji(7, FakeEmoji(7, "pog"))
    receiver = _make_receiver(client)

    applied = await receiver.rename_emoji(target_emoji_id="7", new_name="pog")

    assert emoji.edits == []
    assert applied == "pog"


async def test_rename_emoji_sanitizes_the_name_to_discords_rules():
    client = FakeClient()
    emoji = client.add_emoji(7, FakeEmoji(7, "pog"))
    receiver = _make_receiver(client)

    applied = await receiver.rename_emoji(target_emoji_id="7", new_name="big pog!")

    assert emoji.name == "big_pog"
    assert applied == "big_pog"


async def test_rename_emoji_swallows_an_edit_failure():
    client = FakeClient()
    emoji = client.add_emoji(7, FakeEmoji(7, "pog", edit_error=discord.HTTPException(FakeResponse(), "nope")))
    receiver = _make_receiver(client)

    applied = await receiver.rename_emoji(target_emoji_id="7", new_name="poggers")  # must not raise

    assert emoji.name == "pog"
    assert applied is None


async def test_rename_emoji_returns_none_for_an_unknown_emoji():
    receiver = _make_receiver(FakeClient())

    assert await receiver.rename_emoji(target_emoji_id="7", new_name="poggers") is None


async def test_rename_emoji_returns_none_for_a_non_numeric_id():
    receiver = _make_receiver(FakeClient())

    assert await receiver.rename_emoji(target_emoji_id="01ABCSTOATULID", new_name="poggers") is None


def test_discord_receiver_advertises_emoji_rename_support():
    assert _make_receiver(FakeClient()).supports_emoji_rename is True
