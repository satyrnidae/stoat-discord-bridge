from stoat_discord_bridge.channel_structure import clip_name


def test_clip_name_strips_whitespace():
    assert clip_name("  general  ") == "general"


def test_clip_name_truncates_to_32_chars():
    long_name = "a" * 50
    clipped = clip_name(long_name)
    assert len(clipped) == 32
    assert clipped == "a" * 32


def test_clip_name_strips_then_truncates():
    assert clip_name("  " + "a" * 40 + "  ") == "a" * 32
