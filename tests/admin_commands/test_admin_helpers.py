import pytest

from stoat_discord_bridge.admin_commands import (
    ConnectorInfo,
    LinkedMember,
    LinkError,
    collect_linked_members,
    format_linked_listing,
    pop_kv_option,
)
from stoat_discord_bridge.admin_commands.common import (
    _BULK_ENTITY_BATCH_SIZE,
    _is_all_token,
    _list_entities_for_all,
    _run_bulk_mirror,
)
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping


def test_pop_kv_option_pulls_the_first_matching_pair_out():
    remaining, value = pop_kv_option(["stoat", "general", "category:01ABC", "extra"], "category")
    assert remaining == ["stoat", "general", "extra"]
    assert value == "01ABC"


def test_pop_kv_option_is_case_insensitive_on_the_key_and_accepts_equals():
    remaining, value = pop_kv_option(["a", "CATEGORY=Bot Stuff"], "category")
    assert remaining == ["a"]
    assert value == "Bot Stuff"


def test_pop_kv_option_absent_returns_none_and_all_tokens():
    remaining, value = pop_kv_option(["stoat", "general"], "category")
    assert remaining == ["stoat", "general"]
    assert value is None


def test_pop_kv_option_reassembles_a_quoted_multi_word_value():
    remaining, value = pop_kv_option(["stoat", 'category:"Off', "Topic", 'Zone"', "lobby"], "category")
    assert remaining == ["stoat", "lobby"]
    assert value == "Off Topic Zone"


def test_pop_kv_option_single_token_quoted_value_is_unwrapped():
    remaining, value = pop_kv_option(["a", "category:'Ideas'"], "category")
    assert remaining == ["a"]
    assert value == "Ideas"


# ---------------------------------------------------------------- collect_linked_members


async def test_collect_linked_members_reads_name_off_the_mapping(connectors):
    mappings = [
        ChannelMapping(bridge_group="g1", connector_id="stoat", channel_id="s1", channel_name="general"),
        ChannelMapping(bridge_group="g1", connector_id="discord", channel_id="d1", channel_name="general-chat"),
    ]
    members = await collect_linked_members(mappings, connectors, "channel_id", "channel_name")
    assert members == [
        LinkedMember(connector_id="discord", label="Discord", entity_id="d1", name="general-chat"),
        LinkedMember(connector_id="stoat", label="Stoat", entity_id="s1", name="general"),
    ]


async def test_collect_linked_members_resolves_name_live_when_given_a_hook(connectors):
    mappings = [ChannelMapping(bridge_group="g1", connector_id="stoat", channel_id="s1", channel_name="stale")]

    async def resolve_name(connector_id, entity_id):
        return f"{connector_id}:{entity_id}:fresh"

    members = await collect_linked_members(mappings, connectors, "channel_id", resolve_name=resolve_name)
    assert members == [LinkedMember(connector_id="stoat", label="Stoat", entity_id="s1", name="stoat:s1:fresh")]


async def test_collect_linked_members_falls_back_to_connector_id_for_an_unknown_connector():
    mappings = [ChannelMapping(bridge_group="g1", connector_id="webchat", channel_id="w1", channel_name="general")]
    members = await collect_linked_members(mappings, {}, "channel_id", "channel_name")
    assert members == [LinkedMember(connector_id="webchat", label="webchat", entity_id="w1", name="general")]


async def test_format_linked_listing_matches_collect_linked_members_output(connectors):
    mappings = [
        ChannelMapping(bridge_group="g1", connector_id="stoat", channel_id="s1", channel_name="general"),
        ChannelMapping(bridge_group="g1", connector_id="discord", channel_id="d1", channel_name="general-chat"),
    ]
    lines = await format_linked_listing(
        mappings, connectors, "channel_id", "channel_name", marker_for=("stoat", "s1"), marker_text=" (this channel)"
    )
    assert lines == ["Discord: general-chat (d1)", "Stoat: general (s1) (this channel)"]


# ---------------------------------------------------------------- entity-level `all` (issue #123)


def test_is_all_token_omitted_argument_is_never_all():
    # point 3 of the issue #123 plan: an omitted argument must never be
    # reinterpreted as `all` - only an explicit typed token counts.
    assert _is_all_token(None) is False


def test_is_all_token_matches_case_insensitively():
    assert _is_all_token("all") is True
    assert _is_all_token("ALL") is True
    assert _is_all_token("All") is True


def test_is_all_token_rejects_a_real_name():
    assert _is_all_token("general") is False
    assert _is_all_token("") is False


