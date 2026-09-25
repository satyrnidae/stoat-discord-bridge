"""`services.formatting.partition_link_preview_attachments` (issue #164) - the
per-receiver choice between re-uploading the source's resolved link-preview
media and leaving the raw link for the destination to unfurl itself."""

from stoat_discord_bridge.models import Attachment
from stoat_discord_bridge.services.formatting import partition_link_preview_attachments

_PAGE = "https://www.instagram.com/p/abc"
_PREVIEW = Attachment(url="https://cdn.example/abc.jpg", source_page_url=_PAGE)
_NATIVE = Attachment(url="https://cdn.example/upload.png")


class _Prefs:
    def __init__(self, rules: dict[str, str]):
        self._rules = rules

    async def preferred_kind_for(self, url: str) -> str | None:
        return next((kind for sub, kind in self._rules.items() if sub in url), None)


async def test_default_keeps_the_attachment_and_strips_the_link():
    content, attachments = await partition_link_preview_attachments(
        f"look {_PAGE}", [_NATIVE, _PREVIEW], my_kind="stoat", preferences=_Prefs({})
    )
    assert content == "look"
    assert attachments == [_NATIVE, _PREVIEW]


async def test_a_rule_for_the_other_kind_still_uses_the_attachment():
    content, attachments = await partition_link_preview_attachments(
        f"look {_PAGE}", [_PREVIEW], my_kind="stoat", preferences=_Prefs({"instagram.com": "discord"})
    )
    assert content == "look"
    assert attachments == [_PREVIEW]


async def test_a_rule_for_my_kind_drops_the_attachment_and_keeps_the_link():
    content, attachments = await partition_link_preview_attachments(
        f"look {_PAGE}", [_NATIVE, _PREVIEW], my_kind="stoat", preferences=_Prefs({"instagram.com": "stoat"})
    )
    assert content == f"look {_PAGE}"
    assert attachments == [_NATIVE]


async def test_no_preview_support_keeps_the_link_and_drops_the_attachment():
    # IRC: no unfurler, but the page link is more useful than a bare media URL.
    content, attachments = await partition_link_preview_attachments(
        f"look {_PAGE}", [_NATIVE, _PREVIEW], my_kind=None, preferences=None
    )
    assert content == f"look {_PAGE}"
    assert attachments == [_NATIVE]


async def test_a_preview_whose_link_isnt_in_the_text_is_always_kept():
    # Nothing left to rebuild the preview from, so the media is all there is.
    for my_kind, prefs in (("stoat", _Prefs({"instagram.com": "stoat"})), (None, None)):
        content, attachments = await partition_link_preview_attachments(
            "no link here", [_PREVIEW], my_kind=my_kind, preferences=prefs
        )
        assert content == "no link here"
        assert attachments == [_PREVIEW]


async def test_only_the_whole_link_is_stripped_not_a_longer_one_sharing_its_prefix():
    content, _ = await partition_link_preview_attachments(
        f"{_PAGE}def and {_PAGE}.", [_PREVIEW], my_kind="stoat", preferences=None
    )
    assert content == f"{_PAGE}def and ."


async def test_a_link_only_present_as_a_prefix_of_another_is_not_in_the_text():
    content, attachments = await partition_link_preview_attachments(
        f"{_PAGE}def", [_PREVIEW], my_kind=None, preferences=None
    )
    assert (content, attachments) == (f"{_PAGE}def", [_PREVIEW])


async def test_a_preview_with_no_page_url_is_kept():
    embed_only = Attachment(url="https://cdn.example/rich.png", source_page_url=None)
    content, attachments = await partition_link_preview_attachments(
        "hi", [embed_only], my_kind=None, preferences=None
    )
    assert (content, attachments) == ("hi", [embed_only])


async def test_a_raising_preference_lookup_falls_back_to_the_default():
    class _Broken:
        async def preferred_kind_for(self, url):
            raise RuntimeError("mongo down")

    content, attachments = await partition_link_preview_attachments(
        f"look {_PAGE}", [_PREVIEW], my_kind="stoat", preferences=_Broken()
    )
    assert (content, attachments) == ("look", [_PREVIEW])
