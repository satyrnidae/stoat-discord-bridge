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
        self.edited: list[dict] = []
        self.deleted: list[int] = []
        self._raises = raises
        # per-message_id override, for testing that one already-gone post
        # doesn't stop the rest of a multi-id delete_message call.
        self.raise_on_delete: dict[int, BaseException] = {}
        self._next_message_id = 1000

    async def send(
        self,
        *,
        content: str,
        username: str,
        avatar_url: str | None,
        wait: bool = True,
        thread: Any = None,
        files: Any = None,
    ) -> FakeSentMessage:
        if self._raises is not None:
            raise self._raises
        record = {"content": content, "username": username, "avatar_url": avatar_url, "thread": thread}
        if files:
            record["files"] = [(f.filename, f.fp.read()) for f in files]
        self.sent.append(record)
        message_id = self._next_message_id
        self._next_message_id += 1
        return FakeSentMessage(id=message_id)

    async def edit_message(self, message_id: int, *, content: str, thread: Any = None) -> FakeSentMessage:
        if self._raises is not None:
            raise self._raises
        self.edited.append({"message_id": message_id, "content": content, "thread": thread})
        return FakeSentMessage(id=message_id)

    async def delete_message(self, message_id: int, *, thread: Any = None) -> None:
        if message_id in self.raise_on_delete:
            raise self.raise_on_delete[message_id]
        if self._raises is not None:
            raise self._raises
        self.deleted.append(message_id)


class FakeHistoryIterator:
    """Stands in for discord.py's `HistoryIterator` (the return value of
    `TextChannel.history()`) - just enough to support `async for message in
    channel.history(...)` (issue #122's fetch_history). `_history_messages`
    is always stored oldest-first; `oldest_first=False` (discord.py's own
    default) reverses it, and `limit` truncates from the requested end -
    matching discord.py's own semantics closely enough for these fakes."""

    def __init__(self, messages: list[Any], *, limit: int | None, oldest_first: bool) -> None:
        ordered = list(messages) if oldest_first else list(reversed(messages))
        self._messages = ordered if limit is None else ordered[:limit]

    def __aiter__(self):
        return self._generator()

    async def _generator(self):
        for message in self._messages:
            yield message


