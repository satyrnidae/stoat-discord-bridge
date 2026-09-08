from __future__ import annotations

import pytest

from stoat_discord_bridge.models import StandardEdit
from stoat_discord_bridge.services.base import PartialRelayError
from stoat_discord_bridge.services.stoat_service import StoatReceiverService
from tests.fakes.fake_stoat import FakeChannel, FakeClient
from tests.stoat_receiver.conftest import _FakeSender, _make_receiver, _message


# ---------------------------------------------------------------- edit_message()


def _edit(**overrides) -> StandardEdit:
    defaults = dict(
        origin_connector_id="discord",
        origin_channel_id="d-100",
        origin_message_id="m1",
        new_content_markdown="edited text",
    )
    defaults.update(overrides)
    return StandardEdit(**defaults)


async def test_edit_message_edits_the_relayed_masqueraded_post():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.edit_message(target_channel_id="42", target_message_ids=["7"], edit=_edit())

    assert (await channel.fetch_message("7")).edits == ["edited text"]


async def test_edit_message_blanks_posts_a_shortened_edit_no_longer_fills():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.edit_message(
        target_channel_id="42", target_message_ids=["7", "8"], edit=_edit(new_content_markdown="short")
    )

    assert (await channel.fetch_message("7")).edits == ["short"]
    assert (await channel.fetch_message("8")).edits == ["​"]


# ---------------------------------------------------------------- receive()


async def test_receive_posts_through_masquerade():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    ids = await receiver.receive(_message(), target_channel_id="42")

    assert channel.sent == [{"content": "hello", "masquerade": channel.sent[0]["masquerade"]}]
    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Alice"
    assert masquerade.avatar == "https://cdn.example/alice.png"
    assert ids == ["1"]


async def test_receive_truncates_a_sender_name_over_32_chars():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(_message(sender_name="x" * 40), target_channel_id="42")

    assert channel.sent[0]["masquerade"].name == "x" * 32


async def test_receive_folds_the_source_label_into_the_masquerade_name():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(_message(source_label="Discord"), target_channel_id="42")

    assert channel.sent[0]["masquerade"].name == "Alice [Discord]"


async def test_receive_drops_the_source_suffix_when_it_would_overflow_the_32_char_cap():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(sender_name="a-fairly-long-display-name", source_label="Stoat (public)"),
        target_channel_id="42",
    )

    # the "[Stoat (public)]" suffix won't fit, so it's dropped whole rather
    # than sliced mid-token into a dangling "[".
    name = channel.sent[0]["masquerade"].name
    assert name == "a-fairly-long-display-name"
    assert "[" not in name


async def test_receive_omits_the_source_label_when_source_forwarding_is_off():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = StoatReceiverService(_FakeSender(client), source_forwarding=False)

    await receiver.receive(_message(source_label="Discord"), target_channel_id="42")

    assert channel.sent[0]["masquerade"].name == "Alice"


async def test_receive_folds_source_and_pronouns_into_the_masquerade_name():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(source_label="Discord", sender_pronouns="she/her"), target_channel_id="42"
    )

    assert channel.sent[0]["masquerade"].name == "Alice [Discord, she/her]"


async def test_receive_omits_pronouns_when_pronoun_forwarding_is_off():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = StoatReceiverService(_FakeSender(client), pronoun_forwarding=False)

    await receiver.receive(
        _message(source_label="Discord", sender_pronouns="she/her"), target_channel_id="42"
    )

    assert channel.sent[0]["masquerade"].name == "Alice [Discord]"


async def test_receive_forwards_the_sender_color_onto_the_masquerade():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(_message(sender_color="#5865f2"), target_channel_id="42")

    assert channel.sent[0]["masquerade"].color == "#5865f2"


async def test_receive_omits_the_color_when_color_forwarding_is_off():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = StoatReceiverService(_FakeSender(client), color_forwarding=False)

    await receiver.receive(_message(sender_color="#5865f2"), target_channel_id="42")

    assert channel.sent[0]["masquerade"].color is None


async def test_receive_retries_uncolored_when_a_colored_send_is_rejected(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT", 5)

    real_send = channel.send
    seen: list = []

    async def picky_send(content, *, masquerade=None, attachments=None):
        seen.append(masquerade.color)
        if masquerade.color is not None:
            raise RuntimeError("Missing permission: ManageRole")
        return await real_send(content, masquerade=masquerade)

    channel.send = picky_send

    ids = await receiver.receive(
        _message(content_markdown="abcdefghij", sender_color="#5865f2"), target_channel_id="42"
    )

    # first chunk: colored attempt rejected, uncolored retry succeeds; the
    # second chunk then goes straight out uncolored (no wasted retry).
    assert seen == ["#5865f2", None, None]
    assert ids == ["1", "2"]


async def test_receive_splits_long_content_into_multiple_sends(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT", 5)

    ids = await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    assert [call["content"] for call in channel.sent] == ["abcde", "fghij"]
    assert ids == ["1", "2"]


async def test_receive_raises_partial_relay_error_and_keeps_ids_already_sent(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT", 5)

    # first chunk should succeed before the second fails.
    real_send = channel.send
    call_count = 0

    async def flaky_send(content, *, masquerade=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("rate limited")
        return await real_send(content, masquerade=masquerade)

    channel.send = flaky_send

    with pytest.raises(PartialRelayError) as exc_info:
        await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    assert exc_info.value.partial_ids == ["1"]


