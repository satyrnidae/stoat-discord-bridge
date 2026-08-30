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

    async def begin_typing(self) -> None:
        if self._raises is not None:
            raise self._raises
        self.typing_events.append("begin")

    async def end_typing(self) -> None:
        self.typing_events.append("end")

    async def send(self, content: str, *, masquerade=None) -> FakeSentMessage:
        if self._raises is not None:
            raise self._raises
        self.sent.append({"content": content, "masquerade": masquerade})
        message_id = str(self._next_message_id)
        self._next_message_id += 1
        return FakeSentMessage(id=message_id)

    def get_message(self, message_id: str, *, partial: bool = True) -> FakeStoatMessage:
        return self._messages.setdefault(message_id, FakeStoatMessage(id=message_id))

    async def fetch_message(self, message_id: str) -> FakeStoatMessage:
        if self._raises is not None:
            raise self._raises
        return self._messages.setdefault(message_id, FakeStoatMessage(id=message_id))


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

    def add_member(self, user_id: str, member) -> None:
        self._members[user_id] = member

    async def fetch_member(self, user_id: str):
        member = self._members.get(user_id)
        if member is None:
            raise LookupError(f"no such member: {user_id}")
        return member

    async def create_channel(self, *, name: str):
        self.created_channels.append(name)
        channel = FakeChannel(id=f"chan-{name}", name=name, server_id=self.id)
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
    def __init__(self) -> None:
        self._channels: dict[str, FakeChannel] = {}
        self._servers: dict[str, FakeServer] = {}
        self._users: dict[str, Any] = {}

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self._channels[channel.id] = channel
        return channel

    def add_server(self, server: FakeServer) -> FakeServer:
        self._servers[server.id] = server
        return server

    def add_user(self, user_id: str, user) -> None:
        self._users[user_id] = user

    def get_channel(self, channel_id: str, *, partial: bool = False) -> FakeChannel:
        channel = self._channels.get(channel_id)
        if channel is None:
            raise LookupError(f"no such channel: {channel_id}")
        return channel

    def get_server(self, server_id: str, *, partial: bool = False) -> FakeServer:
        server = self._servers.get(server_id)
        if server is None:
            raise LookupError(f"no such server: {server_id}")
        return server

    async def fetch_server(self, server_id: str, *, populate_channels: bool = False) -> FakeServer:
        server = self._servers.get(server_id)
        if server is None:
            raise LookupError(f"no such server: {server_id}")
        return server

    async def fetch_user(self, user_id: str):
        user = self._users.get(user_id)
        if user is None:
            raise LookupError(f"no such user: {user_id}")
        return user
