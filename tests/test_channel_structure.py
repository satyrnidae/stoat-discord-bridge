from stoat_discord_bridge.channel_structure import ChannelSpec, GroupSpec, GuildStructure, clip_name


def test_clip_name_strips_whitespace():
    assert clip_name("  general  ") == "general"


def test_clip_name_truncates_to_32_chars():
    long_name = "a" * 50
    clipped = clip_name(long_name)
    assert len(clipped) == 32
    assert clipped == "a" * 32


def test_clip_name_strips_then_truncates():
    assert clip_name("  " + "a" * 40 + "  ") == "a" * 32


def test_guild_structure_defaults_are_empty():
    structure = GuildStructure()
    assert structure.groups == []
    assert structure.ungrouped_channels == []


def test_group_spec_holds_its_channels():
    spec = GroupSpec(name="Text Channels", channels=[ChannelSpec(name="general", source_channel_id="123")])
    assert spec.channels[0].name == "general"
    assert spec.channels[0].source_channel_id == "123"