async def test_list_entities_for_all_raises_when_the_connector_has_no_list_hook():
    connectors = {"irc": ConnectorInfo(id="irc", label="IRC")}
    with pytest.raises(LinkError, match="doesn't support listing categorys"):
        await _list_entities_for_all(connectors, "irc", None, kind="category")


async def test_list_entities_for_all_raises_when_the_hook_raises():
    async def boom():
        raise RuntimeError("network down")

    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    with pytest.raises(LinkError, match="couldn't list channels"):
        await _list_entities_for_all(connectors, "discord", boom, kind="channel")


async def test_list_entities_for_all_returns_more_than_the_batch_size_worth_of_entities():
    # issue #157: a large `all` fan-out is batched, not rejected outright.
    async def many():
        return [(str(i), f"chan-{i}") for i in range(_BULK_ENTITY_BATCH_SIZE + 1)]

    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    entities = await _list_entities_for_all(connectors, "discord", many, kind="channel")
    assert len(entities) == _BULK_ENTITY_BATCH_SIZE + 1


async def test_list_entities_for_all_returns_the_hooks_list():
    async def list_channels():
        return [("c1", "general"), ("c2", "random")]

    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    entities = await _list_entities_for_all(connectors, "discord", list_channels, kind="channel")
    assert entities == [("c1", "general"), ("c2", "random")]


async def test_run_bulk_mirror_with_no_entities_reports_nothing_to_mirror():
    async def mirror_one(entity_id, entity_name):
        raise AssertionError("must not be called")

    summary = await _run_bulk_mirror([], mirror_one, pacing_seconds=0)
    assert "nothing to mirror" in summary.lower()


async def test_run_bulk_mirror_runs_every_entity_and_joins_the_results():
    seen = []

    async def mirror_one(entity_id, entity_name):
        seen.append((entity_id, entity_name))
        return f"mirrored {entity_name}"

    summary = await _run_bulk_mirror(
        [("c1", "general"), ("c2", "random")], mirror_one, pacing_seconds=0
    )
    assert seen == [("c1", "general"), ("c2", "random")]
    assert summary == "mirrored general\nmirrored random"


async def test_run_bulk_mirror_reports_a_per_entity_linkerror_instead_of_aborting():
    async def mirror_one(entity_id, entity_name):
        if entity_id == "c1":
            raise LinkError("boom")
        return f"mirrored {entity_name}"

    summary = await _run_bulk_mirror(
        [("c1", "general"), ("c2", "random")], mirror_one, pacing_seconds=0
    )
    lines = summary.splitlines()
    assert len(lines) == 2
    assert "'general': boom" in lines[0]
    assert "mirrored random" in lines[1]


async def test_run_bulk_mirror_reports_a_non_linkerror_instead_of_aborting():
    # mirror_one's own per-connector-problem paths always report via
    # LinkError or a caught-and-reported string, but a genuinely unexpected
    # exception (a bug, a network error that slipped past an inner
    # try/except) must still be contained to its own entity's line rather
    # than aborting the whole bulk fan-out.
    async def mirror_one(entity_id, entity_name):
        if entity_id == "c1":
            raise RuntimeError("kaboom")
        return f"mirrored {entity_name}"

    summary = await _run_bulk_mirror(
        [("c1", "general"), ("c2", "random")], mirror_one, pacing_seconds=0
    )
    lines = summary.splitlines()
    assert len(lines) == 2
    assert "'general': kaboom" in lines[0]
    assert "mirrored random" in lines[1]


async def test_run_bulk_mirror_paces_between_entities_not_before_the_first(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("stoat_discord_bridge.admin_commands.common.asyncio.sleep", fake_sleep)

    async def mirror_one(entity_id, entity_name):
        return "ok"

    await _run_bulk_mirror([("c1", "a"), ("c2", "b"), ("c3", "c")], mirror_one, pacing_seconds=0.5)
    assert sleeps == [0.5, 0.5]


async def test_run_bulk_mirror_paces_longer_between_batches_than_within_them(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("stoat_discord_bridge.admin_commands.common.asyncio.sleep", fake_sleep)

    async def mirror_one(entity_id, entity_name):
        return "ok"

    entities = [(str(i), f"e{i}") for i in range(5)]
    await _run_bulk_mirror(entities, mirror_one, pacing_seconds=0.1, batch_size=2, batch_pacing_seconds=9.0)
    assert sleeps == [0.1, 9.0, 0.1, 9.0]

