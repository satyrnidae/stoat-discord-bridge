"""`entity_gone` - the shared "is this link's entity definitely deleted?"
check (issue #217). Only a hook answering exactly False counts."""

from __future__ import annotations

from stoat_discord_bridge.admin_commands import ConnectorInfo, entity_gone


def _connectors(answer=None, *, raises: BaseException | None = None, wired: bool = True):
    calls: list[tuple[str, str]] = []

    async def entity_exists(kind: str, entity_id: str):
        calls.append((kind, entity_id))
        if raises is not None:
            raise raises
        return answer

    info = ConnectorInfo(id="discord", label="Discord",entity_exists=entity_exists if wired else None)
    return {"discord": info}, calls


async def test_a_definite_false_is_gone():
    connectors, calls = _connectors(False)

    assert await entity_gone(connectors, "discord", "role", "5") is True
    assert calls == [("role", "5")]


async def test_existing_or_cant_tell_is_not_gone():
    for answer in (True, None):
        connectors, _ = _connectors(answer)
        assert await entity_gone(connectors, "discord", "role", "5") is False


async def test_a_raising_hook_is_not_gone():
    connectors, _ = _connectors(raises=RuntimeError("boom"))

    assert await entity_gone(connectors, "discord", "role", "5") is False


async def test_no_hook_or_unknown_connector_is_not_gone():
    connectors, _ = _connectors(False, wired=False)

    assert await entity_gone(connectors, "discord", "role", "5") is False
    assert await entity_gone(connectors, "irc", "channel", "#general") is False
