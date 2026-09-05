from stoat_discord_bridge.admin_commands import ChannelLinker, EmoteLinker, UserLinker, pop_kv_option


# ---------------------------------------------------------------- .connectors (Discord autocomplete)


def test_channel_linker_exposes_the_connectors_it_was_given(connectors):
    linker = ChannelLinker(channel_mappings=None, connectors=connectors)
    assert linker.connectors == connectors


def test_emote_linker_exposes_the_connectors_it_was_given(connectors):
    linker = EmoteLinker(emoji_mappings=None, connectors=connectors)
    assert linker.connectors == connectors


def test_user_linker_exposes_the_connectors_it_was_given(connectors):
    linker = UserLinker(user_mappings=None, connectors=connectors)
    assert linker.connectors == connectors




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

