"""`reorder_roles` on the Stoat connector (issue #199)."""

from __future__ import annotations

from types import SimpleNamespace

from tests.fakes.fake_stoat import FakeClient, FakeServer
from tests.stoat_service.conftest import _make_sender


class _RankServer(FakeServer):
    def __init__(self, *ranked_ids):
        super().__init__(id="s1")
        # Smaller rank = higher in Stoat's role list.
        self.roles = {rid: SimpleNamespace(id=rid, name=rid, rank=rank) for rank, rid in enumerate(ranked_ids)}
        self.rank_edits = []

    async def bulk_edit_role_ranks(self, ranks):
        self.rank_edits.append(list(ranks))


def _sender(server):
    client = FakeClient()
    client.add_server(server)
    return _make_sender(client=client)


async def test_reorder_roles_sends_every_role_with_the_given_ones_reshuffled():
    # Stoat's API needs every role in the list. "a" and "c" swap places;
    # "b" and "d" aren't named, so they keep theirs.
    server = _RankServer("a", "b", "c", "d")
    sender = _sender(server)

    await sender.reorder_roles(["c", "a"])

    assert server.rank_edits == [["c", "b", "a", "d"]]


async def test_reorder_roles_is_a_noop_when_already_in_order():
    server = _RankServer("a", "b", "c")
    sender = _sender(server)

    await sender.reorder_roles(["a", "c"])

    assert server.rank_edits == []


async def test_reorder_roles_ignores_roles_that_arent_cached():
    server = _RankServer("a", "b")
    sender = _sender(server)

    await sender.reorder_roles(["zz", "b", "a"])

    assert server.rank_edits == [["b", "a"]]
