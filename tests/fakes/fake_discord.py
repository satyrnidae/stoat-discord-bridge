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


class FakeWebhook:
    def __init__(self, id: int, *, user: FakeUser | None = None, raises: BaseException | None = None) -> None:
        self.id = id
        self.user = user
        self.sent: list[dict] = []
        self._raises = raises
        self._next_message_id = 1000

    async def send(self, *, content: str, username: str, avatar_url: str | None, wait: bool = True) -> FakeSentMessage:
        if self._raises is not None:
            raise self._raises
        self.sent.append({"content": content, "username": username, "avatar_url": avatar_url})
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
            raise LookupError(f"no such channel: {channel_id}")
        return channel

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self._guilds.get(guild_id)

    def get_user(self, user_id: int) -> FakeUser | None:
        return self._users.get(user_id)


class FakePartialEmoji:
    def __init__(self, *, name: str | None, id: int | None = None, animated: bool = False, url: str = "") -> None:
        self.name = name
        self.id = id
        self.animated = animated
        self.url = url

    def is_custom_emoji(self) -> bool:
        return self.id is not None


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
