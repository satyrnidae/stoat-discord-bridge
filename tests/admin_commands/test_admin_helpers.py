from stoat_discord_bridge.admin_commands import LinkedMember, collect_linked_members, format_linked_listing, pop_kv_option
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

