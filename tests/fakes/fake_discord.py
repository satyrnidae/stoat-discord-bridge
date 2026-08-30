"""Reusable in-memory stand-ins for the slice of discord.py's object graph
DiscordReceiverService/DiscordSenderService actually touch (channels,
webhooks, guilds, users/avatars, raw reaction payloads) - shared scaffolding
so individual test files don't each hand-roll their own ad hoc SimpleNamespace
fakes for the same handful of discord.py shapes.

None of this subclasses the real discord.py types - discord.Client's
constructor and friends do real (if network-free) setup work that isn't
worth carrying into a fake, so these are duck-typed stand-ins covering only
the attributes/methods this codebase's services/discord_service.py reads.
"""

from __future__ import annotations

from typing import Any

import discord


class _FakeHttpResponse:
    """Minimal stand-in for the aiohttp response object discord.py's
    HTTPException reads `.status`/`.reason` off of - enough to construct a
    real discord.NotFound/HTTPException without a live request."""

    def __init__(self, *, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason


class FakeAsset:
    """Stands in for discord.Asset. Unlike stoat.py's Asset, discord.py
    exposes the URL as a plain `.url` attribute, not a `.url()` method."""

    def __init__(self, url: str, *, read_bytes: bytes = b"avatar-bytes") -> None:
        self.url = url
        self._read_bytes = read_bytes

    async def read(self) -> bytes:
        return self._read_bytes


class FakeUser:
    def __init__(
        self,
        id: int,
        *,
        display_name: str = "User",
        bot: bool = False,
        display_avatar: FakeAsset | None = None,
    ) -> None:
        self.id = id
        self.display_name = display_name
        self.bot = bot
        self.display_avatar = display_avatar

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeUser) and self.id == other.id


class FakeAttachment:
    def __init__(self, url: str, *, filename: str | None = None, content_type: str | None = None, size: int = 0) -> None:
        self.url = url
        self.filename = filename
        self.content_type = content_type
        self.size = size


class FakeSentMessage:
    def __init__(self, id: int) -> None:
        self.id = id


class FakePartialMessage:
    """Stands in for discord.PartialMessage - the handle
    DiscordReceiverService.add_reaction/remove_reaction operate on."""

    def __init__(self) -> None:
        self.added_reactions: list[Any] = []
        self.removed_reactions: list[tuple[Any, Any]] = []

    async def add_reaction(self, emoji) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji, user) -> None:
        self.removed_reactions.append((emoji, user))


class FakeReaction:
    """Stands in for a discord.Reaction on a fetched full message."""

    def __init__(self, emoji: Any, *, count: int = 1, me: bool = False) -> None:
        self.emoji = emoji
        self.count = count
        self.me = me


class FakeFullMessage:
    """Stands in for the discord.Message returned by channel.fetch_message -
    carries `.reactions` (used by _reactor_count and the receiver's
    own-reaction idempotency check) and the pin handle DiscordReceiverService.set_pinned
    operates on."""

    def __init__(
        self, id: int, *, reactions: list[FakeReaction] | None = None, pinned: bool = False
    ) -> None:
        self.id = id
        self.reactions: list[FakeReaction] = reactions or []
        self.pinned = pinned
        self.pin_calls: list[str | None] = []
        self.unpin_calls: list[str | None] = []

    async def pin(self, *, reason: str | None = None) -> None:
        self.pin_calls.append(reason)
        self.pinned = True

    async def unpin(self, *, reason: str | None = None) -> None:
        self.unpin_calls.append(reason)
        self.pinned = False


class FakeWebhook:
    def __init__(self, id: int, *, user: FakeUser | None = None, raises: BaseException | None = None) -> None:
        self.id = id
        self.user = user
        self.sent: list[dict] = []
        self._raises = raises
        self._next_message_id = 1000

    async def send(
        self,
        *,
        content: str,
        username: str,
        avatar_url: str | None,
        wait: bool = True,
        thread: Any = None,
    ) -> FakeSentMessage:
        if self._raises is not None:
            raise self._raises
        self.sent.append({"content": content, "username": username, "avatar_url": avatar_url, "thread": thread})
        message_id = self._next_message_id
        self._next_message_id += 1
        return FakeSentMessage(id=message_id)


class FakeChannel:
    def __init__(self, id: int, *, name: str = "general", webhooks: list[FakeWebhook] | None = None) -> None:
        self.id = id
        self.name = name
        self._webhooks = webhooks or []
        self.created_webhooks: list[FakeWebhook] = []
        self.partial_messages: dict[int, FakePartialMessage] = {}
        self.full_messages: dict[int, FakeFullMessage] = {}
        self.typing_calls = 0

    async def typing(self) -> None:
        self.typing_calls += 1

    async def webhooks(self) -> list[FakeWebhook]:
        return list(self._webhooks)

    async def create_webhook(self, *, name: str, avatar: bytes | None = None) -> FakeWebhook:
        webhook = FakeWebhook(id=len(self._webhooks) + len(self.created_webhooks) + 1)
        webhook.created_with = {"name": name, "avatar": avatar}
        self._webhooks.append(webhook)
        self.created_webhooks.append(webhook)
        return webhook

    def get_partial_message(self, message_id: int) -> FakePartialMessage:
        return self.partial_messages.setdefault(message_id, FakePartialMessage())

    async def fetch_message(self, message_id: int) -> FakeFullMessage:
        return self.full_messages.setdefault(message_id, FakeFullMessage(message_id))


