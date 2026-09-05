import pytest

from stoat_discord_bridge.admin_commands import ConnectorInfo, LinkError, UserLinker
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository


# ---------------------------------------------------------------- UserLinker.link_user


async def test_link_user_creates_a_new_group(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111"
    )
    assert "Linked Discord user '111' to IRC user 'Alice'" in summary


async def test_link_user_to_themselves_raises(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="themselves"):
        await linker.link_user(local_connector="discord", local_user_id="111", source="discord", source_user_id="111")


async def test_link_user_strips_a_pasted_discord_mention(fake_db, connectors):
    # Stoat/IRC's /link-user has no member-picker (unlike Discord's) - a
    # Discord id typed/pasted there often arrives as a full "<@id>" mention
    # rather than the bare snowflake.
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="<@216591124222050304>"
    )
    assert "Linked Discord user '216591124222050304' to IRC user 'Alice'" in summary


async def test_link_user_strips_a_pasted_discord_nickname_mention(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="<@!216591124222050304>"
    )
    assert "Linked Discord user '216591124222050304' to IRC user 'Alice'" in summary


async def test_link_user_conflicting_groups_raises(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")
    await linker.link_user(local_connector="irc", local_user_id="Bob", source="discord", source_user_id="222")

    with pytest.raises(LinkError, match="different link groups"):
        await linker.link_user(local_connector="irc", local_user_id="Bob", source="discord", source_user_id="111")


def _name_resolving_connectors(**overrides):
    async def d_by_name(token):
        return {"alice": "111", "bob": "222"}.get(token.casefold())

    async def s_by_name(token):
        return {"shriner": "01KH"}.get(token.casefold())

    base = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_user_id_by_name=d_by_name),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", resolve_user_id_by_name=s_by_name),
        "irc": ConnectorInfo(id="irc", label="IRC"),  # no hook - a nick already IS the id
    }
    base.update(overrides)
    return base


async def test_link_user_resolves_display_names_on_both_sides(fake_db):
    linker = UserLinker(UserMappingRepository(fake_db), _name_resolving_connectors())
    summary = await linker.link_user(
        local_connector="stoat", local_user_id="Shriner", source="discord", source_user_id="Alice"
    )
    assert "Linked Discord user '111' to Stoat user '01KH'." == summary


async def test_link_user_falls_back_to_the_literal_token_when_the_name_is_unknown(fake_db):
    linker = UserLinker(UserMappingRepository(fake_db), _name_resolving_connectors())
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="999"
    )
    assert "Linked Discord user '999' to IRC user 'Alice'." == summary


async def test_unlink_user_resolves_a_display_name(fake_db):
    connectors = _name_resolving_connectors()
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="stoat", local_user_id="01KH", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="discord", local_user_id="Alice", destination="all")
    assert "entire link group" in summary


async def test_list_linked_users_resolves_a_display_name_target(fake_db):
    connectors = _name_resolving_connectors()
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="stoat", local_user_id="01KH", source="discord", source_user_id="111")

    summary = await linker.list_linked_users(local_connector="discord", local_user_id="Alice")
    assert "Discord" in summary and "Stoat" in summary


# ---------------------------------------------------------------- UserLinker.list_linked_users


async def test_list_linked_users_reports_unlinked(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.list_linked_users(local_connector="discord", local_user_id="111")
    assert summary == "This user isn't linked to any others."


async def test_list_linked_users_reports_none_linked_at_all(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    assert await linker.list_linked_users() == "No users are linked yet."


async def test_list_linked_users_resolves_real_names_live(fake_db):
    async def discord_name(user_id):
        return {"216591124222050304": "ShrinerH"}.get(user_id)

    async def stoat_name(user_id):
        return {"01KH7TH31EBY08FTQ7YC2RC4DQ": "shriner"}.get(user_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_user_name=discord_name),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", resolve_user_name=stoat_name),
    }
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(
        local_connector="discord", local_user_id="216591124222050304",
        source="stoat", source_user_id="01KH7TH31EBY08FTQ7YC2RC4DQ",
    )

    summary = await linker.list_linked_users(local_connector="discord", local_user_id="216591124222050304")

    assert "Discord: ShrinerH (216591124222050304)" in summary
    assert "Stoat: shriner (01KH7TH31EBY08FTQ7YC2RC4DQ)" in summary


async def test_list_linked_users_falls_back_to_the_raw_id_when_unresolvable(fake_db):
    async def failing_resolver(user_id):
        raise RuntimeError("boom")

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_user_name=failing_resolver),
        "irc": ConnectorInfo(id="irc", label="IRC"),  # no resolver at all - IRC's id already IS the name
    }
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="discord", local_user_id="111", source="irc", source_user_id="Alice")

    summary = await linker.list_linked_users(local_connector="discord", local_user_id="111")

    # no redundant "(id)" suffix when the resolved name IS the id (fallback
    # or, for IRC, the id always being the display name to begin with)
    assert "Discord: 111" in summary
    assert "Discord: 111 (111)" not in summary
    assert "IRC: Alice" in summary
    assert "(Alice)" not in summary


async def test_list_linked_users_with_no_target_lists_every_group(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="discord", local_user_id="111", source="stoat", source_user_id="s1")
    await linker.link_user(local_connector="discord", local_user_id="222", source="irc", source_user_id="Bob")

    summary = await linker.list_linked_users()

    lines = summary.splitlines()[1:]  # drop the "Linked users:" header
    assert len(lines) == 2
    assert any("111" in line and "s1" in line for line in lines)
    assert any("222" in line and "Bob" in line for line in lines)


# ---------------------------------------------------------------- UserLinker.unlink_user


async def test_unlink_user_unlinked_user_raises(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't linked"):
        await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination=None)


async def test_unlink_user_unknown_destination_raises(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    with pytest.raises(LinkError, match="isn't linked in this user's link group"):
        await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination="stoat")


async def test_unlink_user_specific_destination_kicks_only_that_member(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")
    await linker.link_user(local_connector="stoat", local_user_id="s1", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination="discord")

    assert "Unlinked Discord user '111'" in summary
    remaining = await linker.list_linked_users(local_connector="irc", local_user_id="Alice")
    assert "Discord" not in remaining
    assert "Stoat: s1" in remaining


async def test_unlink_user_all_dissolves_the_whole_group(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination="all")

    assert "2 identity/identities removed" in summary
    assert await user_mappings.get_link_group("irc", "Alice") is None
    assert await user_mappings.get_link_group("discord", "111") is None


async def test_unlink_user_defaults_to_all(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination=None)

    assert await user_mappings.get_link_group("discord", "111") is None


async def test_unlink_user_strips_a_pasted_discord_mention(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="discord", local_user_id="<@111>", destination="all")

    assert "removed" in summary
    assert await user_mappings.get_link_group("irc", "Alice") is None