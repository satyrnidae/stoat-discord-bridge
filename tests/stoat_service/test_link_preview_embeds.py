"""`stoat_service.formatting._link_preview_embed_attachments` (issue #164) -
Stoat's own resolved link-preview media, carried across as attachments."""

from types import SimpleNamespace

import stoat

from stoat_discord_bridge.models import Attachment
from stoat_discord_bridge.services.stoat_service.formatting import _link_preview_embed_attachments


def _image(url):
    return stoat.ImageEmbed(url=url, width=640, height=480, size=stoat.ImageSize.large)


def _video(url):
    return stoat.VideoEmbed(url=url, width=640, height=480)


def _website(*, url, original_url=None, image=None, video=None):
    return stoat.WebsiteEmbed(
        url=url, original_url=original_url, special=None, title="t", description=None,
        image=image, video=video, site_name=None, icon_url=None, color=None,
    )


def _message(*embeds):
    return SimpleNamespace(embeds=list(embeds))


def test_website_embed_image_becomes_an_attachment_keyed_by_the_posted_url():
    embed = _website(
        url="https://www.instagram.com/p/abc/",
        original_url="https://instagram.com/p/abc",
        image=_image("https://scontent.cdninstagram.com/abc.jpg"),
    )

    assert _link_preview_embed_attachments(_message(embed)) == [
        Attachment(
            url="https://scontent.cdninstagram.com/abc.jpg",
            filename="preview.jpg",
            content_type="image/jpeg",
            source_page_url="https://instagram.com/p/abc",
        )
    ]


def test_website_embed_prefers_a_video_file_over_the_image():
    embed = _website(
        url="https://example.com/clip",
        video=_video("https://cdn.example.com/clip.mp4"),
        image=_image("https://cdn.example.com/still.png"),
    )

    [attachment] = _link_preview_embed_attachments(_message(embed))
    assert attachment.url == "https://cdn.example.com/clip.mp4"
    assert attachment.source_page_url == "https://example.com/clip"


def test_website_embed_skips_a_video_player_url_for_the_image():
    embed = _website(
        url="https://example.com/watch",
        video=_video("https://example.com/player/embed"),
        image=_image("https://cdn.example.com/still.png"),
    )

    [attachment] = _link_preview_embed_attachments(_message(embed))
    assert attachment.url == "https://cdn.example.com/still.png"


def test_bare_image_and_video_embeds_point_at_themselves():
    image = _image("https://cdn.example.com/cat.gif")
    video = _video("https://cdn.example.com/dog.mp4")

    assert _link_preview_embed_attachments(_message(image, video)) == [
        Attachment(
            url="https://cdn.example.com/cat.gif",
            filename="gif.gif",
            content_type="image/gif",
            source_page_url="https://cdn.example.com/cat.gif",
        ),
        Attachment(
            url="https://cdn.example.com/dog.mp4",
            filename="preview.mp4",
            content_type="video/mp4",
            source_page_url="https://cdn.example.com/dog.mp4",
        ),
    ]


def test_media_less_and_text_embeds_are_skipped():
    text = stoat.StatelessTextEmbed(
        icon_url=None, url=None, title="hi", description=None, internal_media=None, color=None
    )
    assert _link_preview_embed_attachments(_message(_website(url="https://example.com"), text)) == []


def test_a_message_with_no_embeds_yields_nothing():
    assert _link_preview_embed_attachments(SimpleNamespace()) == []
