"""Spike #7 - pins the stoat.py gateway-event shapes that
StoatSenderService's member/role/channel update handlers read, plus the
handlers' own diffing behavior.

The introspection tests assert against the *installed* stoat.py (1.2.1 when
written): the field names, the `event_name` -> `on_<event_name>` handler
mapping (stoat.Client._dispatch does `getattr(self, 'on_' + event_name)`),
and the attribute surface of the payload objects the handlers pull off each
event. A stoat.py upgrade that renames any of these breaks the test rather
than silently breaking sync. What stays unverified here is live-server
payload completeness - whether `before` / `after` actually arrive populated,
which depends on the shard cache.
"""

from __future__ import annotations

from types import SimpleNamespace

import attr
import stoat
import stoat.events as stoat_events

from stoat_discord_bridge.services.stoat_service import StoatSenderService, _StoatClient


def _fields(cls) -> set[str]:
    return {f.name for f in attr.fields(cls)}


# ---------------------------------------------------------------- event shapes


def test_server_member_update_event_shape():
    ev = stoat_events.ServerMemberUpdateEvent
    assert ev.event_name == "server_member_update"
    assert {"member", "before", "after"} <= _fields(ev)
    # payloads the handler reads
    assert {"server_id", "role_ids"} <= _fields(stoat.Member)
    assert {"server_id", "role_ids"} <= _fields(stoat.PartialMember)
    for cls in (stoat.Member, stoat.PartialMember):
        assert isinstance(getattr(cls, "id"), property)
        assert isinstance(getattr(cls, "display_name"), property)


def test_raw_server_role_update_event_shape():
    ev = stoat_events.RawServerRoleUpdateEvent
    # combined create+update; note old_role/new_role, NOT before/after
    assert ev.event_name == "raw_server_role_update"
    assert {"role", "old_role", "new_role", "server"} <= _fields(ev)
    assert "before" not in _fields(ev) and "after" not in _fields(ev)
    assert {"id", "name", "server_id"} <= _fields(stoat.PartialRole)
    assert {"id", "name", "server_id"} <= {
        n for n in dir(stoat.Role) if not n.startswith("_")
    }


def test_server_role_delete_event_shape():
    ev = stoat_events.ServerRoleDeleteEvent
    assert ev.event_name == "server_role_delete"
    assert {"server_id", "role_id"} <= _fields(ev)


def test_channel_update_event_shape():
    ev = stoat_events.ChannelUpdateEvent
    assert ev.event_name == "channel_update"
    assert {"channel", "before", "after"} <= _fields(ev)
    # a server channel carries the role permission overrides the handler diffs
    for cls in (stoat.TextChannel, stoat.VoiceChannel, stoat.PartialChannel):
        assert "role_permissions" in _fields(cls)
    override = stoat.PermissionOverride()
    assert hasattr(override, "allow") and hasattr(override, "deny")


def test_message_update_event_shape():
    ev = stoat_events.MessageUpdateEvent
    assert ev.event_name == "message_update"
    # the edit handler prefers `after` (full Message) and falls back to the
    # partial `message`; `before` is unused but part of the documented shape
    assert {"message", "before", "after"} <= _fields(ev)


def test_handler_names_match_the_event_name_dispatch_convention():
    # stoat.Client._dispatch: `getattr(self, 'on_' + type.event_name)`
    for ev in (
        stoat_events.ServerMemberUpdateEvent,
        stoat_events.RawServerRoleUpdateEvent,
        stoat_events.ServerRoleDeleteEvent,
        stoat_events.ChannelUpdateEvent,
        stoat_events.MessageUpdateEvent,
    ):
        assert hasattr(_StoatClient, "on_" + ev.event_name)


# ---------------------------------------------------------------- handler behavior


def _sender(**hooks):
    s = object.__new__(StoatSenderService)
    s.connector_id = "stoat"
    s.server_id = "srv-1"
    s._on_member_roles_changed = hooks.get("on_member_roles_changed")
    s._on_role_renamed = hooks.get("on_role_renamed")
    s._on_role_deleted = hooks.get("on_role_deleted")
    s._on_channel_role_permission_changed = hooks.get("on_channel_role_permission_changed")
    return s


def _member(*, id="u1", server_id="srv-1", role_ids):
    return SimpleNamespace(id=id, server_id=server_id, role_ids=list(role_ids))


async def test_handle_member_update_reports_the_role_id_diff():
    seen = []

    async def on_change(connector_id, user_id, added, removed):
        seen.append((connector_id, user_id, added, removed))

    s = _sender(on_member_roles_changed=on_change)
    event = SimpleNamespace(
        member=SimpleNamespace(id="u1"),
        before=_member(role_ids=["r1", "r2"]),
        after=_member(role_ids=["r2", "r3"]),
    )
    await s._handle_member_update(event)
    assert seen == [("stoat", "u1", {"r3"}, {"r1"})]


async def test_handle_member_update_noop_when_role_ids_unchanged():
    seen = []

    async def on_change(*a):
        seen.append(a)

    s = _sender(on_member_roles_changed=on_change)
    event = SimpleNamespace(
        member=SimpleNamespace(id="u1"),
        before=_member(role_ids=["r1", "r2"]),
        after=_member(role_ids=["r2", "r1"]),
    )
    await s._handle_member_update(event)
    assert seen == []


async def test_handle_member_update_ignores_another_server():
    seen = []

    async def on_change(*a):
        seen.append(a)

    s = _sender(on_member_roles_changed=on_change)
    event = SimpleNamespace(
        member=SimpleNamespace(id="u1"),
        before=_member(server_id="other", role_ids=[]),
        after=_member(server_id="other", role_ids=["r1"]),
    )
    await s._handle_member_update(event)
    assert seen == []


async def test_handle_member_update_bails_when_after_is_uncached():
    seen = []

    async def on_change(*a):
        seen.append(a)

    s = _sender(on_member_roles_changed=on_change)
    await s._handle_member_update(
        SimpleNamespace(member=SimpleNamespace(id="u1"), before=None, after=None)
    )
    assert seen == []


async def test_handle_role_update_propagates_a_rename():
    seen = []

    async def on_rename(connector_id, role_id, new_name):
        seen.append((connector_id, role_id, new_name))

    s = _sender(on_role_renamed=on_rename)
    event = SimpleNamespace(
        role=SimpleNamespace(id="r1"),
        old_role=SimpleNamespace(id="r1", name="Old"),
        new_role=SimpleNamespace(id="r1", name="New"),
    )
    await s._handle_role_update(event)
    assert seen == [("stoat", "r1", "New")]


async def test_handle_role_update_ignores_a_creation_or_uncached_server():
    # old_role is None both for a freshly-created role and when the server
    # isn't cached - either way there's nothing to rename.
    seen = []

    async def on_rename(*a):
        seen.append(a)

    s = _sender(on_role_renamed=on_rename)
    event = SimpleNamespace(
        role=SimpleNamespace(id="r1", name="Fresh"), old_role=None, new_role=None
    )
    await s._handle_role_update(event)
    assert seen == []


async def test_handle_role_update_noop_when_name_unchanged():
    seen = []

    async def on_rename(*a):
        seen.append(a)

    s = _sender(on_role_renamed=on_rename)
    event = SimpleNamespace(
        role=SimpleNamespace(id="r1"),
        old_role=SimpleNamespace(id="r1", name="Same"),
        new_role=SimpleNamespace(id="r1", name="Same"),
    )
    await s._handle_role_update(event)
    assert seen == []


async def test_handle_role_delete_drops_just_that_connectors_mapping():
    seen = []

    async def on_delete(connector_id, role_id):
        seen.append((connector_id, role_id))

    s = _sender(on_role_deleted=on_delete)
    await s._handle_role_delete(SimpleNamespace(server_id="srv-1", role_id="r1", role=None))
    assert seen == [("stoat", "r1")]


async def test_handle_role_delete_ignores_another_server():
    seen = []

    async def on_delete(*a):
        seen.append(a)

    s = _sender(on_role_deleted=on_delete)
    await s._handle_role_delete(SimpleNamespace(server_id="other", role_id="r1", role=None))
    assert seen == []


async def test_handle_channel_update_mirrors_a_changed_role_override():
    seen = []

    async def on_perm(connector_id, channel_id, role_id, override, *, is_category):
        seen.append((connector_id, channel_id, role_id, override, is_category))

    s = _sender(on_channel_role_permission_changed=on_perm)
    before = SimpleNamespace(id="42", role_permissions={})
    after = SimpleNamespace(
        id="42",
        role_permissions={"r1": SimpleNamespace(allow=SimpleNamespace(send_messages=True), deny=SimpleNamespace())},
    )
    await s._handle_channel_update(SimpleNamespace(channel=after, before=before, after=after))
    assert len(seen) == 1
    connector_id, channel_id, role_id, override, is_category = seen[0]
    assert (connector_id, channel_id, role_id, is_category) == ("stoat", "42", "r1", False)
    assert "send_messages" in override.allow


async def test_handle_channel_update_noop_when_no_override_changed():
    seen = []

    async def on_perm(*a, **k):
        seen.append((a, k))

    s = _sender(on_channel_role_permission_changed=on_perm)
    shared = SimpleNamespace(allow=SimpleNamespace(), deny=SimpleNamespace())
    channel = SimpleNamespace(id="42", role_permissions={"r1": shared})
    await s._handle_channel_update(
        SimpleNamespace(channel=channel, before=channel, after=channel)
    )
    assert seen == []


async def test_handle_channel_update_falls_back_to_the_partial_channel():
    seen = []

    async def on_perm(connector_id, channel_id, role_id, override, *, is_category):
        seen.append((channel_id, role_id))

    s = _sender(on_channel_role_permission_changed=on_perm)
    partial = SimpleNamespace(
        id="42",
        role_permissions={"r1": SimpleNamespace(allow=SimpleNamespace(view_channel=True), deny=SimpleNamespace())},
    )
    # before/after uncached -> handler diffs event.channel against {}
    await s._handle_channel_update(SimpleNamespace(channel=partial, before=None, after=None))
    assert seen == [("42", "r1")]
