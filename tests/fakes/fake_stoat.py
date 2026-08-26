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
    """Stands in for a stoat.Message with a live add_reaction/remove_reaction
    handle - what StoatReceiverService.add_reaction/remove_reaction operate
    on via channel.get_message(id, partial=True)."""

    def __init__(self, id: str) -> None:
        self.id = id
        self.added_reactions: list[Any] = []
        self.removed_reactions: list[Any] = []

    async def add_reaction(self, emoji) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji) -> None:
        self.removed_reactions.append(emoji)


class FakeChannel:
    def __init__(self, id: str, *, name: str = "general", server_id: str | None = None, raises: BaseException | None = None) -> None:
        self.id = id
        self.name = name
        self.server_id = server_id
        self.sent: list[dict] = []
        self._raises = raises
        self._messages: dict[str, FakeStoatMessage] = {}
        self._next_message_id = 1

    async def send(self, content: str, *, masquerade=None) -> FakeSentMessage:
        if self._raises is not None:
            raise self._raises
        self.sent.append({"content": content, "masquerade": masquerade})
        message_id = str(self._next_message_id)
        self._next_message_id += 1
        return FakeSentMessage(id=message_id)

    def get_message(self, message_id: str, *, partial: bool = True) -> FakeStoatMessage:
        return self._messages.setdefault(message_id, FakeStoatMessage(id=message_id))


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
        self.created_emoji_calls: list[dict] = []
        self._members: dict[str, Any] = {}
        self._next_emoji_id = 1

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

    async def create_emoji(self, *, name: str, image: bytes):
        if self._raises is not None:
            raise self._raises
        self.created_emoji_calls.append({"name": name, "image": image})
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

    async def fetch_user(self, user_id: str):
        user = self._users.get(user_id)
        if user is None:
            raise LookupError(f"no such user: {user_id}")
        return user
