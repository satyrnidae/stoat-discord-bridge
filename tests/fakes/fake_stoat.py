"""Reusable in-memory stand-ins for the slice of stoat.py's object graph
StoatSenderService/StoatReceiverService actually touch (channels, servers,
members/users, masquerade sends, custom emoji) - the stoat.py counterpart of
fake_discord.py, see that module's docstring for the general rationale.

stoat.py's Asset exposes its URL via a `.url()` *method* (see
stoat_service.py's _avatar_url docstring, confirmed against the installed
package), unlike discord.py's plain `.url` attribute - FakeAsset here
matches that method shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import stoat


def _paginate_fake_stoat_history(
    history: list[Any], *, limit: int | None, before: Any, after: Any, sort: Any
) -> list[Any]:
    """Shared backing for `FakeChannel.history`/`FakePartialMessageable.history`
    (issue #122's fetch_history pagination): `history` is always stored
    oldest-first. `after=<id>` (forward pagination, `sort=oldest`) returns
    everything strictly after it, oldest-first; `before=<id>` (backward
    pagination, `sort=latest`) returns everything strictly before it,
    newest-first - matching the two directions `fetch_history` paginates in.
    With neither cursor, `sort=latest` starts from the newest message,
    `sort=oldest` (or no sort) from the oldest. `limit` caps the page from
    whichever end it's read."""
    messages = history
    if after is not None:
        after_id = str(getattr(after, "id", after))
        idx = next((i for i, m in enumerate(messages) if str(m.id) == after_id), -1)
        messages = messages[idx + 1 :]
    elif before is not None:
        before_id = str(getattr(before, "id", before))
        idx = next((i for i, m in enumerate(messages) if str(m.id) == before_id), len(messages))
        messages = list(reversed(messages[:idx]))
    elif sort == stoat.MessageSort.latest:
        messages = list(reversed(messages))
    if limit is not None:
        messages = messages[:limit]
    return list(messages)


class FakeAsset:
    def __init__(self, url: str) -> None:
        self._url = url

    def url(self) -> str:
        return self._url


class FakeAuthor:
    """Stands in for a stoat.Member or stoat.User, whichever _handle_message
    would see as `message.author`."""

    def __init__(
        self,
        id: str,
        *,
        name: str = "user",
        tag: str = "user#0000",
        display_name: str | None = None,
        nick: str | None = None,
        bot: bool = False,
        server_avatar: FakeAsset | None = None,
        avatar: FakeAsset | None = None,
        default_avatar_url: str = "https://cdn.example/default.png",
    ) -> None:
        self.id = id
        self.name = name
        self.tag = tag
        self.display_name = display_name
        self.nick = nick
        self.bot = bot
        self.server_avatar = server_avatar
        self.avatar = avatar
        self.default_avatar_url = default_avatar_url


class FakeSentMessage:
    def __init__(self, id: str) -> None:
        self.id = id


class FakeStoatMessage:
    """Stands in for a stoat.Message with a live react/unreact handle - what
    StoatReceiverService.add_reaction/remove_reaction operate on via
    channel.get_message(id, partial=True) - plus a `reactions` dict
    (emoji -> reactor user ids) that channel.fetch_message exposes for the
    receiver's own-reaction idempotency check."""

    def __init__(
        self, id: str, *, reactions: dict[str, tuple[str, ...]] | None = None, pinned: bool = False
    ) -> None:
        self.id = id
        self.reactions: dict[str, tuple[str, ...]] = dict(reactions or {})
        self.added_reactions: list[Any] = []
        self.removed_reactions: list[Any] = []
        self.pinned = pinned
        self.pin_calls = 0
        self.unpin_calls = 0
        self.content: str | None = None
        self.edits: list[str | None] = []
        self.deleted = False
        # settable post-construction to make delete() raise, for testing that
        # one already-gone post doesn't stop the rest of a multi-id delete.
        self.raises_on_delete: BaseException | None = None

    async def edit(self, *, content=None, **kwargs) -> "FakeStoatMessage":
        self.content = content
        self.edits.append(content)
        return self

    async def delete(self) -> None:
        if self.raises_on_delete is not None:
            raise self.raises_on_delete
        self.deleted = True

    async def react(self, emoji) -> None:
        self.added_reactions.append(emoji)

    async def unreact(self, emoji) -> None:
        self.removed_reactions.append(emoji)

    async def pin(self) -> None:
        self.pin_calls += 1
        self.pinned = True

    async def unpin(self) -> None:
        self.unpin_calls += 1
        self.pinned = False


class FakeChannel:
    def __init__(
        self,
        id: str,
        *,
        name: str = "general",
        server_id: str | None = None,
        raises: BaseException | None = None,
        category: Any = None,
        description: str | None = None,
        nsfw: bool = False,
        icon: Any = None,
        viewable_by: set[str] | None = None,
        voice: Any = None,
    ) -> None:
        self.id = id
        self.name = name
        self.server_id = server_id
        self.sent: list[dict] = []
        self._raises = raises
        self._messages: dict[str, FakeStoatMessage] = {}
        self._next_message_id = 1
        self.category = category
        self.typing_events: list[str] = []
        self.description = description
        self.nsfw = nsfw
        self.icon = icon
        # None for a plain text channel; set (issue #146) for a voice-capable
        # one - matches `_channel_supports_voice`'s `channel.voice is not
        # None` check, which is how a modern Stoat voice channel is
        # distinguished from the deprecated dedicated stoat.VoiceChannel type.
        self.voice = voice
        self.edits: list[dict] = []
        # When set, the channel models stoat.py's ServerChannel.permissions_for:
        # a member id in the set sees the channel, one outside it doesn't.
        self._viewable_by = viewable_by
        # Oldest-first fake backing for `.history()` (issue #122's
        # fetch_history) - see `set_history`.
        self._history: list[Any] = []
        # Made-up rate-limit failures for `.history()` (issue #151) - see
        # `set_history_raises`.
        self._history_raise_exc: BaseException | None = None
        self._history_raise_remaining: int | None = None

    def set_history(self, messages: list[Any]) -> None:
        """Seed this channel's fake history, oldest-first."""
        self._history = list(messages)

    def set_history_raises(self, exc: BaseException, *, times: int | None = None) -> None:
        """Make the next `history()` call(s) raise `exc` instead of
        returning a page (issue #151's rate-limit-retry handling) -
        `times=None` raises on every call (a persistent-failure case),
        otherwise the given number of calls raise before falling back to
        `history()`'s normal paginated behavior."""
        self._history_raise_exc = exc
        self._history_raise_remaining = times

    async def history(
        self,
        *,
        limit: int | None = None,
        before: Any = None,
        after: Any = None,
        sort: Any = None,
        nearby: Any = None,
        populate_users: bool | None = None,
        **_kwargs: Any,
    ) -> list[Any]:
        """Stands in for stoat.py's `TextChannel.history` - a single page,
        honoring `after`/`sort=oldest` (forward pagination) or
        `before`/`sort=latest` (backward pagination), the two directions
        `fetch_history`'s pagination uses (issue #122), and `limit` as a
        page-size cap (real stoat.py caps this at 100 - not enforced here,
        since `fetch_history` is what applies that cap)."""
        if self._raises is not None:
            raise self._raises
        if self._history_raise_exc is not None and (
            self._history_raise_remaining is None or self._history_raise_remaining > 0
        ):
            if self._history_raise_remaining is not None:
                self._history_raise_remaining -= 1
            raise self._history_raise_exc
        return _paginate_fake_stoat_history(self._history, limit=limit, before=before, after=after, sort=sort)

    async def edit(self, **kwargs) -> "FakeChannel":
        if self._raises is not None:
            raise self._raises
        self.edits.append(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    def permissions_for(self, target, /):
        if self._viewable_by is None:
            raise AttributeError("this FakeChannel doesn't model permissions")
        return SimpleNamespace(view_channel=str(getattr(target, "id", target)) in self._viewable_by)

    async def begin_typing(self) -> None:
        if self._raises is not None:
            raise self._raises
        self.typing_events.append("begin")

    async def end_typing(self) -> None:
        self.typing_events.append("end")

    async def send(self, content: str, *, masquerade=None, attachments=None, replies=None) -> FakeSentMessage:
        if self._raises is not None:
            raise self._raises
        record = {"content": content, "masquerade": masquerade}
        if attachments:
            record["attachments"] = list(attachments)
        if replies:
            record["replies"] = list(replies)
        self.sent.append(record)
        message_id = str(self._next_message_id)
        self._next_message_id += 1
        return FakeSentMessage(id=message_id)

    def get_message(self, message_id: str, *, partial: bool = True) -> FakeStoatMessage:
        return self._messages.setdefault(message_id, FakeStoatMessage(id=message_id))

    async def fetch_message(self, message_id: str) -> FakeStoatMessage:
        if self._raises is not None:
            raise self._raises
        return self._messages.setdefault(message_id, FakeStoatMessage(id=message_id))


class FakeLocalParticipant:
    """Stands in for livekit.rtc.LocalParticipant - just `identity` (for the
    echo-guard identity check) and `publish_track` (issue #113 Phase 3)."""

    def __init__(self, identity: str = "bridge") -> None:
        self.identity = identity
        self.publish_track_calls: list = []
        self.publish_options_calls: list = []

    async def publish_track(self, track, options=None):
        self.publish_track_calls.append(track)
        self.publish_options_calls.append(options)
        return None


class FakeStoatRoom:
    """Stands in for the livekit.rtc.Room `VoiceChannel.connect()` returns -
    `disconnect` (issue #113 Phase 2) plus `on`/`local_participant` (Phase
    3's send/receive wiring). `.on(event, callback)` just records the
    callback under its event name - `trigger(event, *args)` invokes it,
    standing in for LiveKit actually firing the event."""

    def __init__(self) -> None:
        self.disconnect_calls = 0
        self.local_participant = FakeLocalParticipant()
        self._listeners: dict[str, object] = {}

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    def on(self, event: str, callback=None):
        self._listeners[event] = callback
        return callback

    def trigger(self, event: str, *args) -> None:
        self._listeners[event](*args)


class FakeVoiceChannel(stoat.VoiceChannel):
    """Stands in for stoat.VoiceChannel - needed for the
    isinstance(channel, stoat.VoiceChannel) check `channel_is_voice`
    (issue #113) uses. Skips the real (attrs-generated) __init__, same
    pattern as fake_discord.py's discord.py subclass fakes; `.voice_states`
    is shadowed with a bare participants dict since the real property reads
    a gateway-fed cache this fake has none of.

    `connect_result`/`connect_error` (issue #113 Phase 2) drive
    `StoatVoiceConnector.join`'s test double, mirroring
    fake_discord.py's `FakeVoiceChannel.connect` - `connect_calls` records
    every `node=` kwarg `connect()` was called with."""

    def __init__(
        self,
        id: str,
        *,
        name: str = "voice",
        server_id: str | None = None,
        participants: "dict[str, Any] | None" = None,
        connect_result: "FakeStoatRoom | None" = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.server_id = server_id
        self._participants = participants or {}
        self._connect_result = connect_result
        self._connect_error = connect_error
        self.connect_calls: list[str | None] = []

    @property
    def voice_states(self) -> SimpleNamespace:
        return SimpleNamespace(participants=self._participants)

    async def connect(self, *, node: str | None = None, **kwargs):
        self.connect_calls.append(node)
        if self._connect_error is not None:
            raise self._connect_error
        return self._connect_result or FakeStoatRoom()


class FakeVoiceEnabledTextChannel(stoat.TextChannel):
    """Stands in for a *modern* Stoat voice channel: an ordinary
    `TextChannel` whose `.voice` metadata is set, rather than the legacy
    dedicated `stoat.VoiceChannel` type/parser (deprecated server-side since
    API 0.7.0 - a live server no longer ever sends it). Same
    skip-the-real-`__init__` pattern as `FakeVoiceChannel` above; `.voice`
    is just a truthy sentinel since `_channel_supports_voice` only checks
    it's not `None`."""

    def __init__(
        self,
        id: str,
        *,
        name: str = "voice",
        server_id: str | None = None,
        participants: "dict[str, Any] | None" = None,
    ) -> None:
        self.id = id
        self.name = name
        self.server_id = server_id
        self.voice = SimpleNamespace(max_users=0)
        self._participants = participants or {}

    @property
    def voice_states(self) -> SimpleNamespace:
        return SimpleNamespace(participants=self._participants)


class FakePartialMessageable:
    """What stoat.py's Client.get_channel(..., partial=True) hands back on a
    cache miss: an id plus the Messageable send/typing/fetch_message surface,
    and deliberately no `.name` / `.category` / `.role_permissions` - those
    live only on a fully cached channel."""

    def __init__(self, id: str) -> None:
        self.id = id
        self.sent: list[dict] = []
        self.typing_events: list[str] = []
        self._messages: dict[str, FakeStoatMessage] = {}
        self._next_message_id = 1
        self._history: list[Any] = []

    async def begin_typing(self) -> None:
        self.typing_events.append("begin")

    async def end_typing(self) -> None:
        self.typing_events.append("end")

    async def send(self, content: str, *, masquerade=None, attachments=None, replies=None) -> FakeSentMessage:
        record = {"content": content, "masquerade": masquerade}
        if attachments:
            record["attachments"] = list(attachments)
        if replies:
            record["replies"] = list(replies)
        self.sent.append(record)
        message_id = str(self._next_message_id)
        self._next_message_id += 1
        return FakeSentMessage(id=message_id)

    def get_message(self, message_id: str, *, partial: bool = True) -> FakeStoatMessage:
        return self._messages.setdefault(message_id, FakeStoatMessage(id=message_id))

    async def fetch_message(self, message_id: str) -> FakeStoatMessage:
        return self._messages.setdefault(message_id, FakeStoatMessage(id=message_id))

    def set_history(self, messages: list[Any]) -> None:
        self._history = list(messages)

    async def history(
        self,
        *,
        limit: int | None = None,
        before: Any = None,
        after: Any = None,
        sort: Any = None,
        nearby: Any = None,
        populate_users: bool | None = None,
        **_kwargs: Any,
    ) -> list[Any]:
        return _paginate_fake_stoat_history(self._history, limit=limit, before=before, after=after, sort=sort)


class FakeCategory:
    def __init__(self, id: str, title: str, *, channels: list[str] | None = None) -> None:
        self.id = id
        self.title = title
        self.channels = channels or []


class FakeEmoji:
    def __init__(self, id: str, name: str, *, animated: bool = False) -> None:
        self.id = id
        self.name = name
        self.animated = animated
        self.image = FakeAsset(f"https://cdn.example/emoji/{id}.png")


class FakeServer:
    def __init__(self, id: str, *, raises: BaseException | None = None) -> None:
        self.id = id
        self.channels: list[Any] = []
        self.categories: list[Any] = []
        self._raises = raises
        self.created_channels: list[str] = []
        self.created_channel_calls: list[dict] = []
        self.created_categories: list[dict] = []
        self.server_edits: list[dict] = []
        self.state = SimpleNamespace(http=SimpleNamespace(request=self._http_request))
        self.created_emoji_calls: list[dict] = []
        self._members: dict[str, Any] = {}
        self._next_emoji_id = 1
        # id -> FakeEmoji; `Server.emojis` is a Mapping in stoat.py
        self._emojis: dict[str, Any] = {}
        self.fetch_emojis_calls = 0

    @property
    def emojis(self):
        return dict(self._emojis)

    def add_emoji(self, emoji) -> None:
        self._emojis[str(emoji.id)] = emoji

    def get_emoji(self, emoji_id: str):
        return self._emojis.get(str(emoji_id))

    async def fetch_emojis(self, **kwargs):
        self.fetch_emojis_calls += 1
        return list(self._emojis.values())

    @property
    def members(self):
        # `BaseServer.members` is a Mapping[id, Member] in stoat.py 1.2.1
        return dict(self._members)

    def add_member(self, user_id: str, member) -> None:
        self._members[user_id] = member

    async def fetch_member(self, user_id: str):
        member = self._members.get(user_id)
        if member is None:
            raise LookupError(f"no such member: {user_id}")
        return member

    async def create_channel(
        self, *, name: str, description: str | None = None, nsfw: bool | None = None, **kwargs: Any
    ):
        self.created_channels.append(name)
        call = {"name": name, "description": description, "nsfw": nsfw}
        call.update(kwargs)
        self.created_channel_calls.append(call)
        channel = FakeChannel(
            id=f"chan-{name}",
            name=name,
            server_id=self.id,
            description=description,
            nsfw=bool(nsfw),
            voice=kwargs.get("voice"),
        )
        self.channels.append(channel)
        return channel

    async def create_category(self, name: str, *, channels: list[str]):
        self.created_categories.append({"name": name, "channels": channels})
        category = FakeCategory(id=f"cat-{name}", title=name, channels=list(channels))
        self.categories.append(category)
        return category

    async def edit_category(self, category, *, channels: list[str]):
        category.channels = list(channels)
        return category

    async def _http_request(self, compiled_route, *, json=None, **kwargs):
        # Older-Stoat fallback path: PATCH /servers/{id} with a hand-built
        # {categories: [{id,title,channels}]} payload.
        self.server_edits.append(json)
        if json and "categories" in json:
            self.categories = [
                FakeCategory(id=c["id"], title=c["title"], channels=list(c["channels"])) for c in json["categories"]
            ]
        return json

    async def create_server_emoji(self, name: str, *, image, nsfw=None):
        if self._raises is not None:
            raise self._raises
        # `image` is a stoat.Upload; unwrap its bytes for assertions
        self.created_emoji_calls.append({"name": name, "image": getattr(image, "content", image)})
        emoji = FakeEmoji(id=str(self._next_emoji_id), name=name)
        self._next_emoji_id += 1
        return emoji


class FakeClient:
    def __init__(self, *, user: Any = None) -> None:
        # The bridge bot's own identity (issue #154's call-started notice
        # reads this the same way fake_discord.py's FakeClient.user already
        # does) - defaults to a real fake user rather than None, matching
        # the assumption that it's always populated once actually logged in
        # (see StoatSenderService._handle_ready, which sets stoat.Client.user
        # as a side effect of storing event.me).
        self.user: Any = user if user is not None else FakeAuthor(id="bridge-bot-id", name="Bridge", display_name="Bridge")
        self._channels: dict[str, FakeChannel] = {}
        self._fetched_channels: dict[str, FakeChannel] = {}
        self._servers: dict[str, FakeServer] = {}
        self._fresh_servers: dict[str, FakeServer] = {}
        self._users: dict[str, Any] = {}
        # channel_id -> raw JSON dict (or an exception to raise), for a raw
        # GET /channels/{id} via `self.http` - StoatLookupsMixin's
        # `_fetch_channel_slowmode` reads a channel's slowmode this way since
        # stoat.py's typed Channel drops the field (issue #108).
        self._channel_fetch_responses: dict[str, Any] = {}
        self.http_calls: list[tuple[str, str, Any]] = []  # (method, path, json)
        self.http = SimpleNamespace(request=self._http_request)

    def set_channel_fetch_response(self, channel_id: str, response: Any) -> None:
        self._channel_fetch_responses[channel_id] = response

    async def _http_request(self, compiled_route, *, json: Any = None, **kwargs) -> Any:
        method = compiled_route.route.method
        path = compiled_route.build()
        self.http_calls.append((method, path, json))
        channel_id = path.rsplit("/", 1)[-1] if path.startswith("/channels/") else None
        if method == "GET" and channel_id in self._channel_fetch_responses:
            response = self._channel_fetch_responses[channel_id]
            if isinstance(response, BaseException):
                raise response
            return response
        raise RuntimeError(f"no fake http response configured for {method} {path}")

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self._channels[channel.id] = channel
        return channel

    def set_fetched_channel(self, channel: FakeChannel) -> FakeChannel:
        """Make `fetch_channel` return `channel` without it being in the
        cache `get_channel` reads - models a channel `refresh_groups`'s
        cache-only classification misses (issue #66's drift) but a live
        `fetch_channel` fallback still resolves."""
        self._fetched_channels[channel.id] = channel
        return channel

    def add_server(self, server: FakeServer) -> FakeServer:
        self._servers[server.id] = server
        return server

    def set_fetched_server(self, server: FakeServer) -> FakeServer:
        """Make `fetch_server` return `server` while `get_server` (the cache)
        keeps returning whatever `add_server` registered - so a test can model
        a cached Server whose category list has drifted from the real one."""
        self._fresh_servers[server.id] = server
        return server

    def add_user(self, user_id: str, user) -> None:
        self._users[user_id] = user

    def get_channel(self, channel_id: str, *, partial: bool = False):
        # Matches stoat.py 1.2.1's Client.get_channel: a cache-only lookup
        # that, on a miss, returns None (partial=False) or a bare
        # PartialMessageable stub (partial=True) - it never raises for a
        # missing channel and never does I/O. FakePartialMessageable carries
        # just the send/typing/fetch_message surface, no .name/.category.
        channel = self._channels.get(channel_id)
        if channel is not None:
            return channel
        return FakePartialMessageable(channel_id) if partial else None

    def get_user(self, user_id: str, *, partial: bool = False):
        # Matches stoat.py 1.2.1's Client.get_user: a cache-only lookup that
        # returns None on a miss and never does I/O.
        return self._users.get(user_id)

    def get_server(self, server_id: str, *, partial: bool = False) -> FakeServer:
        server = self._servers.get(server_id)
        if server is None:
            raise LookupError(f"no such server: {server_id}")
        return server

    async def fetch_server(self, server_id: str, *, populate_channels: bool = False) -> FakeServer:
        server = self._fresh_servers.get(server_id) or self._servers.get(server_id)
        if server is None:
            raise LookupError(f"no such server: {server_id}")
        return server

    async def fetch_user(self, user_id: str):
        user = self._users.get(user_id)
        if user is None:
            raise LookupError(f"no such user: {user_id}")
        return user

    async def fetch_channel(self, channel_id: str):
        channel = self._fetched_channels.get(channel_id) or self._channels.get(channel_id)
        if channel is None:
            raise LookupError(f"no such channel: {channel_id}")
        return channel
