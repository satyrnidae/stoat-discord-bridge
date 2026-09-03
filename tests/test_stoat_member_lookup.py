"""StoatSenderService's member lookups, which back the bare-name form of the
`/link user` etc. commands and role auto-grant's member iteration.

Spike (issue #9): `Server.members` is a `Mapping[str, Member]` keyed by user
id in stoat.py 1.2.1 - a plain `dict` off the cache, `{}` when uncached -
never a list. `_all_members` / `_members_of` therefore take the
`list(members.values())` path; the `list(members)` branch is only a
defensive fallback.
"""

from __future__ import annotations

import pytest

from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeAuthor, FakeClient, FakeServer

pytestmark = pytest.mark.asyncio


def _sender(server: FakeServer) -> StoatSenderService:
    client = FakeClient()
    client.add_server(server)
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._client = client
    sender.server_id = server.id
    return sender


def _with_members(*members) -> StoatSenderService:
    server = FakeServer("s1")
    for m in members:
        server.add_member(m.id, m)
    return _sender(server)


async def test_all_members_reads_the_mapping_values_not_its_keys():
    alice = FakeAuthor("01ALICE", name="alice")
    bob = FakeAuthor("01BOB", name="bob")
    sender = _with_members(alice, bob)

    assert {m.id for m in sender._all_members()} == {"01ALICE", "01BOB"}


async def test_resolve_user_id_by_name_matches_id_name_nick_and_display_name():
    m = FakeAuthor("01ULID", name="corvid", display_name="Corvid Jay", nick="CJ")
    sender = _with_members(m)

    assert await sender.resolve_user_id_by_name("01ULID") == "01ULID"
    assert await sender.resolve_user_id_by_name("corvid") == "01ULID"
    assert await sender.resolve_user_id_by_name("CORVID") == "01ULID"
    assert await sender.resolve_user_id_by_name("Corvid Jay") == "01ULID"
    assert await sender.resolve_user_id_by_name("cj") == "01ULID"


async def test_resolve_user_id_by_name_returns_none_for_an_unknown_token():
    sender = _with_members(FakeAuthor("01ULID", name="corvid"))

    assert await sender.resolve_user_id_by_name("nobody") is None


async def test_resolve_user_id_by_name_handles_an_empty_member_cache():
    sender = _sender(FakeServer("s1"))

    assert sender._all_members() == []
    assert await sender.resolve_user_id_by_name("anyone") is None