class FakeThread(discord.Thread):
    """Stands in for discord.Thread - a Discord thread (and a forum post,
    which discord.py also represents as a Thread whose .parent is the
    ForumChannel) has no webhooks of its own, so DiscordReceiverService
    resolves/creates webhooks on .parent instead and passes thread= to
    Webhook.send(). Subclasses the real discord.Thread (rather than duck
    typing, like the other fakes here) so isinstance() checks in the
    receiver see it as a thread, but skips Thread.__init__ - which needs a
    real guild/state/payload - setting only id/name/parent directly, and
    shadowing the inherited `parent` property (which has no setter) with
    its own.
    """

    def __init__(
        self,
        id: int,
        *,
        parent: FakeChannel,
        name: str = "thread",
        guild: FakeGuild | None = None,
        starter_message: Any = None,
    ) -> None:
        self.id = id
        self.name = name
        self._parent = parent
        # `starter_message` on the real Thread is a cache-backed property with
        # no setter (like `parent`) - shadow it so tests can supply one.
        self._starter_message = starter_message
        # `guild` is a plain instance attribute on the real Thread (not a
        # property), set in its real __init__ - which this fake skips - so
        # it's assigned directly here rather than shadowed via a property
        # like `parent` below.
        self.guild = guild

    @property
    def parent(self) -> FakeChannel:
        return self._parent

    @property
    def starter_message(self) -> Any:
        return self._starter_message


class FakeGuildChannel(discord.TextChannel):
    """Stands in for discord.TextChannel - used to test
    DiscordSenderService._handle_channel_create, which does an
    isinstance(channel, (discord.TextChannel, discord.VoiceChannel)) check
    that a plain duck-typed FakeChannel can't satisfy. Subclasses the real
    discord.TextChannel (same pattern as FakeThread above), skipping its real
    __init__ - which needs a real guild/state/payload - and setting only
    id/name/guild/category directly.
    """

    def __init__(
        self, id: int, *, name: str = "general", guild: FakeGuild | None = None, category: FakeChannel | None = None
    ) -> None:
        self.id = id
        self.name = name
        self.guild = guild
        self._category = category

    @property
    def category(self) -> FakeChannel | None:
        return self._category


class FakeEmoji:
    def __init__(self, id: int, name: str, *, url: str = "https://cdn.example/emoji.png", animated: bool = False) -> None:
        self.id = id
        self.name = name
        self.url = url
        self.animated = animated


class FakeGuild:
    def __init__(self, id: int, *, raises: BaseException | None = None) -> None:
        self.id = id
        self._raises = raises
        self.created_emoji_calls: list[dict] = []
        self._next_emoji_id = 1
        self._members: dict[int, FakeUser] = {}

    def add_member(self, member: FakeUser) -> FakeUser:
        self._members[member.id] = member
        return member

    def get_member(self, user_id: int) -> FakeUser | None:
        return self._members.get(user_id)

    async def fetch_member(self, user_id: int) -> FakeUser:
        member = self._members.get(user_id)
        if member is None:
            raise discord.NotFound(_FakeHttpResponse(status=404, reason="Not Found"), "member not found")
        return member

    async def create_custom_emoji(self, *, name: str, image: bytes) -> FakeEmoji:
        if self._raises is not None:
            raise self._raises
        self.created_emoji_calls.append({"name": name, "image": image})
        emoji = FakeEmoji(id=self._next_emoji_id, name=name)
        self._next_emoji_id += 1
        return emoji


class FakeClient:
    """Stands in for the discord.Client instance DiscordReceiverService is
    constructed with. `user` is the bridge bot's own identity - used both as
    the "did the bridge already make a webhook here" check and as the
    fallback avatar source when a relayed message's own avatar is unknown."""

    def __init__(self, *, user: FakeUser | None = None) -> None:
        self.user = user or FakeUser(id=1, display_name="Bridge", display_avatar=FakeAsset("https://cdn.example/bot.png"))
        self._channels: dict[int, FakeChannel] = {}
        self._guilds: dict[int, FakeGuild] = {}
        self._users: dict[int, FakeUser] = {}

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self._channels[channel.id] = channel
        return channel

    def add_guild(self, guild: FakeGuild) -> FakeGuild:
        self._guilds[guild.id] = guild
        return guild

    def add_user(self, user: FakeUser) -> FakeUser:
        self._users[user.id] = user
        return user

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        channel = self._channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(_FakeHttpResponse(status=404, reason="Not Found"), "channel not found")
        return channel

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self._guilds.get(guild_id)

    def get_user(self, user_id: int) -> FakeUser | None:
        return self._users.get(user_id)

    async def fetch_user(self, user_id: int) -> FakeUser:
        user = self._users.get(user_id)
        if user is None:
            raise discord.NotFound(_FakeHttpResponse(status=404, reason="Not Found"), "user not found")
        return user


class FakePartialEmoji:
    def __init__(self, *, name: str | None, id: int | None = None, animated: bool = False, url: str = "") -> None:
        self.name = name
        self.id = id
        self.animated = animated
        self.url = url

    def is_custom_emoji(self) -> bool:
        return self.id is not None

    def __str__(self) -> str:
        return self.name or ""


class FakeRawReactionActionEvent:
    def __init__(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        emoji: FakePartialEmoji,
        member: FakeUser | None = None,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.user_id = user_id
        self.emoji = emoji
        self.member = member
