from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, EmoteLinker, UserLinker


async def _none(_id):
    return None


def test_connector_info_capability_flags_follow_the_wired_hooks():
    irc = ConnectorInfo(id="irc", label="IRC")
    assert not irc.supports_roles
    assert not irc.supports_categories
    assert not irc.supports_emotes

    full = ConnectorInfo(
        id="discord",
        label="Discord",
        resolve_role_name=_none,
        resolve_category_name=_none,
        resolve_emoji_name=_none,
    )
    assert full.supports_roles
    assert full.supports_categories
    assert full.supports_emotes


def test_connector_info_name_limits_default_to_none():
    # issue #99: `None` means "no bridge-side clip" - the /mirror linkers skip
    # clipping entirely for such a connector.
    bare = ConnectorInfo(id="x", label="X")
    assert bare.channel_name_limit is None
    assert bare.category_name_limit is None
    assert bare.role_name_limit is None


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
