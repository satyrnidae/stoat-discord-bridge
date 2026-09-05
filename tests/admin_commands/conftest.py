import pytest

from stoat_discord_bridge.admin_commands import ConnectorInfo


@pytest.fixture
def connectors():
    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }
