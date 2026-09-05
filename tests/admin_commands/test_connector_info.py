from stoat_discord_bridge.admin_commands import ConnectorInfo


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