class FakeChannel:
    def __init__(
        self, id: int, *, name: str = "general", webhooks: list[FakeWebhook] | None = None,
        history_messages: list[Any] | None = None, guild: Any = None, edit_error: Exception | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.guild = guild
        self._webhooks = webhooks or []
        self.created_webhooks: list[FakeWebhook] = []
        self.partial_messages: dict[int, FakePartialMessage] = {}
        self.full_messages: dict[int, FakeFullMessage] = {}
        self.typing_calls = 0
        # Oldest-first, regardless of what order the test hands in - see
        # FakeHistoryIterator.
        self._history_messages = history_messages or []
        self._edit_error = edit_error
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        if self._edit_error is not None:
            raise self._edit_error
        self.edits.append(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)

    def set_history(self, messages: list[Any]) -> None:
        self._history_messages = messages

    def history(self, *, limit: int | None = None, oldest_first: bool = False, **_kwargs: Any) -> FakeHistoryIterator:
        return FakeHistoryIterator(self._history_messages, limit=limit, oldest_first=oldest_first)

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


class FakeForumChannel(discord.ForumChannel):
    """Stands in for discord.ForumChannel (a forum or media channel) - the
    DiscordReceiverService rejects relaying into one outright (issue #69), an
    isinstance(channel, discord.ForumChannel) check a duck-typed FakeChannel
    can't satisfy. Subclasses the real class (same pattern as FakeThread),
    skipping its real __init__. Still carries FakeChannel's webhook machinery
    because a forum *post* (a FakeThread whose .parent is this) resolves its
    webhook on the parent forum channel.
    """

    def __init__(
        self,
        id: int,
        *,
        name: str = "forum",
        webhooks: list[FakeWebhook] | None = None,
        threads: list[Any] | None = None,
        guild: "FakeGuild | None" = None,
        category: Any = None,
    ) -> None:
        self.id = id
        self.name = name
        self._webhooks = webhooks or []
        self.created_webhooks: list[FakeWebhook] = []
        # `forum.threads` / `.category` are read-only properties on the real
        # class - shadow them here (same pattern as FakeThread.parent).
        self._threads = threads or []
        self.guild = guild
        self._category = category

    @property
    def threads(self) -> list[Any]:
        # active (non-archived) posts - what channels_in_category enumerates
        # for a forum-as-Category (issue #100).
        return self._threads

    @property
    def category(self) -> Any:
        return self._category

    async def webhooks(self) -> list[FakeWebhook]:
        return list(self._webhooks)

    async def create_webhook(self, *, name: str, avatar: bytes | None = None) -> FakeWebhook:
        webhook = FakeWebhook(id=len(self._webhooks) + len(self.created_webhooks) + 1)
        webhook.created_with = {"name": name, "avatar": avatar}
        self._webhooks.append(webhook)
        self.created_webhooks.append(webhook)
        return webhook


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


class FakeDiscordVoiceClient:
    """Stands in for the `discord.ext.voice_recv.VoiceRecvClient`
    `VoiceChannel.connect()` returns - `is_connected`/`disconnect` (issue
    #113 Phase 2) plus `listen`/`stop_listening`/`is_listening` and
    `play`/`stop`/`is_playing` (Phase 3's send/receive surface)."""

    def __init__(self) -> None:
        self.connected = True
        self.disconnect_calls: list[bool] = []
        self.listening = False
        self.listen_calls: list[object] = []
        self.stop_listening_calls = 0
        self.playing = False
        self.play_calls: list[object] = []
        self.stop_calls = 0
        self._after: object = None

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnect_calls.append(force)
        self.connected = False

    def listen(self, sink, *, after=None) -> None:
        self.listen_calls.append(sink)
        self.listening = True
        self._after = after

    def is_listening(self) -> bool:
        return self.listening

    def stop_listening(self) -> None:
        self.stop_listening_calls += 1
        self.listening = False

    def simulate_listen_stopped(self, error: "Exception | None" = None) -> None:
        """Stands in for `discord-ext-voice-recv`'s `AudioReader._stop()`
        calling its `after` callback once receiving stops - matches real
        behavior's ordering (the reader is no longer "listening" *before*
        `after` fires) so a test exercising the restart path
        (`DiscordVoiceTransport._on_listen_stopped`) sees the same state
        `is_listening()` would report at that point for real."""
        self.listening = False
        after, self._after = self._after, None
        if after is not None:
            after(error)

    def play(self, source, **kwargs) -> None:
        self.play_calls.append(source)
        self.playing = True

    def is_playing(self) -> bool:
        return self.playing

    def stop(self) -> None:
        self.stop_calls += 1
        self.playing = False


class FakeVoiceChannel(discord.VoiceChannel):
    """Stands in for discord.VoiceChannel - needed for the
    isinstance(channel, discord.VoiceChannel) check `channel_is_voice`
    (issue #113) uses. Skips the real __init__ (same pattern as
    FakeGuildChannel); `.members` is shadowed since the real property reads
    the guild's voice-state cache, which this fake has none of.

    `connect_result`/`connect_error` (issue #113 Phase 2) drive
    `DiscordVoiceConnector.join`'s test double: a channel either hands back
    a `FakeDiscordVoiceClient` from `connect()` or raises `connect_error` -
    `connect_calls` records every `cls=` kwarg `connect()` was called with."""

    def __init__(
        self,
        id: int,
        *,
        name: str = "voice",
        guild: FakeGuild | None = None,
        members: "list[FakeUser] | None" = None,
        connect_result: "FakeDiscordVoiceClient | None" = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.guild = guild
        self._members = members or []
        self._connect_result = connect_result
        self._connect_error = connect_error
        self.connect_calls: list[object] = []

    @property
    def members(self) -> "list[FakeUser]":
        return self._members

    async def connect(self, *, cls=None, **kwargs):
        self.connect_calls.append(cls)
        if self._connect_error is not None:
            raise self._connect_error
        return self._connect_result or FakeDiscordVoiceClient()


class FakeEmoji:
    def __init__(self, id: int, name: str, *, url: str = "https://cdn.example/emoji.png", animated: bool = False) -> None:
        self.id = id
        self.name = name
        self.url = url
        self.animated = animated


class FakeCategoryChannel(discord.CategoryChannel):
    """Stands in for discord.CategoryChannel - needed for the
    isinstance(..., discord.CategoryChannel) checks in the Category-placement
    hooks. Skips the real __init__ (same pattern as FakeGuildChannel)."""

    def __init__(self, id: int, *, name: str = "Team", guild: "FakeGuild | None" = None) -> None:
        self.id = id
        self.name = name
        self.guild = guild


class FakeGuild:
    def __init__(self, id: int, *, raises: BaseException | None = None) -> None:
        self.id = id
        self._raises = raises
        self.created_emoji_calls: list[dict] = []
        self._next_emoji_id = 1
        self._members: dict[int, FakeUser] = {}
        self.text_channels: list = []
        self.voice_channels: list = []
        self.categories: list = []
        self.created_text_channels: list[dict] = []
        self.created_voice_channels: list[dict] = []
        self.created_categories: list[str] = []

    async def create_text_channel(self, name: str, *, reason: str | None = None, **kwargs) -> FakeGuildChannel:
        self.created_text_channels.append({"name": name, **kwargs})
        channel = FakeGuildChannel(id=self._next_emoji_id + 5000, name=name, guild=self, category=kwargs.get("category"))
        self._next_emoji_id += 1
        channel.topic = kwargs.get("topic")
        channel.nsfw = bool(kwargs.get("nsfw", False))
        self.text_channels.append(channel)
        return channel

    async def create_voice_channel(self, name: str, *, reason: str | None = None, **kwargs) -> "FakeVoiceChannel":
        self.created_voice_channels.append({"name": name, **kwargs})
        channel = FakeVoiceChannel(id=self._next_emoji_id + 6000, name=name, guild=self)
        self._next_emoji_id += 1
        self.voice_channels.append(channel)
        return channel

    async def create_category(self, name: str, *, reason: str | None = None) -> FakeCategoryChannel:
        self.created_categories.append(name)
        category = FakeCategoryChannel(id=self._next_emoji_id + 9000, name=name, guild=self)
        self._next_emoji_id += 1
        self.categories.append(category)
        return category

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
        self._emojis: dict[int, object] = {}

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

    def add_emoji(self, emoji_id: int, emoji: object) -> object:
        self._emojis[emoji_id] = emoji
        return emoji

    def get_emoji(self, emoji_id: int) -> object | None:
        return self._emojis.get(emoji_id)

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
