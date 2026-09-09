from stoat_discord_bridge.channel_structure import (
    FORUM_CATEGORY_PREFIX,
    THREAD_CATEGORY_PREFIX,
    clip_name,
    forum_category_title,
    strip_thread_category_prefix,
    thread_category_title,
)


def test_clip_name_strips_whitespace():
    assert clip_name("  general  ") == "general"


def test_clip_name_truncates_to_32_chars():
    long_name = "a" * 50
    clipped = clip_name(long_name)
    assert len(clipped) == 32
    assert clipped == "a" * 32


def test_clip_name_strips_then_truncates():
    assert clip_name("  " + "a" * 40 + "  ") == "a" * 32


def test_clip_name_respects_a_passed_limit():
    assert clip_name("a" * 40, 10) == "a" * 10
    assert clip_name("  " + "a" * 40 + "  ", 10) == "a" * 10


def test_clip_name_limit_defaults_to_32():
    assert clip_name("a" * 40) == clip_name("a" * 40, 32) == "a" * 32


def test_clip_name_shorter_than_limit_is_untouched():
    assert clip_name("general", 100) == "general"


def test_thread_category_title_prefixes_with_the_marker():
    assert thread_category_title("general") == "🧵 #general"
    assert thread_category_title("  general  ") == "🧵 #general"


def test_thread_category_title_clips_after_prefixing():
    clipped = thread_category_title("a" * 50)
    assert len(clipped) == 32
    assert clipped.startswith(THREAD_CATEGORY_PREFIX)


def test_forum_category_title_prefixes_with_the_forum_marker():
    assert forum_category_title("ttrpg-forum") == "💬 #ttrpg-forum"
    assert forum_category_title("  ttrpg-forum  ") == "💬 #ttrpg-forum"


def test_forum_category_title_clips_after_prefixing():
    clipped = forum_category_title("a" * 50)
    assert len(clipped) == 32
    assert clipped.startswith(FORUM_CATEGORY_PREFIX)


def test_strip_thread_category_prefix_round_trips():
    assert strip_thread_category_prefix(thread_category_title("general")) == "general"
    assert strip_thread_category_prefix(forum_category_title("general")) == "general"


def test_strip_thread_category_prefix_leaves_an_unprefixed_title_alone():
    assert strip_thread_category_prefix("general") == "general"
